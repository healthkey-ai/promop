"""PatientRecord-first writes for the six current-diagnosis MM fields (#5521)."""
from datetime import date

import pytest

from django.utils import timezone

from omop_core.models import Concept, ConditionOccurrence, FieldConceptMapping, Observation
from omop_core.services.omop_projection import project_field_to_omop, project_single_value
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _OMOP_DERIVED_FIELDS,
    refresh_patient_record,
)
from omop_core.services.write_descriptor import KIND_DIRECT, build_writable_field_descriptor
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.serializers import PatientRecordSerializer
from tests.factories import (
    ConceptFactory,
    ConditionOccurrenceFactory,
    DomainFactory,
    ObservationFactory,
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


def test_imported_positive_history_does_not_become_current_yes():
    """MGUS is an Observation-domain concept; history is not a current answer."""
    _seed_vocab()
    person = PersonFactory()
    record = PatientRecordFactory(person=person, mgus=False)
    record.user_edited_fields = ['mgus']
    record.save(update_fields=['mgus', 'user_edited_fields'])

    with suppress_patient_record_refresh():
        ObservationFactory(
            person=person, observation_concept_id=4149022,
            observation_source_value='35601003', value_as_number=1.0,
            observation_date=timezone.localdate(),
        )

    refreshed = refresh_patient_record(person)

    assert refreshed.mgus is False
    assert refreshed.smoldering_multiple_myeloma is None


def test_an_unknown_answer_survives_refresh_with_an_existing_positive_fact():
    _seed_vocab()
    person = PersonFactory()
    descriptor = build_writable_field_descriptor()
    projection = {**descriptor['mgus']['projection'], 'value_kind': 'boolean'}
    assert project_single_value(person, 'mgus', True, projection) is True

    record = PatientRecordFactory(person=person)
    record.mgus = None
    record.user_edited_fields = ['mgus']
    record.save(update_fields=['mgus', 'user_edited_fields'])

    refreshed = refresh_patient_record(person)

    assert refreshed.mgus is None
    # The earlier positive fact is history; it is neither deleted nor promoted.
    assert Observation.objects.filter(
        person=person, observation_concept_id=4149022,
    ).count() == 1


def test_a_curated_mapping_cannot_replace_the_pinned_recipe():
    _seed_vocab()
    concept = ConceptFactory(
        concept_id=9_999_991, concept_code='MGUS-ALT', vocabulary_id='SNOMED',
        concept_name='Alternative MGUS concept',
    )
    FieldConceptMapping.objects.create(
        field_name='mgus', concept=concept, status='approved', omop_table='observation',
    )

    entry = build_writable_field_descriptor()['mgus']

    assert entry['tri_state'] is True
    assert entry['projection']['concept_id'] == 4149022
    assert entry['projection']['affirmative_only'] is True
    assert entry['projection']['best_effort'] is True


def test_a_curated_mapping_does_not_derive_a_current_answer():
    _seed_vocab()
    concept = ConceptFactory(
        concept_id=9_999_992, concept_code='MGUS-ALT-2', vocabulary_id='SNOMED',
        concept_name='Alternative MGUS concept',
    )
    FieldConceptMapping.objects.create(
        field_name='mgus', concept=concept, status='approved', omop_table='observation',
    )
    person = PersonFactory()
    PatientRecordFactory(person=person)

    with suppress_patient_record_refresh():
        ObservationFactory(
            person=person, observation_concept_id=concept.concept_id,
            observation_source_value=concept.concept_code, value_as_number=1.0,
            observation_date=timezone.localdate(),
        )

    assert refresh_patient_record(person).mgus is None


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


def test_approving_a_curated_mapping_does_not_backfill_a_pinned_answer():
    _seed_vocab()
    person = PersonFactory()
    record = PatientRecordFactory(person=person, mgus=True)
    record.user_edited_fields = ['mgus']
    record.save(update_fields=['mgus', 'user_edited_fields'])
    clinical = ObservationFactory(
        person=person, observation_concept_id=4149022,
        observation_source_value='35601003', value_as_number=0.0,
        observation_date=timezone.localdate(), observation_type_concept_id=32817,
    )
    concept = ConceptFactory(
        concept_id=9_999_993, concept_code='MGUS-ALT-3', vocabulary_id='SNOMED',
        concept_name='Alternative MGUS concept',
    )
    mapping = FieldConceptMapping.objects.create(
        field_name='mgus', concept=concept, status='approved', omop_table='observation',
    )

    assert project_field_to_omop(mapping) == 0

    clinical.refresh_from_db()
    assert clinical.value_as_number == 0.0
    assert Observation.objects.filter(
        person=person, observation_concept_id=4149022,
    ).count() == 1


def test_a_yes_does_not_displace_the_primary_cancer():
    """The answer row is dated today and 'Smoldering Multiple Myeloma' reads as a
    cancer, so without the source-value marker the primary-cancer ranking would
    pick it over an untreated myeloma diagnosed years ago (review of #2098)."""
    _seed_vocab()
    person = PersonFactory()
    myeloma = ConceptFactory(
        concept_id=437233, concept_code='109989006', vocabulary_id='SNOMED',
        concept_name='Multiple myeloma', domain=DomainFactory(domain_id='Condition'),
    )
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=person, condition_concept=myeloma, condition_start_date='2020-03-01',
        )
    assert refresh_patient_record(person).disease == 'Multiple Myeloma'

    descriptor = build_writable_field_descriptor()
    projection = {**descriptor['smoldering_multiple_myeloma']['projection'], 'value_kind': 'boolean'}
    assert project_single_value(person, 'smoldering_multiple_myeloma', True, projection) is True

    refreshed = refresh_patient_record(person)

    assert refreshed.disease == 'Multiple Myeloma'
    assert refreshed.diagnosis_date == date(2020, 3, 1)
    answer = ConditionOccurrence.objects.get(person=person, condition_concept_id=4184985)
    assert answer.condition_source_value == 'patient-record:smoldering_multiple_myeloma'


