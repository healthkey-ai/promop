"""Migration-backed aliases honor the approved FCM destination and curator edits."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection

from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping
from omop_core.mapping.code_resolution import resolve_source_code
from omop_core.services.patient_record_service import _LOINC_LAB_FIELDS
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

seed = importlib.import_module('omop_core.migrations.0285_approve_lab_extractor_aliases')


def test_alias_seed_covers_every_real_approved_extractor_alias():
    fixture = json.loads((Path(__file__).parents[1] / 'omop_core' / 'data'
                          / 'field_curation_v1.json').read_text())
    approved = {row['field_name']: row['concept_code'] for row in fixture['mappings']
                if row['status'] == 'approved' and row['vocabulary_id'] == 'LOINC'}
    expected = {code for code, (field, _) in _LOINC_LAB_FIELDS.items()
                if field in approved and code != approved[field]
                and code not in {'33944-8', '33945-5'}}
    assert set(seed.ALIASES) == expected


def _seed():
    seed.approve_aliases(apps, SimpleNamespace(connection=connection))


def test_seeds_approved_alias_before_athena_and_is_idempotent():
    FieldConceptMapping.objects.create(
        field_name='monoclonal_protein_serum', status='approved',
        vocabulary_id='LOINC', concept_code='33358-3', omop_table='measurement',
    )
    _seed()
    _seed()
    row = SourceCodeConceptMapping.objects.get(
        organization=None, source_vocabulary_id='LOINC', source_code='51435-6',
    )
    assert row.status == 'approved'
    assert row.target_concept_id == 3046299
    assert row.origin_system == 'lab-extractor-alias-v1'


def test_does_not_override_curated_source_mapping_or_proposed_field():
    FieldConceptMapping.objects.create(
        field_name='monoclonal_protein_serum', status='approved',
        vocabulary_id='LOINC', concept_code='33358-3', omop_table='measurement',
    )
    FieldConceptMapping.objects.create(
        field_name='kappa_flc', status='proposed', vocabulary_id='LOINC',
        concept_code='104544-2', omop_table='measurement',
    )
    curated = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='51435-6', status='rejected',
    )
    _seed()
    curated.refresh_from_db()
    assert curated.status == 'rejected'
    assert not SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id='LOINC', source_code='36916-5',
    ).exists()


def test_replaces_only_machine_generated_athena_identity():
    FieldConceptMapping.objects.create(
        field_name='monoclonal_protein_serum', status='approved',
        vocabulary_id='LOINC', concept_code='33358-3', omop_table='measurement',
    )
    source = ConceptFactory(
        concept_code='51435-6', vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    identity = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='51435-6',
        source_concept=source, target_concept=source, status='approved',
        origin='import', origin_system='athena-direct', source='Athena',
    )
    _seed()
    identity.refresh_from_db()
    assert identity.target_concept_id == 3046299
    assert identity.source_concept_id == source.concept_id
    assert identity.origin_system == 'lab-extractor-alias-v1'


def test_import_resolver_uses_auto_approved_alias_after_athena_load():
    target = ConceptFactory(
        concept_id=3046299, concept_code='33358-3',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    FieldConceptMapping.objects.create(
        field_name='monoclonal_protein_serum', concept=target,
        status='approved', vocabulary_id='LOINC', concept_code='33358-3',
        omop_table='measurement',
    )
    _seed()

    resolved, mapping = resolve_source_code(
        source_code='51435-6', source_vocabulary_id='LOINC',
        omop_table='measurement',
    )
    assert resolved == target
    assert mapping.status == 'approved'
