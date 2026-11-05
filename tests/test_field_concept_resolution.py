"""Seeded field mappings resolve their Concept once the vocabulary is loaded.

On a fresh instance migration 0240 seeds ``FieldConceptMapping`` before
``load_athena_vocabularies`` runs, so every row lands with ``concept = NULL``
and only its ``(vocabulary_id, concept_code)`` text. These tests build that
state the way 0240 does (``apply_payload`` with no Concept loaded), load the
Concept afterwards, and check that the resolver, the loader's completion hook
and the management command fill the FK in, that an unknown code is counted
and left alone, that a second run changes nothing, and that lab derivation
(``resolve_lab_code_fields``) picks the row up once it is resolved.
"""
import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from omop_core.management.commands.load_athena_vocabularies import Command as AthenaCommand
from omop_core.models import Concept, FieldConceptMapping, SourceCodeConceptMapping
from omop_core.services.field_concept_resolution import (
    ResolutionResult,
    fixture_concept_refs,
    resolve_field_concept_mappings,
    unresolved_mappings,
)
from omop_core.services.field_curation_transfer import apply_payload
from omop_core.services.patient_record_service import (
    _LOINC_LAB_FIELDS, resolve_lab_code_fields,
)
from tests.conftest import FIELD_CURATION_FIXTURE
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _fixture_rows(*field_names):
    data = json.loads(FIELD_CURATION_FIXTURE.read_text())
    rows = [row for row in data['mappings'] if row['field_name'] in field_names]
    assert len(rows) == len(field_names), 'every requested field is in the fixture'
    return rows


def _seed_without_concepts(*field_names, coded=True, concept_loaded=False):
    """Seed fixture rows as 0240 does on a fresh instance: no Concept loaded.

    ``coded`` says whether the rows carry their concept in the text columns;
    the SCT rows do not (they name it only through the fixture's resolved
    pair). ``concept_loaded`` admits a row whose concept the test database
    already holds, which the caller then clears.
    """
    rows = _fixture_rows(*field_names)
    stats = apply_payload({'mappings': rows}, tables=('mappings',))
    assert stats.total(stats.created) == len(rows)
    seeded = FieldConceptMapping.objects.filter(field_name__in=field_names)
    assert concept_loaded or all(m.concept_id is None for m in seeded)
    assert all(bool(m.vocabulary_id and m.concept_code) is coded for m in seeded)
    return {row['field_name']: row for row in rows}


def _load_concepts(rows):
    """What load_athena_vocabularies leaves behind for these rows' codes."""
    vocabularies = {}
    for row in rows.values():
        vocabulary = vocabularies.setdefault(
            row['vocabulary_id'], VocabularyFactory(vocabulary_id=row['vocabulary_id']),
        )
        ConceptFactory(
            concept_id=row['concept_id'], concept_code=row['concept_code'],
            vocabulary=vocabulary, concept_name=f"{row['vocabulary_id']} {row['concept_code']}",
        )


# ── The resolver ───────────────────────────────────────────────────────────


def test_seeded_row_resolves_once_its_concept_is_loaded():
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts(rows)
    seeded_at = dict(FieldConceptMapping.objects.values_list('field_name', 'updated_at'))

    result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved, result.unresolved_refs) == (2, 0, [])
    for field_name, row in rows.items():
        mapping = FieldConceptMapping.objects.get(field_name=field_name)
        assert mapping.concept_id == row['concept_id']
        # Only the pointer changed: text, status, provenance and updated_at
        # are as seeded.
        assert (mapping.vocabulary_id, mapping.concept_code, mapping.status) == (
            row['vocabulary_id'], row['concept_code'], row['status'],
        )
        assert mapping.updated_at == seeded_at[field_name]
    assert not unresolved_mappings().exists()


def test_uncoded_row_resolves_through_the_fixture_pair():
    """The SCT rows carry no code (keyed by source_value); the pair the
    fixture resolved them to is what names their concept. On a fresh
    instance migration 0156 skips them (type concept 32817 is not loaded
    yet) and 0240 seeds them with no concept. The test database may already
    hold 32817 from another test, in which case apply_payload resolves it at
    seed time, so the fresh-instance state is forced."""
    rows = _seed_without_concepts('sct_date', 'sct_eligibility', coded=False, concept_loaded=True)
    FieldConceptMapping.objects.filter(field_name__in=rows).update(concept=None)
    assert fixture_concept_refs()['sct_date'] == ('Type Concept', 'OMOP4976890')
    ConceptFactory(
        concept_id=32817, concept_code='OMOP4976890',
        vocabulary=VocabularyFactory(vocabulary_id='Type Concept'), concept_name='EHR',
    )

    result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved) == (2, 0)
    for field_name, row in rows.items():
        mapping = FieldConceptMapping.objects.get(field_name=field_name)
        assert mapping.concept_id == 32817
        assert (mapping.vocabulary_id, mapping.concept_code) == ('', '')
        assert mapping.source_value == row['source_value']


