"""Tests for PatientRecord-first write architecture.

All UI edits write to PatientRecord first.  Fields with approved
FieldConceptMapping rows are projected into OMOP tables; fields without
mappings are preserved in user_edited_fields until a mapping is approved.
"""
import pytest
from datetime import date

from omop_core.models import (
    FieldConceptMapping, Measurement, Observation, PatientRecord,
)
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS, refresh_patient_record,
)
from omop_core.services.write_descriptor import (
    KIND_DIRECT, build_writable_field_descriptor,
)
from omop_core.services.omop_projection import project_single_value
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Write descriptor — KIND_DIRECT for unmapped fields
# ---------------------------------------------------------------------------

class TestDirectKindForUnmappedFields:
    """Fields with no OMOP mapping should be KIND_DIRECT and writable."""

    def test_unmapped_field_is_direct_writable(self):
        descriptor = build_writable_field_descriptor()
        # Pick a field that is in PATIENT_RECORD_OMOP_MAPPED_FIELDS but has no
        # LOINC/SNOMED/curated mapping — it should be KIND_DIRECT.
        entry = descriptor.get('planned_therapies')
        assert entry is not None
        assert entry['kind'] == KIND_DIRECT
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'

    def test_direct_field_carries_value_kind(self):
        descriptor = build_writable_field_descriptor()
        entry = descriptor.get('planned_therapies')
        assert 'value_kind' in entry

    def test_direct_field_carries_field_choice_options(self):
        """FieldChoice options should be attached to direct fields."""
        from omop_core.models import FieldChoice
        FieldChoice.objects.create(field_name='planned_therapies', display='Option A')
        descriptor = build_writable_field_descriptor()
        entry = descriptor.get('planned_therapies')
        assert entry['kind'] == KIND_DIRECT
        assert any(o['value'] == 'Option A' for o in entry.get('options', []))


# ---------------------------------------------------------------------------
# Derivation preserves user-edited values
# ---------------------------------------------------------------------------

class TestDerivationPreservesUserEdits:
    """User-edited values survive derivation when no OMOP fact backs them."""

    def test_user_edit_preserved_across_refresh(self):
        person = PersonFactory()
        record = PatientRecordFactory(person=person, hemoglobin_g_dl=12.5)
        record.user_edited_fields = ['hemoglobin_g_dl']
        record.save(update_fields=['user_edited_fields'])

        # No OMOP measurement for hemoglobin → derivation should preserve it
        refreshed = refresh_patient_record(person)

        assert refreshed.hemoglobin_g_dl == 12.5
        assert 'hemoglobin_g_dl' in refreshed.user_edited_fields

    def test_unprojected_edit_is_not_overwritten_by_a_different_fact(self):
        person = PersonFactory()
        record = PatientRecordFactory(person=person, hemoglobin_g_dl=12.5)
        record.user_edited_fields = ['hemoglobin_g_dl']
        record.save(update_fields=['user_edited_fields'])

        # Create an OMOP measurement that derivation will pick up
        VocabularyFactory(vocabulary_id='LOINC')
        concept = ConceptFactory(
            concept_code='718-7', vocabulary_id='LOINC',
            concept_name='Hemoglobin',
        )
        MeasurementFactory(
            person=person,
            measurement_concept=concept,
            measurement_date=date.today(),
            value_as_number=14.0,
            measurement_source_value='718-7',
        )

        refreshed = refresh_patient_record(person)

        # A different fact does not prove that this pending edit was projected.
        assert refreshed.hemoglobin_g_dl == 12.5
        assert 'hemoglobin_g_dl' in refreshed.user_edited_fields

    def test_empty_user_edit_not_preserved(self):
        """A user-edited field with an empty value is not preserved."""
        person = PersonFactory()
        record = PatientRecordFactory(person=person, hemoglobin_g_dl=None)
        record.user_edited_fields = ['hemoglobin_g_dl']
        record.save(update_fields=['user_edited_fields'])

        refreshed = refresh_patient_record(person)

        assert refreshed.hemoglobin_g_dl is None
        # Empty value is not preserved, so it's cleaned from tracking
        assert 'hemoglobin_g_dl' not in (refreshed.user_edited_fields or [])


