"""PatientRecord-first writes for the six current-diagnosis MM fields (#5521)."""
import pytest

from omop_core.models import ConditionOccurrence, Observation
from omop_core.services.omop_projection import project_single_value
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _OMOP_DERIVED_FIELDS,
    refresh_patient_record,
)
from omop_core.services.write_descriptor import KIND_DIRECT, build_writable_field_descriptor
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.serializers import PatientRecordSerializer
from tests.factories import (
    ConditionOccurrenceFactory,
    ConceptFactory,
    DomainFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

FIELDS = (
    'mgus',
    'smoldering_multiple_myeloma',
    'waldenstrom_macroglobulinemia',
    'igm_multiple_myeloma',
    'al_amyloidosis',
    'poems_syndrome',
)

CONCEPTS = {
    'mgus': (4149022, '35601003', 'Observation'),
    'smoldering_multiple_myeloma': (4184985, '413587002', 'Condition'),
    'waldenstrom_macroglobulinemia': (4098597, '190818004', 'Condition'),
    'al_amyloidosis': (4047607, '23132008', 'Condition'),
    'poems_syndrome': (4193776, '79268002', 'Condition'),
}


def _seed_vocab():
    VocabularyFactory(vocabulary_id='SNOMED')
    VocabularyFactory(vocabulary_id='Type Concept')
    ConceptFactory(concept_id=32817, concept_code='32817', vocabulary_id='Type Concept',
                   concept_name='EHR')
    for field, (concept_id, code, domain_id) in CONCEPTS.items():
        domain = DomainFactory(domain_id=domain_id)
        ConceptFactory(
            concept_id=concept_id, concept_code=code, vocabulary_id='SNOMED',
            concept_name=field.replace('_', ' ').title(), domain=domain,
        )


def test_all_six_are_direct_and_writable():
    _seed_vocab()
    descriptor = build_writable_field_descriptor()

    for field in FIELDS:
        entry = descriptor[field]
        assert entry['kind'] == KIND_DIRECT
        assert entry['writable'] is True
        assert entry['target'] == 'patient_record'
        assert entry['value_kind'] == 'boolean'
        assert entry['tri_state'] is True

    # Five have a reviewed concept; IgM myeloma deliberately has none.
    for field in CONCEPTS:
        assert 'projection' in descriptor[field]
    assert 'projection' not in descriptor['igm_multiple_myeloma']


def test_true_projects_an_affirmative_fact_and_false_does_not():
    _seed_vocab()
    descriptor = build_writable_field_descriptor()
    person = PersonFactory()

    mgus = {**descriptor['mgus']['projection'], 'value_kind': 'boolean'}
    assert project_single_value(person, 'mgus', True, mgus) is True
    assert Observation.objects.filter(
        person=person, observation_concept_id=4149022,
    ).count() == 1

    assert project_single_value(person, 'mgus', False, mgus) is False
    assert Observation.objects.filter(
        person=person, observation_concept_id=4149022,
    ).count() == 1  # no negative occurrence invented

    smm = {
        **descriptor['smoldering_multiple_myeloma']['projection'],
        'value_kind': 'boolean',
    }
    assert project_single_value(person, 'smoldering_multiple_myeloma', True, smm) is True
    assert ConditionOccurrence.objects.filter(
        person=person, condition_concept_id=4184985,
    ).count() == 1


def test_the_fields_are_writable_but_never_derived():
    """They are user-entered current state, not a projection of OMOP history."""
    for field in FIELDS:
        assert field in PATIENT_RECORD_OMOP_MAPPED_FIELDS
        assert field not in _OMOP_DERIVED_FIELDS


def test_a_manual_false_survives_refresh_and_imported_history_is_not_current_yes():
    _seed_vocab()
    person = PersonFactory()
    record = PatientRecordFactory(person=person, mgus=False)
    record.user_edited_fields = ['mgus']
    record.save(update_fields=['mgus', 'user_edited_fields'])

    # An imported historical MGUS occurrence must not turn the current answer
    # into Yes, and the patient's explicit No must survive the refresh.
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=person, condition_concept_id=4149022,
            condition_source_value='35601003',
        )

    refreshed = refresh_patient_record(person)

    assert refreshed.mgus is False
    assert refreshed.smoldering_multiple_myeloma is None


def test_serializer_accepts_true_false_and_null():
    _seed_vocab()
    person = PersonFactory()
    record = PatientRecordFactory(person=person)

    serializer = PatientRecordSerializer(
        record, data={'mgus': True, 'al_amyloidosis': None}, partial=True,
    )
    assert serializer.is_valid(), serializer.errors
    saved = serializer.save()

    assert saved.mgus is True
    assert saved.al_amyloidosis is None
