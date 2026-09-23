"""Original-export reconciliation, independent of STCM or local mirrored edges."""
import csv
import zipfile
from datetime import date
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from omop_core.models import Concept, SourceCodeConceptMapping as Mapping, SourceToConceptMap
from omop_core.services import athena_mapping_reconciliation as service
from tests.factories import DomainFactory
from tests.test_icd10_stcm_reconcile import (
    _mapping, athena_target, condition_domain, old_target, snomed_vocab,
)

pytestmark = pytest.mark.django_db
COMMAND = 'omop_core.management.commands.reconcile_athena_mappings'
CONCEPT_FIELDS = ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id', 'concept_class_id',
                  'standard_concept', 'concept_code', 'valid_start_date', 'valid_end_date', 'invalid_reason')
REL_FIELDS = ('concept_id_1', 'concept_id_2', 'relationship_id', 'valid_start_date', 'valid_end_date', 'invalid_reason')


def run_command(*args, **kwargs):
    stdout, stderr = StringIO(), StringIO()
    call_command('reconcile_athena_mappings', *args, stdout=stdout, stderr=stderr, **kwargs)
    return stdout.getvalue(), stderr.getvalue()


def write_tsv(path, name, fields, rows):
    with (path / name).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fields, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def export(tmp_path, athena_target):
    source = dict(concept_id=123456, concept_name='ICD source', vocabulary_id='ICD10CM',
                  concept_code='A01.0', concept_class_id='4-char billing code', domain_id='Condition',
                  standard_concept='', valid_start_date='20000101', valid_end_date='20991231', invalid_reason='')
    target = dict(concept_id=athena_target.pk, concept_name=athena_target.concept_name,
                  vocabulary_id='SNOMED', concept_code=athena_target.concept_code,
                  concept_class_id=athena_target.concept_class_id, domain_id='Condition',
                  standard_concept='S', valid_start_date='20000101', valid_end_date='20991231', invalid_reason='')
    edge = dict(concept_id_1=123456, concept_id_2=athena_target.pk, relationship_id='Maps to',
                valid_start_date='20000101', valid_end_date='20991231', invalid_reason='')

    def build(*, source_changes=None, target_changes=None, edge_changes=None, extra_targets=(), extra_edges=()):
        write_tsv(tmp_path, 'CONCEPT.csv', CONCEPT_FIELDS,
                  [source | (source_changes or {}), target | (target_changes or {}), *extra_targets])
        write_tsv(tmp_path, 'CONCEPT_RELATIONSHIP.csv', REL_FIELDS,
                  [edge | (edge_changes or {}), *extra_edges])
        return str(tmp_path)

    build()
    return build, source, target, edge


def test_empty_stcm_and_no_local_source_still_yield_read_only_export_match(old_target, athena_target, export):
    build, _, _, _ = export
    row = _mapping(' a01.0 ', 'ICD10CM', old_target)
    assert not SourceToConceptMap.objects.exists()
    before = Mapping.objects.values().get(pk=row.pk)
    with CaptureQueriesContext(connection) as queries:
        out, err = run_command(path=build(), report='-')
    receipt, = list(csv.DictReader(StringIO(out)))
    assert receipt['outcome'] == 'would_change_destination'
    assert receipt['athena_target_ids'] == str(athena_target.pk)
    assert 'sha256=' in receipt['reference']
    assert 'No changes were made' in err
    assert Mapping.objects.values().get(pk=row.pk) == before
    assert all(q['sql'].lstrip().upper().startswith('SELECT') and 'FOR UPDATE' not in q['sql'] for q in queries)


@pytest.mark.parametrize('same_target', [True, False])
def test_apply_preserves_signoff_and_only_writes_sccm(old_target, athena_target, export, same_target):
    from patient_portal.models import Identity
    build, _, _, _ = export
    reviewer = Identity.objects.create(issuer='local', sub='export-reviewer', uid='export-reviewer')
    row = _mapping('A01.0', 'ICD10CM', athena_target if same_target else old_target)
    Mapping.objects.filter(pk=row.pk).update(reviewer=reviewer, reviewed_at=timezone.now(), notes='Keep this note')
    before = Mapping.objects.values().get(pk=row.pk)
    with CaptureQueriesContext(connection) as queries:
        run_command(path=build(), apply=True)
    after = Mapping.objects.values().get(pk=row.pk)
    assert after['target_concept_id'] == athena_target.pk
    assert after['origin_system'] == 'athena'
    assert after['source'] == 'Athena'
    changed = {field for field in before if before[field] != after[field]}
    assert changed <= {'target_concept_id', 'destination_vocabulary_id', 'domain_id', 'omop_table',
                       'origin_system', 'source', 'notes', 'updated_at'}
    assert after['notes'].startswith('Keep this note\nAthena export reconciliation')
    assert 'sha256=' in after['notes']
    writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('UPDATE', 'DELETE', 'INSERT'))]
    assert writes and all(q.startswith('UPDATE "source_code_concept_mapping"') for q in writes)
    with patch(COMMAND + '._download_gdrive_vocabulary') as download:
        assert 'Nothing to reconcile' in run_command(apply=True)[0]
    download.assert_not_called()