# ---------------------------------------------------------------------------
# OMOP projection on mapping approval
# ---------------------------------------------------------------------------

class TestOmopProjection:
    """When a FieldConceptMapping is approved, user-edited values are projected."""

    def test_projection_creates_omop_fact(self):
        from omop_core.services.omop_projection import project_field_to_omop

        person = PersonFactory()
        record = PatientRecordFactory(
            person=person, planned_therapies='some therapy plan',
        )
        record.user_edited_fields = ['planned_therapies']
        record.save(update_fields=['user_edited_fields'])

        VocabularyFactory(vocabulary_id='SNOMED')
        concept = ConceptFactory(
            concept_code='313059006', vocabulary_id='SNOMED',
            concept_name='Planned therapy',
        )
        # Observation type concept
        VocabularyFactory(vocabulary_id='Type Concept')
        type_concept = ConceptFactory(
            concept_id=32817, concept_code='32817', vocabulary_id='Type Concept',
            concept_name='EHR',
        )

        mapping = FieldConceptMapping.objects.create(
            field_name='planned_therapies',
            concept=concept,
            omop_table='observation',
            source_value='planned-therapies',
            value_kind='string',
            status='approved',
        )

        from omop_core.models import Observation
        assert Observation.objects.filter(
            person=person,
            observation_source_value='planned-therapies',
        ).exists()

    def test_projection_skips_when_omop_fact_already_exists(self):
        from omop_core.services.omop_projection import project_field_to_omop
        from omop_core.models import Observation

        person = PersonFactory()
        record = PatientRecordFactory(
            person=person, planned_therapies='some therapy plan',
        )
        record.user_edited_fields = ['planned_therapies']
        record.save(update_fields=['user_edited_fields'])

        VocabularyFactory(vocabulary_id='SNOMED')
        concept = ConceptFactory(
            concept_code='313059006', vocabulary_id='SNOMED',
            concept_name='Planned therapy',
        )
        VocabularyFactory(vocabulary_id='Type Concept')
        ConceptFactory(
            concept_id=32817, concept_code='32817', vocabulary_id='Type Concept',
            concept_name='EHR',
        )

        mapping = FieldConceptMapping.objects.create(
            field_name='planned_therapies',
            concept=concept,
            omop_table='observation',
            source_value='planned-therapies',
            value_kind='string',
            status='approved',
        )

        # First projection creates the fact
        count_after_first = Observation.objects.filter(
            person=person,
            observation_source_value='planned-therapies',
        ).count()

        # Run again — should not duplicate
        count = project_field_to_omop(mapping)
        count_after_second = Observation.objects.filter(
            person=person,
            observation_source_value='planned-therapies',
        ).count()

        assert count == 0  # already existed
        assert count_after_second == count_after_first


# ---------------------------------------------------------------------------
# Write descriptor — mapped fields carry projection metadata
# ---------------------------------------------------------------------------

class TestMappedFieldsCarryProjection:
    """Fields with LOINC/SNOMED mappings should be KIND_DIRECT with projection."""

    def test_loinc_mapped_field_has_projection(self):
        VocabularyFactory(vocabulary_id='LOINC')
        VocabularyFactory(vocabulary_id='UCUM')
        ConceptFactory(concept_code='718-7', vocabulary_id='LOINC',
                       concept_name='Hemoglobin')

        descriptor = build_writable_field_descriptor()
        entry = descriptor.get('hemoglobin_g_dl')

        assert entry is not None
        assert entry['kind'] == KIND_DIRECT
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert 'projection' in entry
        assert entry['projection']['omop_table'] == 'measurement'
        assert entry['projection']['concept_id'] is not None
        assert entry['projection']['source_value'] == '718-7'

    def test_loinc_mapped_field_without_concept_is_still_writable(self):
        """Even without the vocabulary loaded, the field is directly writable."""
        descriptor = build_writable_field_descriptor()
        entry = descriptor.get('hemoglobin_g_dl')

        assert entry is not None
        assert entry['kind'] == KIND_DIRECT
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        # No projection since the concept isn't loaded
        assert 'projection' not in entry