def test_uncoded_row_is_reported_under_the_fixture_pair_until_it_loads():
    rows = _seed_without_concepts('sct_date', coded=False, concept_loaded=True)
    FieldConceptMapping.objects.filter(field_name__in=rows).update(concept=None)

    with patch(
        'omop_core.services.field_concept_resolution.fixture_concept_refs',
        return_value={'sct_date': ('Type Concept', 'NOT-LOADED-HERE')},
    ):
        result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved) == (0, 1)
    assert result.unresolved_refs == [('sct_date', 'Type Concept', 'NOT-LOADED-HERE')]
    assert FieldConceptMapping.objects.get(field_name='sct_date').concept_id is None


def test_resolution_saves_through_the_projection_receiver(settings):
    """A resolved approved row is saved as the editor saves it, so the
    post_save projection backfills pending user edits; a proposed row is not
    projected, as in the editor."""
    settings.CELERY_BROKER_URL = ''
    rows = _seed_without_concepts('hemoglobin_g_dl', 'creatinine_clearance_ml_min')
    FieldConceptMapping.objects.filter(field_name='creatinine_clearance_ml_min').update(
        status='proposed',
    )
    _load_concepts(rows)
    approved = FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl')

    with patch('omop_core.signals._project_field_sync') as project:
        result = resolve_field_concept_mappings()

    assert result.resolved == 2
    project.assert_called_once_with(approved.pk)


def test_unknown_code_stays_null_and_is_counted():
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts({'hemoglobin_g_dl': rows['hemoglobin_g_dl']})

    result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved) == (1, 1)
    assert result.unresolved_refs == [('serum_creatinine_mg_dl', 'LOINC', '2160-0')]
    assert 'serum_creatinine_mg_dl (LOINC:2160-0)' in result.summary()
    unresolved = FieldConceptMapping.objects.get(field_name='serum_creatinine_mg_dl')
    assert unresolved.concept_id is None
    assert (unresolved.vocabulary_id, unresolved.concept_code) == ('LOINC', '2160-0')
    assert list(unresolved_mappings().values_list('field_name', flat=True)) == [
        'serum_creatinine_mg_dl',
    ]


def test_resolution_is_idempotent():
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts({'hemoglobin_g_dl': rows['hemoglobin_g_dl']})
    first = resolve_field_concept_mappings()
    before = list(FieldConceptMapping.objects.order_by('pk').values(
        'pk', 'concept_id', 'vocabulary_id', 'concept_code', 'status', 'updated_at',
    ))

    second = resolve_field_concept_mappings()

    assert (first.resolved, second.resolved) == (1, 0)
    assert (first.unresolved, second.unresolved) == (1, 1)
    assert list(FieldConceptMapping.objects.order_by('pk').values(
        'pk', 'concept_id', 'vocabulary_id', 'concept_code', 'status', 'updated_at',
    )) == before


def test_resolution_never_overwrites_a_curated_concept():
    """A row that already points at a Concept is not a candidate, whatever its text says."""
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)
    curated = ConceptFactory(
        concept_id=9_718_007, concept_code='718-7-CURATED',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    FieldConceptMapping.objects.filter(field_name='hemoglobin_g_dl').update(concept=curated)

    result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved) == (0, 0)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id == curated.pk


def test_rows_without_a_code_are_not_candidates():
    """The fixture's recipe-only rows (no vocabulary, no code) name no concept."""
    FieldConceptMapping.objects.create(field_name='recipe_only', status='approved')
    FieldConceptMapping.objects.create(
        field_name='vocabulary_only', vocabulary_id='LOINC', status='approved',
    )

    result = resolve_field_concept_mappings()

    assert result.total == 0
    assert result.summary().startswith('field mappings: every row')
    assert FieldConceptMapping.objects.filter(concept__isnull=True).count() == 2


def test_resolution_batches_its_queries():
    """Concepts are looked up per batch, not per row: one Concept query per
    batch, then one locking read and one UPDATE per row saved."""
    vocabulary = VocabularyFactory(vocabulary_id='LOINC')
    for n in range(6):
        FieldConceptMapping.objects.create(
            field_name=f'batched_{n}', vocabulary_id='LOINC', concept_code=f'B-{n}',
        )
        ConceptFactory(concept_id=9_800_000 + n, concept_code=f'B-{n}', vocabulary=vocabulary)

    with CaptureQueriesContext(connection) as queries:
        result = resolve_field_concept_mappings(batch_size=3)

    assert result.resolved == 6
    # 1 candidate select + 1 concept select per batch, then 1 locking select
    # and 1 UPDATE per row; savepoints are neither.
    selects = [q for q in queries.captured_queries if q['sql'].startswith('SELECT')]
    updates = [q for q in queries.captured_queries if q['sql'].startswith('UPDATE')]
    assert len(selects) == 1 + 2 + 6
    assert len(updates) == 6
    assert sum('FOR UPDATE' in q['sql'] for q in selects) == 6


