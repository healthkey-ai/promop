"""Audit actual STCM matches without changing staging data."""
import csv
from datetime import date
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from omop_core.models import SourceCodeConceptMapping as Mapping
from omop_core.services import stcm_mapping_audit as audit
from tests.test_icd10_stcm_reconcile import (
    _mapping, _stcm, athena_target, condition_domain, old_target, snomed_vocab,
)

pytestmark = pytest.mark.django_db


def run_command(*args, **kwargs):
    stdout, stderr = StringIO(), StringIO()
    call_command('audit_athena_mapping_reconciliation', *args,
                 stdout=stdout, stderr=stderr, **kwargs)
    return stdout.getvalue(), stderr.getvalue()


def test_default_dry_run_reports_match_without_dml(old_target, athena_target):
    row = _mapping(' a01.0 ', 'ICD10CM', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)
    before = Mapping.objects.values().get(pk=row.pk)
    with CaptureQueriesContext(connection) as queries:
        stdout, stderr = run_command('ICD10CM', report='-')
    receipt, = list(csv.DictReader(StringIO(stdout)))
    assert receipt['exact_vocabulary_code_matches'] == '1'
    assert receipt['outcome'] == 'would_change_destination'
    assert receipt['proposed_target_id'] == str(athena_target.pk)
    assert 'Migration 0256' in stderr
    assert 'direct vocabulary/code STCM match: 1' in stderr
    assert Mapping.objects.values().get(pk=row.pk) == before
    for query in queries:
        sql = query['sql'].strip().upper()
        assert sql.startswith('SELECT'), sql
        assert 'FOR UPDATE' not in sql


def test_empty_stcm_reports_zero_not_an_inferred_mapping(old_target):
    _mapping('A01.0', 'ICD10CM', old_target)
    stdout, stderr = run_command(report='-')
    receipt, = list(csv.DictReader(StringIO(stdout)))
    assert receipt['outcome'] == 'no_stcm_match'
    assert 'STCM ICD10CM: 0 rows' in stderr


@pytest.mark.parametrize('field,value,outcome', [
    ('valid_start_date', date(2099, 1, 1), 'no_current_stcm_match'),
    ('valid_end_date', date(2000, 1, 1), 'no_current_stcm_match'),
    ('invalid_reason', 'D', 'no_current_stcm_match'),
])
def test_raw_match_survives_invalid_stcm(old_target, athena_target, field, value, outcome):
    row = _mapping('A01.0', 'ICD10CM', old_target)
    entry = _stcm('A01.0', 'ICD10CM', athena_target)
    type(entry).objects.filter(pk=entry.pk).update(**{field: value})
    receipt, = audit.audit_batch([row])
    assert receipt['exact_vocabulary_code_matches'] == 1
    assert receipt['outcome'] == outcome


@pytest.mark.parametrize('field,value', [
    ('valid_start_date', date(2099, 1, 1)),
    ('valid_end_date', date(2000, 1, 1)),
    ('invalid_reason', 'D'),
    ('standard_concept', None),
])
def test_invalid_targets_are_not_changes(old_target, athena_target, field, value):
    row = _mapping('A01.0', 'ICD10CM', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)
    type(athena_target).objects.filter(pk=athena_target.pk).update(**{field: value})
    receipt, = audit.audit_batch([row], apply=True)
    assert receipt['outcome'] == 'no_valid_standard_target'
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'


def test_multiple_distinct_targets_skip_but_duplicates_do_not(old_target, athena_target):
    row = _mapping('A01.0', 'ICD10CM', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)
    duplicate = _stcm(' A01.0 ', 'ICD10CM', athena_target)
    assert audit.audit_batch([row])[0]['outcome'] == 'would_change_destination'
    type(duplicate).objects.filter(pk=duplicate.pk).update(target_concept=old_target)
    assert audit.audit_batch([row], apply=True)[0]['outcome'] == 'multiple_targets'
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'


def test_ht_one_alias_report_distinguishes_direct_and_migration_matches(old_target, athena_target):
    row = _mapping('A01.0', 'ICD10', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)
    receipt, = audit.audit_batch([row])
    assert receipt['exact_vocabulary_code_matches'] == 0
    assert receipt['lookup_matches'] == 1
    assert receipt['lookup_vocabulary'] == 'ICD10CM'
    assert receipt['outcome'] == 'would_change_destination'


