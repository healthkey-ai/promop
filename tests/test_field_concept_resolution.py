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
from django.db import connection
from django.test.utils import CaptureQueriesContext

from omop_core.management.commands.load_athena_vocabularies import Command as AthenaCommand
from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping
from omop_core.services.field_concept_resolution import (
    ResolutionResult,
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


def _seed_without_concepts(*field_names):
    """Seed fixture rows as 0240 does on a fresh instance: no Concept loaded."""
    rows = _fixture_rows(*field_names)
    stats = apply_payload({'mappings': rows}, tables=('mappings',))
    assert stats.total(stats.created) == len(rows)
    seeded = FieldConceptMapping.objects.filter(field_name__in=field_names)
    assert all(m.concept_id is None for m in seeded)
    assert all(m.vocabulary_id and m.concept_code for m in seeded)
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

    result = resolve_field_concept_mappings()

    assert (result.resolved, result.unresolved, result.unresolved_refs) == (2, 0, [])
    for field_name, row in rows.items():
        mapping = FieldConceptMapping.objects.get(field_name=field_name)
        assert mapping.concept_id == row['concept_id']
        # Only the pointer changed: text, status and provenance are as seeded.
        assert (mapping.vocabulary_id, mapping.concept_code, mapping.status) == (
            row['vocabulary_id'], row['concept_code'], row['status'],
        )
    assert not unresolved_mappings().exists()


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
    assert not unresolved_mappings().exists()


def test_resolution_batches_its_queries():
    """Rows are looked up per batch, not per row: one Concept query and one
    UPDATE per distinct concept in each batch."""
    vocabulary = VocabularyFactory(vocabulary_id='LOINC')
    for n in range(6):
        FieldConceptMapping.objects.create(
            field_name=f'batched_{n}', vocabulary_id='LOINC', concept_code=f'B-{n}',
        )
        ConceptFactory(concept_id=9_800_000 + n, concept_code=f'B-{n}', vocabulary=vocabulary)

    with CaptureQueriesContext(connection) as queries:
        result = resolve_field_concept_mappings(batch_size=3)

    assert result.resolved == 6
    # 1 candidate select + per batch (1 concept select + 3 updates + savepoint pair).
    selects = [q for q in queries.captured_queries if q['sql'].startswith('SELECT')]
    updates = [q for q in queries.captured_queries if q['sql'].startswith('UPDATE')]
    assert len(selects) == 3
    assert len(updates) == 6


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
    left is its control flow: which completion steps run on which path.
    ``stubs`` replaces individual step stubs; ``overrides`` are handle options."""
    options = dict(
        path=str(tmp_path), archive=None, bucket=None, gdrive=None, lfs=None,
        replace=False, dry_run=False, skip_clinical_vocabulary_verification=True,
        concepts_only=True, skip_code_mappings=True, umls_release_url=None,
        skip_umls_cache=True, migration_bootstrap=False, loinc_archive=None,
        verbosity=1,
    )
    options.update(overrides)
    steps = {name: MagicMock(return_value=0) for name in _LOAD_STEPS}
    steps.update({name: MagicMock(return_value=None) for name in _POST_LOAD_STEPS})
    steps.update(stubs or {})
    output = StringIO()
    with patch.multiple(AthenaCommand, **steps):
        AthenaCommand(stdout=output).handle(**options)
    return output.getvalue()


def test_loader_resolves_field_mappings_after_a_successful_load(tmp_path):
    rows = _seed_without_concepts('hemoglobin_g_dl', 'serum_creatinine_mg_dl')
    _load_concepts({'hemoglobin_g_dl': rows['hemoglobin_g_dl']})

    output = _run_loader(tmp_path)

    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id \
        == rows['hemoglobin_g_dl']['concept_id']
    assert FieldConceptMapping.objects.get(field_name='serum_creatinine_mg_dl').concept_id is None
    assert '1 row(s) now point at their Concept, 1 still unresolved' in output
    assert 'serum_creatinine_mg_dl (LOINC:2160-0)' in output


def test_loader_resolves_on_concepts_only_but_not_dry_run_or_bootstrap(tmp_path):
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)

    _run_loader(tmp_path, dry_run=True)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None

    _run_loader(tmp_path, migration_bootstrap=True)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None

    _run_loader(tmp_path, concepts_only=True)
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


def test_command_dry_run_rolls_back():
    rows = _seed_without_concepts('hemoglobin_g_dl')
    _load_concepts(rows)
    output = StringIO()

    call_command('resolve_field_concept_mappings', dry_run=True, stdout=output)

    assert 'Dry run, rolled back. Would have resolved 1 row(s).' in output.getvalue()
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').concept_id is None
