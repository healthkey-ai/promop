"""A retained PatientRecord value is not proof that derivation backed it.

`refresh_patient_record` may drop a pending marker only when the refresh
actually evaluated the field. A field that is neither cleared nor populated
keeps its stored value, so comparing it with itself would falsely confirm an
edit that has no OMOP backing (docs/patient-record-first-writes.md:190-206).
"""
import pytest
from django.utils import timezone

from omop_core.models import Concept, FieldConceptMapping, Observation
from omop_core.services.omop_projection import project_field_to_omop
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

HEMOGLOBIN_CONCEPT = 3000963
HEMOGLOBIN_CODE = '718-7'
PATIENT_SELF_REPORT = 32865
EHR_TYPE = 32817


def _seed_lab_vocab():
    for vocabulary_id in ('LOINC', 'UCUM', 'Type Concept'):
        VocabularyFactory(vocabulary_id=vocabulary_id)
    ConceptFactory(
        concept_id=HEMOGLOBIN_CONCEPT, concept_code=HEMOGLOBIN_CODE,
        vocabulary_id='LOINC', concept_name='Hemoglobin',
    )
    ConceptFactory(
        concept_id=8713, concept_code='g/dL', vocabulary_id='UCUM',
        concept_name='gram per deciliter',
    )
    ConceptFactory(
        concept_id=PATIENT_SELF_REPORT, concept_code=str(PATIENT_SELF_REPORT),
        vocabulary_id='Type Concept', concept_name='Patient self-report',
    )
    ConceptFactory(
        concept_id=EHR_TYPE, concept_code=str(EHR_TYPE),
        vocabulary_id='Type Concept', concept_name='EHR',
    )


def test_unmapped_pending_edit_keeps_its_marker_across_refresh():
    person = PersonFactory()
    record = PatientRecordFactory(person=person, planned_therapies='watch and wait')
    record.user_edited_fields = ['planned_therapies']
    record.save(update_fields=['user_edited_fields'])

    refreshed = refresh_patient_record(person)

    assert refreshed.planned_therapies == 'watch and wait'
    assert 'planned_therapies' in refreshed.user_edited_fields


def test_unmapped_pending_edit_can_still_be_backfilled_after_refresh():
    person = PersonFactory()
    record = PatientRecordFactory(person=person, planned_therapies='watch and wait')
    record.user_edited_fields = ['planned_therapies']
    record.save(update_fields=['user_edited_fields'])

    refresh_patient_record(person)

    VocabularyFactory(vocabulary_id='SNOMED')
    VocabularyFactory(vocabulary_id='Type Concept')
    concept = ConceptFactory(
        concept_code='313059006', vocabulary_id='SNOMED',
        concept_name='Planned therapy',
    )
    ConceptFactory(
        concept_id=EHR_TYPE, concept_code=str(EHR_TYPE),
        vocabulary_id='Type Concept', concept_name='EHR',
    )
    mapping = FieldConceptMapping.objects.create(
        field_name='planned_therapies',
        concept=concept,
        omop_table='observation',
        source_value='planned-therapies',
        value_kind='string',
        status='proposed',
    )
    # The post_save signal backfills on an approved save; update() lets this
    # test exercise the marker-based backfill directly.
    FieldConceptMapping.objects.filter(pk=mapping.pk).update(status='approved')
    mapping.refresh_from_db()

    assert project_field_to_omop(mapping) == 1
    assert Observation.objects.filter(
        person=person, observation_source_value='planned-therapies',
    ).exists()


def test_pinned_answer_keeps_its_marker_across_refresh():
    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    record.mgus = True
    record.user_edited_fields = ['mgus']
    record.save(update_fields=['mgus', 'user_edited_fields'])

    refreshed = refresh_patient_record(person)

    assert refreshed.mgus is True
    assert 'mgus' in refreshed.user_edited_fields


def test_derived_value_still_drops_its_marker():
    _seed_lab_vocab()
    person = PersonFactory()
    record = PatientRecordFactory(person=person, hemoglobin_g_dl=12.5)
    record.user_edited_fields = ['hemoglobin_g_dl']
    record.save(update_fields=['hemoglobin_g_dl', 'user_edited_fields'])
    MeasurementFactory(
        person=person,
        measurement_concept=Concept.objects.get(pk=HEMOGLOBIN_CONCEPT),
        measurement_source_value=HEMOGLOBIN_CODE,
        measurement_date=timezone.localdate(),
        measurement_type_concept=Concept.objects.get(pk=PATIENT_SELF_REPORT),
        value_as_number=12.5,
        unit_source_value='g/dL',
    )

    refreshed = refresh_patient_record(person)

    assert refreshed.hemoglobin_g_dl == 12.5
    assert 'hemoglobin_g_dl' not in (refreshed.user_edited_fields or [])