@pytest.mark.parametrize('section,changes,outcome', [
    ('source_changes', {'invalid_reason': 'D'}, 'not_found'),
    ('source_changes', {'valid_start_date': '20990101'}, 'not_found'),
    ('edge_changes', {'invalid_reason': 'D'}, 'not_found'),
    ('edge_changes', {'valid_end_date': '20000101'}, 'not_found'),
    ('edge_changes', {'relationship_id': 'Mapped from'}, 'not_found'),
    ('target_changes', {'standard_concept': ''}, 'not_found'),
    ('target_changes', {'valid_end_date': '20000101'}, 'not_found'),
])
def test_invalid_or_reverse_export_evidence_never_applies(old_target, export, section, changes, outcome):
    build, _, _, _ = export
    row = _mapping('A01.0', 'ICD10CM', old_target)
    assert f'{outcome}=1' in run_command(path=build(**{section: changes}), apply=True)[0]
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'


def test_multiple_export_targets_remain_ambiguous_when_one_is_missing_locally(old_target, export):
    build, _, target, edge = export
    _mapping('A01.0', 'ICD10CM', old_target)
    path = build(extra_targets=[target | {'concept_id': 800000, 'concept_code': 'other'}],
                 extra_edges=[edge | {'concept_id_2': 800000}])
    assert 'ambiguous_targets=1' in run_command(path=path, apply=True)[0]


def test_duplicate_edges_to_same_target_do_not_create_ambiguity(old_target, export):
    build, _, _, edge = export
    _mapping('A01.0', 'ICD10CM', old_target)
    assert 'would_change_destination=1' in run_command(path=build(extra_edges=[edge]))[0]


@pytest.mark.parametrize('vocabulary,origin,expected', [
    ('ICD10', 'HT-One', 'would_change_destination'),
    ('ICD10', 'curator', 'not_found'),
    ('ICD10CM', 'HT-One', 'would_change_destination'),
])
def test_only_documented_ht_one_alias_is_used(old_target, export, vocabulary, origin, expected):
    build, _, _, _ = export
    _mapping('A01.0', vocabulary, old_target, origin_system=origin)
    assert f'{expected}=1' in run_command(path=build())[0]


@pytest.mark.parametrize('changes,expected', [
    ({'concept_code': 'wrong'}, 'local_target_conflict'),
    ({'standard_concept': None}, 'invalid_local_target'),
    ({'valid_end_date': date(2000, 1, 1)}, 'invalid_local_target'),
    ({'source': 'HealthKey'}, 'invalid_local_target'),
])
def test_local_destination_conflicts_are_not_silently_overwritten(old_target, athena_target, export, changes, expected):
    build, _, _, _ = export
    row = _mapping('A01.0', 'ICD10CM', old_target)
    Concept.objects.filter(pk=athena_target.pk).update(**changes)
    assert f'{expected}=1' in run_command(path=build(), apply=True)[0]
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


def test_local_domain_conflict_is_reported(old_target, athena_target, export):
    build, _, _, _ = export
    _mapping('A01.0', 'ICD10CM', old_target)
    observation = DomainFactory(domain_id='Observation')
    Concept.objects.filter(pk=athena_target.pk).update(domain=observation)
    out, _ = run_command(path=build(), report='-')
    receipt, = list(csv.DictReader(StringIO(out)))
    assert receipt['outcome'] == 'local_target_conflict'
    assert receipt['reason'].endswith('domain_id')


def test_missing_local_destination_is_not_created(old_target, athena_target, export):
    build, _, _, _ = export
    _mapping('A01.0', 'ICD10CM', old_target)
    Concept.objects.filter(pk=athena_target.pk).delete()
    assert 'missing_local_target=1' in run_command(path=build(), apply=True)[0]
    assert not Concept.objects.filter(pk=athena_target.pk).exists()


def test_locked_and_ineligible_rows_are_preserved(old_target, export):
    from patient_portal.models import Identity
    build, _, _, _ = export
    user = Identity.objects.create(issuer='local', sub='export-lock', uid='export-lock')
    row = _mapping('A01.0', 'ICD10CM', old_target)
    Mapping.objects.filter(pk=row.pk).update(locked_by=user)
    _mapping('B', 'ICD10CM', old_target, status='proposed')
    _mapping('C', 'ICD10CM', old_target, origin_system='athena')
    out, err = run_command(path=build(), apply=True, report='-')
    receipt, = list(csv.DictReader(StringIO(out)))
    assert receipt['outcome'] == 'locked'
    assert 'Approved non-Athena mappings: 1' in err


@pytest.mark.parametrize('change', [{'notes': 'A curator edited this'}, {'status': 'rejected'}, {'origin_system': 'athena'}])
def test_edits_during_export_read_are_preserved(old_target, export, change):
    build, _, _, _ = export
    row = _mapping('A01.0', 'ICD10CM', old_target)
    original = service.reconcile_batch

    def edit_then_apply(*args, **kwargs):
        Mapping.objects.filter(pk=row.pk).update(**change)
        return original(*args, **kwargs)

    with patch(COMMAND + '.reconcile_batch', side_effect=edit_then_apply):
        assert 'changed_during_audit=1' in run_command(path=build(), apply=True)[0]
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