def test_dry_run_counts_without_locking_saving_or_projecting(settings):
    settings.CELERY_BROKER_URL = ''
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts({'hemoglobin_g_dl': rows['hemoglobin_g_dl']})

    with patch('omop_core.signals._project_field_sync') as project, \
            CaptureQueriesContext(connection) as queries:
        result = resolve_field_concept_mappings(dry_run=True)

    assert (result.resolved, result.unresolved) == (1, 1)
    assert result.unresolved_refs == [('serum_creatinine_mg_dl', 'LOINC', '2160-0')]
    project.assert_not_called()
    assert not any('FOR UPDATE' in q['sql'] or q['sql'].startswith('UPDATE')
                   for q in queries.captured_queries)
    assert FieldConceptMapping.objects.filter(concept__isnull=True).count() == 2


def test_retired_concept_resolves_and_is_named_for_recuration():
    """The pair is what the fixture names, so it is honoured as apply_payload
    would; the log says which rows need a curator."""
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)
    ConceptFactory(
        concept_id=rows['hemoglobin_g_dl']['concept_id'], concept_code='718-7',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    Concept.objects.filter(concept_id=rows['hemoglobin_g_dl']['concept_id']).update(
        invalid_reason='U',
    )

    result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved) == (1, 0)
    assert result.retired_refs == [('hemoglobin_g_dl', 'LOINC', '718-7')]
    assert '1 of the resolved point at a retired concept, re-curate: hemoglobin_g_dl (LOINC:718-7).' \
        in result.summary()
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id \
        == rows['hemoglobin_g_dl']['concept_id']


def test_summary_names_the_unresolved_rows_up_to_a_cap():
    result = ResolutionResult(
        resolved=1, unresolved=25,
        unresolved_refs=[(f'field_{n}', 'LOINC', f'C-{n}') for n in range(25)],
    )

    text = result.summary()

    assert text.startswith('field mappings: 1 row(s) now point at their Concept, 25 still unresolved: ')
    assert 'field_19 (LOINC:C-19)' in text
    assert 'field_20' not in text
    assert text.endswith(', and 5 more.')


# ── Lab derivation reads the resolved row ──────────────────────────────────


def test_lab_routing_picks_up_the_row_once_resolved():
    """Before resolution the field is on the extractor table; after it the
    mapping routes the field, and an approved LOINC alias on its concept
    routes too."""
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)
    hemoglobin = rows['hemoglobin_g_dl']
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='HGB-ALIAS', omop_table='measurement',
        target_concept_id=hemoglobin['concept_id'], status='approved',
    )
    table_cast = _LOINC_LAB_FIELDS['718-7'][1]

    before = resolve_lab_code_fields()
    assert before['718-7'] == ('hemoglobin_g_dl', table_cast)
    assert 'HGB-ALIAS' not in before

    resolve_field_concept_mappings()

    after = resolve_lab_code_fields()
    assert after['718-7'] == ('hemoglobin_g_dl', table_cast)
    assert after['HGB-ALIAS'] == ('hemoglobin_g_dl', table_cast)


# ── The loader's completion hook ───────────────────────────────────────────


_LOAD_STEPS = (
    '_load_relationships', '_load_vocabularies', '_load_domains',
    '_load_concept_classes', '_load_concepts', '_load_concept_relationships',
    '_load_concept_ancestors', '_load_concept_synonym', '_load_drug_strength',
    '_load_source_to_concept_map',
)
_POST_LOAD_STEPS = (
    '_seed_concept_zero', '_verify_migration_bootstrap_concepts',
    '_remove_stale_concepts', '_refresh_suggest_synonym_terms',
    '_sync_cdm_source_metadata', '_verify_required_clinical_vocabularies',
    '_load_raw_umls', '_record_version_history', '_load_code_mappings',
    '_load_loinc_axes', '_publish_release',
)


