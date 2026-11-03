"""Downstream reconciliation uses Athena SCCM rows, with no bundled evidence."""
import csv
import importlib
from datetime import date
from io import StringIO
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from omop_core.data_migrations import athena_sccm_reconcile_v1 as loader
from omop_core.models import Concept, SourceCodeConceptMapping as Mapping, SourceToConceptMap, Vocabulary
from tests.factories import DomainFactory
from tests.test_icd10_stcm_reconcile import (
    _mapping, athena_target, condition_domain, old_target, snomed_vocab,
)

migration = importlib.import_module('omop_core.migrations.0259_reconcile_approved_icd10_from_athena_sccm')
TODAY = date(2026, 9, 24)


def evidence(target, code='A01.0', vocab='ICD10CM', **kwargs):
    return _mapping(code, vocab, target, origin_system='athena', **kwargs)


def run(**kwargs):
    return loader.reconcile(apps, connection, today=TODAY, **kwargs)


@pytest.mark.django_db
def test_actual_migration_uses_historical_models_and_preserves_signoff(old_target, athena_target):
    from patient_portal.models import Identity
    reviewer = Identity.objects.create(issuer='local', sub='offline-reviewer', uid='offline-reviewer')
    row = _mapping('A01.0', 'ICD10', old_target)
    source = evidence(athena_target)
    source_before = Mapping.objects.values().get(pk=source.pk)
    Mapping.objects.filter(pk=row.pk).update(reviewer=reviewer, reviewed_at=timezone.now(), notes='Keep this note')
    before = Mapping.objects.values().get(pk=row.pk)
    assert not SourceToConceptMap.objects.exists()
    with override_settings(MIGRATION_MODULES={}):
        state = MigrationLoader(None).project_state(migration.Migration.dependencies)
    operation = migration.Migration('0259_reconcile_approved_icd10_from_athena_sccm', 'omop_core')
    with CaptureQueriesContext(connection) as queries:
        with connection.schema_editor(atomic=False) as editor:
            operation.apply(state, editor)
    after = Mapping.objects.values().get(pk=row.pk)
    assert after['target_concept_id'] == athena_target.pk
    assert after['origin_system'] == 'athena' and after['source'] == 'Athena'
    changed = {key for key in before if before[key] != after[key]}
    assert changed <= {'target_concept_id', 'destination_vocabulary_id', 'domain_id', 'omop_table',
                       'origin_system', 'source', 'notes', 'updated_at'}
    assert after['notes'].startswith('Keep this note\nAthena SCCM reconciliation (0259)')
    assert f'evidence SCCM [{source.pk}]' in after['notes']
    writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
    assert writes and all(q.startswith('UPDATE "source_code_concept_mapping"') for q in writes)
    assert Mapping.objects.values().get(pk=source.pk) == source_before
    assert run() == []