def test_a_later_no_or_unknown_retracts_the_projected_yes():
    _seed_vocab()
    descriptor = build_writable_field_descriptor()
    person = PersonFactory()

    smm = {**descriptor['smoldering_multiple_myeloma']['projection'], 'value_kind': 'boolean'}
    assert project_single_value(person, 'smoldering_multiple_myeloma', True, smm) is True
    rows = ConditionOccurrence.objects.filter(person=person, condition_concept_id=4184985)
    assert rows.filter(is_erroneous=False).count() == 1

    assert project_single_value(person, 'smoldering_multiple_myeloma', False, smm) is False
    assert rows.filter(is_erroneous=False).count() == 0
    assert rows.get().erroneous_reason  # kept as history, flagged in error

    mgus = {**descriptor['mgus']['projection'], 'value_kind': 'boolean'}
    assert project_single_value(person, 'mgus', True, mgus) is True
    assert project_single_value(person, 'mgus', None, mgus) is False
    assert not Observation.objects.filter(
        person=person, observation_concept_id=4149022, is_erroneous=False,
    ).exists()


def test_a_no_retracts_only_the_answers_own_rows():
    """An imported diagnosis with the same concept, and another author's answer, stay."""
    _seed_vocab()
    ConceptFactory(concept_id=32865, concept_code='32865', vocabulary_id='Type Concept',
                   concept_name='Patient self-report')
    descriptor = build_writable_field_descriptor()
    person = PersonFactory()
    smm = {**descriptor['smoldering_multiple_myeloma']['projection'], 'value_kind': 'boolean'}
    with suppress_patient_record_refresh():
        imported = ConditionOccurrenceFactory(
            person=person, condition_concept=Concept.objects.get(concept_id=4184985),
            condition_source_value='413587002', condition_start_date='2021-01-01',
        )
    assert project_single_value(person, 'smoldering_multiple_myeloma', True, smm) is True
    assert project_single_value(
        person, 'smoldering_multiple_myeloma', True, smm, type_concept_id=32865,
    ) is True
    live = ConditionOccurrence.objects.filter(
        person=person, condition_concept_id=4184985, is_erroneous=False,
    )
    assert live.count() == 3

    # The patient's No withdraws only the patient's own answer.
    assert project_single_value(
        person, 'smoldering_multiple_myeloma', False, smm, type_concept_id=32865,
    ) is False
    assert set(live.values_list('condition_type_concept_id', flat=True)) == {
        32817, imported.condition_type_concept_id,
    }

    # A clinician's No withdraws the clinical answer, and still not the import.
    assert project_single_value(person, 'smoldering_multiple_myeloma', False, smm) is False
    assert list(live.values_list('condition_occurrence_id', flat=True)) == [imported.pk]


def test_a_yes_is_not_read_back_as_another_active_malignancy(monkeypatch):
    """Smoldering myeloma is a SNOMED descendant of malignant neoplastic
    disease, so without the source-value marker the answer row would count as
    a second active malignancy and flip no_other_active_malignancies."""
    from omop_core.services import patient_record_service

    _seed_vocab()
    monkeypatch.setattr(
        patient_record_service, '_active_condition_descendant_ids',
        lambda _root: {4184985, 4098597},
    )
    person = PersonFactory()
    PatientRecordFactory(person=person)
    descriptor = build_writable_field_descriptor()
    projection = {**descriptor['smoldering_multiple_myeloma']['projection'], 'value_kind': 'boolean'}
    assert project_single_value(person, 'smoldering_multiple_myeloma', True, projection) is True

    refreshed = refresh_patient_record(person)

    assert refreshed.active_malignancies == []
    assert refreshed.no_other_active_malignancies is True