def test_destination_is_rechecked_after_export_read(old_target, athena_target, export):
    build, _, _, _ = export
    _mapping('A01.0', 'ICD10CM', old_target)
    original = service.reconcile_batch

    def retire_then_apply(*args, **kwargs):
        Concept.objects.filter(pk=athena_target.pk).update(invalid_reason='D')
        return original(*args, **kwargs)

    with patch(COMMAND + '.reconcile_batch', side_effect=retire_then_apply):
        assert 'invalid_local_target=1' in run_command(path=build(), apply=True)[0]


def test_malformed_or_missing_export_fails_before_apply(old_target, export, tmp_path):
    build, _, _, _ = export
    _mapping('A01.0', 'ICD10CM', old_target)
    build()
    (tmp_path / 'CONCEPT_RELATIONSHIP.csv').write_text('wrong_header\nbroken\n')
    with patch(COMMAND + '.reconcile_batch') as apply:
        with pytest.raises(CommandError, match='Cannot read complete Athena export'):
            run_command(path=str(tmp_path), apply=True)
    apply.assert_not_called()


def test_zip_and_default_gdrive_use_original_archive(old_target, export, tmp_path):
    build, _, _, _ = export
    build()
    _mapping('A01.0', 'ICD10CM', old_target)
    archive = tmp_path / 'athena.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        for filename in ('CONCEPT.csv', 'CONCEPT_RELATIONSHIP.csv'):
            bundle.write(tmp_path / filename, 'vocabulary/' + filename)
    assert 'would_change_destination=1' in run_command(archive=str(archive))[0]
    def noisy_download(*args):
        print('Processing file from Google Drive')
        return archive

    with patch(COMMAND + '._download_gdrive_vocabulary', side_effect=noisy_download) as download:
        out, err = run_command(report='-')
    assert 'Processing file from Google Drive' in err
    assert '1HoRWGepqcH3pMKK03KNb1oWpaVs0Avl7' in download.call_args.args[0]
    receipt, = list(csv.DictReader(StringIO(out)))
    assert receipt['outcome'] == 'would_change_destination'
    assert 'Athena ZIP sha256=' in receipt['reference']
    assert not download.call_args.args[1].exists()  # Owned temporary download directory cleaned up.


def test_bad_report_path_prevents_download_and_changes(old_target, tmp_path):
    _mapping('A01.0', 'ICD10CM', old_target)
    with patch(COMMAND + '._download_gdrive_vocabulary') as download:
        with pytest.raises(CommandError, match='Cannot open report'):
            run_command(apply=True, report=str(tmp_path / 'missing' / 'audit.csv'))
    download.assert_not_called()


def test_more_than_one_batch_does_not_skip_rows_as_they_become_athena(old_target, export, tmp_path):
    build, source, target, edge = export
    build()
    sources, edges, mappings = [], [], []
    for index in range(251):
        code, source_id = f'TEST-{index}', 100000 + index
        sources.append(source | {'concept_id': source_id, 'concept_code': code})
        edges.append(edge | {'concept_id_1': source_id})
        mappings.append(Mapping(source_code=code, source_vocabulary_id='ICD10CM',
                                target_concept=old_target, status='approved', origin_system='HT-One'))
    Mapping.objects.bulk_create(mappings)
    write_tsv(tmp_path, 'CONCEPT.csv', CONCEPT_FIELDS, [*sources, target])
    write_tsv(tmp_path, 'CONCEPT_RELATIONSHIP.csv', REL_FIELDS, edges)
    assert 'destination_changed=251' in run_command(path=str(tmp_path), apply=True)[0]
    assert Mapping.objects.filter(pk__in=[r.pk for r in mappings], origin_system='athena').count() == 251


def test_local_curator_relationship_does_not_override_original_export(old_target, athena_target, export):
    from omop_core.models import ConceptRelationship, Relationship
    from tests.factories import ConceptFactory, VocabularyFactory
    build, _, _, _ = export
    source = ConceptFactory(concept_id=123456, concept_code='A01.0', standard_concept='',
                            vocabulary=VocabularyFactory(vocabulary_id='ICD10CM'))
    relationship = Relationship.objects.create(relationship_id='Maps to', relationship_name='Maps to',
        is_hierarchical=0, defines_ancestry=0, reverse_relationship_id='Mapped from', relationship_concept_id=0)
    ConceptRelationship.objects.create(concept_1=source, concept_2=old_target, relationship=relationship,
                                      valid_start_date='2000-01-01', valid_end_date='2099-12-31')
    row = _mapping('A01.0', 'ICD10CM', old_target)
    out, _ = run_command(path=build(), report='-')
    receipt, = list(csv.DictReader(StringIO(out)))
    assert receipt['outcome'] == 'would_change_destination'
    assert receipt['proposed_target_id'] == str(athena_target.pk)
    # When the export lacks evidence, the local mirrored edge still cannot prove Athena provenance.
    assert 'not_found=1' in run_command(path=build(edge_changes={'invalid_reason': 'D'}), apply=True)[0]
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'