def _run_loader(tmp_path, stubs=None, **overrides):
    """Run the loader's handle with every table load stubbed out, so what is
    left is its control flow: which completion steps run on which path, in
    which order. ``stubs`` replaces individual step stubs; ``overrides`` are
    handle options. Returns the output and the order the steps ran in."""
    options = dict(
        path=str(tmp_path), archive=None, bucket=None, gdrive=None, lfs=None,
        replace=False, dry_run=False, skip_clinical_vocabulary_verification=True,
        concepts_only=False, skip_code_mappings=True, umls_release_url=None,
        skip_umls_cache=True, migration_bootstrap=False, loinc_archive=None,
        verbosity=1,
    )
    options.update(overrides)
    steps = {name: MagicMock(return_value=0) for name in _LOAD_STEPS}
    steps.update({name: MagicMock(return_value=None) for name in _POST_LOAD_STEPS})
    steps.update(stubs or {})
    output = StringIO()
    command = AthenaCommand(stdout=output)
    resolver = AthenaCommand._resolve_field_concept_mappings
    steps['_resolve_field_concept_mappings'] = MagicMock(side_effect=lambda: resolver(command))
    calls = MagicMock()
    for name, step in steps.items():
        calls.attach_mock(step, name)
    with patch.multiple(AthenaCommand, **steps):
        command.handle(**options)
    return output.getvalue(), [name for name, _, _ in calls.mock_calls]


def test_loader_resolves_field_mappings_after_a_successful_load(tmp_path):
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts({'hemoglobin_g_dl': rows['hemoglobin_g_dl']})

    output, order = _run_loader(tmp_path)

    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id \
        == rows['hemoglobin_g_dl']['concept_id']
    assert FieldConceptMapping.objects.get(field_name='serum_creatinine_mg_dl').concept_id is None
    assert '1 row(s) now point at their Concept, 1 still unresolved' in output
    assert 'serum_creatinine_mg_dl (LOINC:2160-0)' in output
    # After every table load and the release publication, once.
    assert order.count('_resolve_field_concept_mappings') == 1
    assert order[-1] == '_resolve_field_concept_mappings'
    assert order.index('_publish_release') < order.index('_resolve_field_concept_mappings')


def test_loader_resolves_on_concepts_only_but_not_dry_run_or_bootstrap(tmp_path):
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)

    _, order = _run_loader(tmp_path, dry_run=True)
    assert '_resolve_field_concept_mappings' not in order
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None

    _, order = _run_loader(tmp_path, migration_bootstrap=True)
    assert '_resolve_field_concept_mappings' not in order
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None

    _, order = _run_loader(tmp_path, concepts_only=True)
    assert '_load_concept_relationships' not in order
    assert order[-1] == '_resolve_field_concept_mappings'
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id \
        == rows['hemoglobin_g_dl']['concept_id']


def test_loader_does_not_resolve_after_a_failed_load(tmp_path):
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)

    with pytest.raises(RuntimeError, match='load failed'):
        _run_loader(tmp_path, stubs={
            '_load_concepts': MagicMock(side_effect=RuntimeError('load failed')),
        })

    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None


def test_loader_reports_but_survives_a_failed_resolution(tmp_path, caplog):
    """The load is complete and published by then; under sync_athena_vocabulary
    an exception here would roll the whole vocabulary back."""
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)

    with patch(
        'omop_core.services.field_concept_resolution.resolve_field_concept_mappings',
        side_effect=RuntimeError('lock timeout'),
    ):
        output, order = _run_loader(tmp_path)

    assert order[-1] == '_resolve_field_concept_mappings'
    assert 'WARNING: field mappings were not resolved; run manage.py resolve_field_concept_mappings.' in output
    assert 'resolution failed after the load' in caplog.text
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None


def test_loader_confines_a_database_error_to_a_savepoint(tmp_path):
    """Under sync_athena_vocabulary the loader runs inside one transaction;
    a database error in the hook must not leave it aborted, or the sync's
    next statement would roll the whole vocabulary back."""
    from django.db import DatabaseError

    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)

    def broken(*args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1/0')

    with transaction.atomic():
        with patch(
            'omop_core.services.field_concept_resolution.resolve_field_concept_mappings',
            side_effect=broken,
        ):
            output, _ = _run_loader(tmp_path)
        # The outer transaction is still usable: the next statement runs.
        assert FieldConceptMapping.objects.filter(field_name='hemoglobin_g_dl').exists()

    assert 'WARNING: field mappings were not resolved' in output
    with pytest.raises(DatabaseError):
        broken()


# ── The management command ─────────────────────────────────────────────────


def test_command_resolves_and_reports():
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts({'hemoglobin_g_dl': rows['hemoglobin_g_dl']})
    output = StringIO()

    call_command('resolve_field_concept_mappings', stdout=output)

    text = output.getvalue()
    assert 'Resolved 1 field mapping concept(s); 1 still unresolved.' in text
    assert 'serum_creatinine_mg_dl (LOINC:2160-0)' in text
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id \
        == rows['hemoglobin_g_dl']['concept_id']


def test_command_dry_run_writes_nothing():
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)
    output = StringIO()

    call_command('resolve_field_concept_mappings', dry_run=True, stdout=output)

    assert 'Dry run, nothing written. Would resolve 1 row(s).' in output.getvalue()
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None