# ---------------------------------------------------------------------------
# project_single_value — inline projection
# ---------------------------------------------------------------------------

class TestProjectSingleValue:
    """project_single_value upserts OMOP facts for a single person + field."""

    def _setup_concept(self):
        VocabularyFactory(vocabulary_id='SNOMED')
        concept = ConceptFactory(
            concept_code='313059006', vocabulary_id='SNOMED',
            concept_name='Planned therapy',
        )
        VocabularyFactory(vocabulary_id='Type Concept')
        ConceptFactory(
            concept_id=32817, concept_code='32817',
            vocabulary_id='Type Concept', concept_name='EHR',
        )
        return concept

    def test_creates_observation_for_string_value(self):
        concept = self._setup_concept()
        person = PersonFactory()
        projection = {
            'omop_table': 'observation',
            'concept_id': concept.concept_id,
            'type_concept_id': 32817,
            'source_value': 'planned-therapies',
        }

        result = project_single_value(person, 'planned_therapies', 'chemo', projection)

        assert result is True
        obs = Observation.objects.get(
            person=person, observation_source_value='planned-therapies',
        )
        assert obs.value_as_string == 'chemo'

    def test_updates_existing_fact(self):
        concept = self._setup_concept()
        person = PersonFactory()
        projection = {
            'omop_table': 'observation',
            'concept_id': concept.concept_id,
            'type_concept_id': 32817,
            'source_value': 'planned-therapies',
        }

        project_single_value(person, 'planned_therapies', 'chemo', projection)
        project_single_value(person, 'planned_therapies', 'radiation', projection)

        obs_count = Observation.objects.filter(
            person=person, observation_source_value='planned-therapies',
        ).count()
        assert obs_count == 1
        obs = Observation.objects.get(
            person=person, observation_source_value='planned-therapies',
        )
        assert obs.value_as_string == 'radiation'

    def test_creates_measurement_for_numeric_value(self):
        VocabularyFactory(vocabulary_id='LOINC')
        concept = ConceptFactory(
            concept_code='718-7', vocabulary_id='LOINC',
            concept_name='Hemoglobin',
        )
        VocabularyFactory(vocabulary_id='Type Concept')
        ConceptFactory(
            concept_id=32865, concept_code='32865',
            vocabulary_id='Type Concept', concept_name='Patient self-report',
        )
        person = PersonFactory()
        projection = {
            'omop_table': 'measurement',
            'concept_id': concept.concept_id,
            'type_concept_id': 32865,
            'source_value': '718-7',
        }

        result = project_single_value(person, 'hemoglobin_g_dl', 12.5, projection)

        assert result is True
        m = Measurement.objects.get(
            person=person, measurement_source_value='718-7',
        )
        assert m.value_as_number == 12.5

    def test_records_explicit_empty_value(self):
        concept = self._setup_concept()
        person = PersonFactory()
        projection = {
            'omop_table': 'observation',
            'concept_id': concept.concept_id,
            'type_concept_id': 32817,
            'source_value': 'planned-therapies',
        }

        result = project_single_value(person, 'planned_therapies', None, projection)

        assert result is True
        obs = Observation.objects.get(
            person=person, observation_source_value='planned-therapies',
        )
        from omop_core.services.omop_projection import CLEAR_VALUE
        assert obs.value_source_value == CLEAR_VALUE
        assert obs.value_as_string is None
