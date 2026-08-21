"""The descriptor that tells a client how to write a clinical fact.

PatientRecord has no writable clinical columns, so an editor has to write the OMOP
fact instead. These pin the two properties that make that possible: a mapped field
carries everything needed to build a complete Measurement, and an unmapped one says
so out loud rather than going missing.
"""
import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection

from omop_core.services.mappings import CONCEPT_LAB_TYPE, LAB_FIELD_TO_LOINC
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _load_loinc(code, concept_id=None):
    VocabularyFactory(vocabulary_id='LOINC')
    return ConceptFactory(
        concept_code=code, vocabulary_id='LOINC',
        concept_name=f'Concept {code}',
        **({'concept_id': concept_id} if concept_id else {}),
    )


def _load_ucum(unit):
    VocabularyFactory(vocabulary_id='UCUM', vocabulary_name='UCUM')
    return ConceptFactory(
        concept_code=unit, vocabulary_id='UCUM', concept_name=unit,
    )


class TestMappedFields:
    def test_a_loaded_mapping_is_writable_with_a_full_fact_recipe(self):
        concept = _load_loinc('718-7')
        unit = _load_ucum('g/dL')

        entry = build_writable_field_descriptor()['hemoglobin_g_dl']

        assert entry['writable'] is True
        assert entry['target'] == 'measurement'
        assert entry['concept_id'] == concept.concept_id
        assert entry['code'] == '718-7'
        assert entry['vocabulary'] == 'LOINC'
        assert entry['value_kind'] == 'number'
        assert entry['unit'] == 'g/dL'
        assert entry['unit_concept_id'] == unit.concept_id
        assert entry['type_concept_id'] == CONCEPT_LAB_TYPE
        assert entry['source_value'] == '718-7'

    def test_every_key_a_measurement_write_needs_is_present(self):
        _load_loinc('718-7')
        entry = build_writable_field_descriptor()['hemoglobin_g_dl']
        required = {
            'target', 'concept_id', 'value_kind', 'type_concept_id', 'source_value',
        }
        assert required <= set(entry)

    def test_missing_unit_concept_does_not_make_the_field_unwritable(self):
        """UCUM may be absent; the fact is still writable with a source unit string."""
        _load_loinc('718-7')

        entry = build_writable_field_descriptor()['hemoglobin_g_dl']

        assert entry['writable'] is True
        assert entry['unit'] == 'g/dL'
        assert entry['unit_concept_id'] is None


class TestUnmappedFields:
    def test_a_field_with_no_concept_set_is_reported_not_omitted(self):
        """A client must be able to tell 'may not edit' from 'was not sent'."""
        descriptor = build_writable_field_descriptor()

        assert 'planned_therapies' in descriptor
        assert descriptor['planned_therapies']['writable'] is False
        assert 'reason' in descriptor['planned_therapies']

    def test_a_mapped_code_absent_from_the_vocabulary_is_not_writable(self):
        """Better to refuse here than to strand a fact against an unresolvable concept."""
        descriptor = build_writable_field_descriptor()

        entry = descriptor['hemoglobin_g_dl']
        assert entry['writable'] is False
        assert '718-7' in entry['reason']
        assert entry['code'] == '718-7'

    def test_every_mapped_projection_field_appears(self):
        descriptor = build_writable_field_descriptor()
        lifecycle = {
            'id', 'person', 'organization', 'created_at', 'updated_at',
            'derived_at', 'derivation_version', 'user_edited_fields',
        }
        assert set(descriptor) == PATIENT_RECORD_OMOP_MAPPED_FIELDS - lifecycle

    def test_no_lifecycle_column_is_offered(self):
        descriptor = build_writable_field_descriptor()
        for field in ('created_at', 'derivation_version', 'user_edited_fields'):
            assert field not in descriptor


class TestCost:
    def test_query_count_is_flat_not_per_field(self):
        """One lookup per vocabulary, however many fields are mapped."""
        for code, _unit, _display in LAB_FIELD_TO_LOINC.values():
            _load_loinc(code)

        with CaptureQueriesContext(connection) as ctx:
            build_writable_field_descriptor()

        assert len(ctx) <= 2, [q['sql'][:80] for q in ctx]


class TestEndpoint:
    def test_requires_authentication(self, client):
        resp = client.get('/api/v1/patient-records/writable-fields/')
        assert resp.status_code in (401, 403)