@pytest.mark.django_db
def test_same_destination_is_reattributed(athena_target):
    row = _mapping('A01.0', 'ICD10', athena_target)
    evidence(athena_target)
    assert loader.summarize(run()) == {'reattributed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.pk and row.origin_system == 'athena'


@pytest.mark.django_db
def test_dry_run_is_select_only(old_target, athena_target):
    row = _mapping('A01.0', 'ICD10', old_target)
    evidence(athena_target)
    before = Mapping.objects.values().get(pk=row.pk)
    with CaptureQueriesContext(connection) as queries:
        assert loader.summarize(run(dry_run=True)) == {'would_change_destination': 1}
    assert all(q['sql'].lstrip().upper().startswith('SELECT') and 'FOR UPDATE' not in q['sql'] for q in queries)
    assert Mapping.objects.values().get(pk=row.pk) == before


@pytest.mark.django_db
@pytest.mark.parametrize('vocab,source_vocab', [('ICD10', 'ICD10CM'), ('ICD10CM', 'ICD10'), ('ICD10CM', 'ICD10CM')])
@pytest.mark.parametrize('origin', ['HT-One', 'curator', 'suggest-v0.4'])
def test_merged_icd10_tab_matches_codes_for_every_origin(old_target, athena_target, vocab, source_vocab, origin):
    _mapping(' a01.0 ', vocab, old_target, origin_system=origin)
    evidence(athena_target, vocab=source_vocab)
    assert loader.summarize(run()) == {'destination_changed': 1}


@pytest.mark.django_db
@pytest.mark.parametrize('code', ['A010', 'A01.00', '', '  '])
def test_no_fuzzy_or_blank_code_matching(old_target, athena_target, code):
    _mapping(code, 'ICD10', old_target)
    evidence(athena_target)
    assert loader.summarize(run()) == {'not_found': 1}


@pytest.mark.django_db
@pytest.mark.parametrize('status', ['proposed', 'rejected'])
def test_unapproved_evidence_excluded(old_target, athena_target, status):
    _mapping('A01.0', 'ICD10', old_target)
    evidence(athena_target, status=status)
    assert loader.summarize(run()) == {'not_found': 1}


@pytest.mark.django_db
def test_no_fallback_to_other_provenance_or_vocabularies(old_target, athena_target):
    _mapping('A01.0', 'ICD10', old_target)
    _mapping('A01.0', 'ICD10CM', athena_target, origin_system='umls')
    evidence(athena_target, vocab='ICD9CM')
    assert loader.summarize(run()) == {'not_found': 2}


@pytest.mark.django_db
def test_conflicting_athena_destinations_skipped_before_validity_filter(old_target, athena_target):
    _mapping(' a01.0 ', 'ICD10', old_target)
    evidence(athena_target)
    evidence(old_target, vocab='ICD10')
    # An invalid alternative must not turn conflicting evidence into a winner.
    Concept.objects.filter(pk=old_target.pk).update(standard_concept=None)
    assert loader.summarize(run()) == {'ambiguous_targets': 1}


@pytest.mark.django_db
def test_duplicate_evidence_for_same_destination_deduplicates(old_target, athena_target):
    _mapping(' a01.0 ', 'ICD10', old_target)
    evidence(athena_target)
    evidence(athena_target, vocab='ICD10')
    assert loader.summarize(run()) == {'destination_changed': 1}


@pytest.mark.django_db
def test_incomplete_evidence_blocks(old_target, athena_target):
    _mapping('A01.0', 'ICD10', old_target)
    source = evidence(athena_target)
    Mapping.objects.filter(pk=source.pk).update(target_concept=None)
    assert loader.summarize(run()) == {'incomplete_evidence': 1}


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [
    {'standard_concept': None}, {'source': 'HealthKey'}, {'invalid_reason': 'D'},
    {'valid_end_date': date(2000, 1, 1)}, {'valid_start_date': date(2099, 1, 1)},
])
def test_invalid_destination_is_not_applied(old_target, athena_target, changes):
    row = _mapping('A01.0', 'ICD10', old_target)
    evidence(athena_target)
    Concept.objects.filter(pk=athena_target.pk).update(**changes)
    assert loader.summarize(run()) == {'invalid_local_target': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


@pytest.mark.django_db
def test_deprecated_vocabulary_is_not_applied(old_target, athena_target):
    _mapping('A01.0', 'ICD10', old_target)
    evidence(athena_target)
    Vocabulary.objects.filter(pk='SNOMED').update(is_deprecated=True)
    assert loader.summarize(run()) == {'invalid_local_target': 1}


@pytest.mark.django_db
def test_domain_and_destination_metadata_follow_standard_concept(old_target, athena_target):
    row = _mapping('A01.0', 'ICD10', old_target)
    evidence(athena_target)
    Concept.objects.filter(pk=athena_target.pk).update(domain=DomainFactory(domain_id='Observation'))
    assert loader.summarize(run()) == {'destination_changed': 1}
    row.refresh_from_db()
    assert (row.domain_id, row.omop_table, row.destination_vocabulary_id) == ('Observation', 'observation', 'SNOMED')


@pytest.mark.django_db
@pytest.mark.parametrize('lock_evidence,expected', [(False, 'locked'), (True, 'evidence_locked')])
def test_curator_locks_preserved(old_target, athena_target, lock_evidence, expected):
    from patient_portal.models import Identity
    user = Identity.objects.create(issuer='local', sub='offline-lock', uid='offline-lock')
    row = _mapping('A01.0', 'ICD10', old_target)
    source = evidence(athena_target)
    Mapping.objects.filter(pk=source.pk if lock_evidence else row.pk).update(locked_by=user)
    assert loader.summarize(run()) == {expected: 1}


@pytest.mark.django_db
@pytest.mark.parametrize('edit_evidence', [False, True])
def test_concurrent_edits_preserved(old_target, athena_target, edit_evidence):
    row = _mapping('A01.0', 'ICD10', old_target)
    source = evidence(athena_target)
    original = loader._batch

    def edit_then_apply(*args, **kwargs):
        Mapping.objects.filter(pk=source.pk if edit_evidence else row.pk).update(notes='Concurrent curator edit')
        return original(*args, **kwargs)

    with patch.object(loader, '_batch', side_effect=edit_then_apply):
        result = run()
    assert loader.summarize(result) == {('evidence_changed_during_audit' if edit_evidence else 'changed_during_audit'): 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


@pytest.mark.django_db
def test_new_conflicting_evidence_between_snapshot_and_apply_is_detected(old_target, athena_target):
    _mapping(' a01.0 ', 'ICD10', old_target)
    evidence(athena_target)
    original = loader._batch

    def edit_then_apply(*args, **kwargs):
        evidence(old_target, vocab='ICD10')
        return original(*args, **kwargs)

    with patch.object(loader, '_batch', side_effect=edit_then_apply):
        assert loader.summarize(run()) == {'evidence_changed_during_audit': 1}


@pytest.mark.django_db
def test_batches_do_not_turn_newly_converted_rows_into_evidence(old_target, athena_target):
    # Distinct stored codes with the same normalized code span a batch boundary.
    # Their original Athena row remains the only evidence, even after 250 writes.
    source = evidence(athena_target)
    rows = [Mapping(source_vocabulary_id='ICD10', source_code=' ' * (index // 80) + 'A01.0' + ' ' * (index % 80),
                    status='approved', origin_system='HealthTree', target_concept=old_target)
            for index in range(251)]
    Mapping.objects.bulk_create(rows)
    receipts = run()
    assert loader.summarize(receipts) == {'destination_changed': 251}
    assert all(row['evidence_mapping_ids'] == [source.pk] for row in receipts)
    assert run() == []


@pytest.mark.django_db
def test_proposed_and_rejected_candidates_untouched(old_target, athena_target):
    _mapping('A01.0', 'ICD10', old_target, status='proposed')
    _mapping(' A01.0 ', 'ICD10', old_target, status='rejected')
    evidence(athena_target)
    assert run() == []


@pytest.mark.django_db
def test_command_dry_run_csv_and_explicit_apply(old_target, athena_target):
    row = _mapping('A01.0', 'ICD10', old_target)
    evidence(athena_target)
    out, err = StringIO(), StringIO()
    call_command('reconcile_athena_sccm_mappings', 'ICD10', report='-', stdout=out, stderr=err)
    receipt, = list(csv.DictReader(StringIO(out.getvalue())))
    assert receipt['outcome'] == 'would_change_destination'
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'
    call_command('reconcile_athena_sccm_mappings', 'ICD10', apply=True, stdout=StringIO())
    row.refresh_from_db()
    assert row.origin_system == 'athena'


def test_migration_graph_preserves_old_nodes_but_disables_bundled_export():
    old = importlib.import_module('omop_core.migrations.0258_reconcile_approved_icd10_from_athena_export')
    assert old.Migration.operations == []
    with override_settings(MIGRATION_MODULES={}):
        graph = MigrationLoader(None).graph
    leaf, = graph.leaf_nodes('omop_core')
    plan = graph.forwards_plan(leaf)
    for name in ['0256_reconcile_icd10_mapped_against_stcm', '0258_reconcile_approved_icd10_from_athena_export',
                 '0259_reconcile_approved_icd10_from_athena_sccm']:
        assert ('omop_core', name) in plan