@pytest.mark.parametrize('vocabulary,provenance,stcm_vocabulary', [
    ('ICD10', 'curator', 'ICD10CM'), ('ICD10CM', 'HT-One', 'ICD10'),
])
def test_no_generic_cross_vocabulary_match(old_target, athena_target, vocabulary, provenance, stcm_vocabulary):
    row = _mapping('A01.0', vocabulary, old_target, origin_system=provenance)
    _stcm('A01.0', stcm_vocabulary, athena_target)
    assert audit.audit_batch([row])[0]['outcome'] == 'no_stcm_match'


@pytest.mark.parametrize('same_target', [True, False])
def test_apply_preserves_review_and_only_updates_mapping_table(old_target, athena_target, same_target):
    from patient_portal.models import Identity
    reviewer = Identity.objects.create(issuer='local', sub='audit-reviewer', uid='audit-reviewer')
    row = _mapping('A01.0', 'ICD10CM', old_target)
    Mapping.objects.filter(pk=row.pk).update(reviewer=reviewer, reviewed_at=timezone.now(), notes='Original note')
    target = old_target if same_target else athena_target
    _stcm('A01.0', 'ICD10CM', target)
    before = Mapping.objects.values().get(pk=row.pk)
    with CaptureQueriesContext(connection) as queries:
        stdout, _ = run_command(apply=True)
    after = Mapping.objects.values().get(pk=row.pk)
    assert after['target_concept_id'] == target.pk
    assert after['origin_system'] == 'athena'
    assert after['source'] == 'Athena'
    assert after['reviewer_id'] == before['reviewer_id']
    assert after['reviewed_at'] == before['reviewed_at']
    allowed = {'target_concept_id', 'destination_vocabulary_id', 'domain_id', 'omop_table',
               'origin_system', 'source', 'notes', 'updated_at'}
    assert {key for key in before if before[key] != after[key]} <= allowed
    writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))]
    assert writes and all(q.startswith('UPDATE "source_code_concept_mapping"') for q in writes)
    assert ('reattributed=1' if same_target else 'destination_changed=1') in stdout
    assert 'Approved non-Athena mappings: 0' in run_command(apply=True)[0]


def test_locked_mapping_is_reported_and_preserved(old_target, athena_target):
    from patient_portal.models import Identity
    user = Identity.objects.create(issuer='local', sub='audit-lock', uid='audit-lock')
    row = _mapping('A01.0', 'ICD10CM', old_target)
    Mapping.objects.filter(pk=row.pk).update(locked_by=user)
    _stcm('A01.0', 'ICD10CM', athena_target)
    assert 'locked=1' in run_command(apply=True)[0]
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


@pytest.mark.parametrize('change', [{'notes': 'Concurrent edit'}, {'status': 'rejected'}, {'origin_system': 'athena'}])
def test_concurrent_edits_preserved(old_target, athena_target, change):
    row = _mapping('A01.0', 'ICD10CM', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)
    Mapping.objects.filter(pk=row.pk).update(**change)
    assert audit.audit_batch([row], apply=True)[0]['outcome'] == 'changed_during_audit'
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


def test_command_only_processes_selected_approved_non_athena(old_target):
    _mapping('A', 'ICD10CM', old_target, status='proposed')
    _mapping('B', 'ICD10CM', old_target, origin_system='athena')
    _mapping('C', 'ICD10', old_target)
    stdout, stderr = run_command('ICD10CM', report='-')
    assert list(csv.DictReader(StringIO(stdout))) == []
    assert 'status=proposed' in stderr
    assert "provenance='athena'" in stderr


def test_report_failure_precedes_apply(old_target, tmp_path):
    _mapping('A', 'ICD10CM', old_target)
    with patch('omop_core.management.commands.audit_athena_mapping_reconciliation.audit_batch') as batch:
        with pytest.raises(CommandError, match='Cannot open report'):
            run_command(apply=True, report=str(tmp_path / 'missing' / 'report.csv'))
    batch.assert_not_called()
