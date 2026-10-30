"""Shared-form contract for the six current-diagnosis MM fields (#5521).

Trial Details (CB #5482/#5567) writes through the shared patient form, which
names these rows in camelCase. They must be writable there, resolve through a
binding, and save to PatientRecord even when their OMOP projection is an
occurrence that derivation never reads back.
"""
import pytest
from django.utils import timezone

from omop_core.models import Concept, ConditionOccurrence, Observation, ProvenanceRecord
from omop_core.services.patient_form import (
    apply_patient_form_patch,
    patient_form_descriptors,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import (
    ConceptFactory,
    DomainFactory,
    ObservationFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

BINDINGS = {
    'mgus': 'mgus',
    'smoldering_multiple_myeloma': 'smolderingMultipleMyeloma',
    'waldenstrom_macroglobulinemia': 'waldenstromMacroglobulinemia',
    'igm_multiple_myeloma': 'igmMultipleMyeloma',
    'al_amyloidosis': 'alAmyloidosis',
    'poems_syndrome': 'poemsSyndrome',
}

LABELS = {
    'mgus': 'MGUS',
    'smoldering_multiple_myeloma': 'Smoldering multiple myeloma',
    'waldenstrom_macroglobulinemia': 'Waldenström macroglobulinemia',
    'igm_multiple_myeloma': 'IgM multiple myeloma',
    'al_amyloidosis': 'Light-chain (AL) amyloidosis',
    'poems_syndrome': 'POEMS syndrome',
}

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
    ConceptFactory(concept_id=32865, concept_code='32865', vocabulary_id='Type Concept',
                   concept_name='Patient self-report')
    for _field, (concept_id, code, domain_id) in CONCEPTS.items():
        ConceptFactory(
            concept_id=concept_id, concept_code=code, vocabulary_id='SNOMED',
            concept_name=code, domain=DomainFactory(domain_id=domain_id),
        )


def test_shared_form_advertises_all_six_with_trial_details_bindings():
    _seed_vocab()
    descriptors = patient_form_descriptors(build_writable_field_descriptor())

    for field, binding in BINDINGS.items():
        entry = descriptors[field]
        assert entry['writable'] is True, (field, entry.get('reason'))
        assert entry['target'] == 'patient_record'
        assert entry['control'] == 'boolean'
        assert entry['label'] == LABELS[field]
        assert entry['tri_state'] is True
        assert binding in entry['bindings']


def test_a_yes_is_saved_and_projected_even_though_derivation_ignores_it():
    _seed_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(
        person, {'smoldering_multiple_myeloma': True, 'mgus': True},
    )

    assert 'smoldering_multiple_myeloma' in result.applied
    assert record.smoldering_multiple_myeloma is True
    assert record.mgus is True
    assert ConditionOccurrence.objects.filter(
        person=person, condition_concept_id=4184985,
    ).count() == 1
    assert Observation.objects.filter(
        person=person, observation_concept_id=4149022,
    ).count() == 1


def test_unknown_is_not_projected_as_a_negative_answer():
    _seed_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person, smoldering_multiple_myeloma=True)

    record, result = apply_patient_form_patch(
        person, {'smoldering_multiple_myeloma': None},
    )

    assert 'smoldering_multiple_myeloma' in result.applied
    assert record.smoldering_multiple_myeloma is None
    assert not ConditionOccurrence.objects.filter(
        person=person, condition_concept_id=4184985,
    ).exists()


def test_a_no_is_saved_without_an_occurrence():
    _seed_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, _result = apply_patient_form_patch(person, {'al_amyloidosis': False})

    assert record.al_amyloidosis is False
    assert not ConditionOccurrence.objects.filter(
        person=person, condition_concept_id=4047607,
    ).exists()


def test_a_patient_answer_is_self_report_beside_a_clinical_fact():
    _seed_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person)
    clinical = ObservationFactory(
        person=person, observation_concept_id=4149022,
        observation_source_value='35601003', value_as_number=0.0,
        observation_date=timezone.localdate(), observation_type_concept_id=32817,
    )

    record, _result = apply_patient_form_patch(
        person, {'mgus': True},
        authorship={'type_concept_id': 32865, 'patient_user_id': 'issuer|sub'},
    )

    assert record.mgus is True
    rows = Observation.objects.filter(person=person, observation_concept_id=4149022)
    assert rows.count() == 2
    untouched = rows.get(observation_id=clinical.observation_id)
    assert untouched.observation_type_concept_id == 32817
    assert untouched.value_as_number == 0.0
    self_report = rows.exclude(observation_id=clinical.observation_id).get()
    assert self_report.observation_type_concept_id == 32865
    assert ProvenanceRecord.objects.filter(
        object_id=self_report.pk, source='PATIENT_SELF', source_user_id='issuer|sub',
    ).exists()


def test_a_projection_failure_does_not_lose_the_saved_answer(monkeypatch):
    _seed_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person)

    def boom(*_args, **_kwargs):
        raise RuntimeError('projection is down')

    monkeypatch.setattr(
        'omop_core.services.omop_projection.project_single_value', boom,
    )

    record, result = apply_patient_form_patch(person, {'poems_syndrome': True})

    assert 'poems_syndrome' in result.applied
    assert record.poems_syndrome is True
    assert not ConditionOccurrence.objects.filter(
        person=person, condition_concept_id=4193776,
    ).exists()


def test_a_direct_answer_does_not_rederive_the_record(monkeypatch):
    """PatientRecord first: the record already holds the answer and the OMOP
    side effect is not read back, so the shared form must not derive now, any
    more than the native PATCH does (docs/patient-record-first-writes.md)."""
    _seed_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person)
    derived = []
    monkeypatch.setattr(
        'omop_core.services.patient_form.refresh_patient_record', derived.append,
    )

    record, result = apply_patient_form_patch(person, {'poems_syndrome': True})

    assert 'poems_syndrome' in result.applied
    assert record.poems_syndrome is True
    assert derived == []


def test_the_write_itself_skips_a_patient_answer_without_the_self_report_type():
    """The rule lives in the write, not in a view: a patient's answer that
    cannot be typed self-report is kept on the record and not projected,
    whichever caller forgot to say so."""
    _seed_vocab()
    Concept.objects.filter(concept_id=32865).delete()
    person = PersonFactory()
    PatientRecordFactory(person=person)
    clinical = ObservationFactory(
        person=person, observation_concept_id=4149022,
        observation_source_value='patient-record:mgus', value_as_number=0.0,
        observation_date=timezone.localdate(), observation_type_concept_id=32817,
    )

    record, result = apply_patient_form_patch(
        person, {'mgus': True}, authorship={}, patient_author=True,
    )

    assert 'mgus' in result.applied
    assert record.mgus is True
    clinical.refresh_from_db()
    assert clinical.value_as_number == 0.0
    assert Observation.objects.filter(person=person, observation_concept_id=4149022).count() == 1

    # Anyone else writes the recipe's own fact.
    apply_patient_form_patch(person, {'mgus': True}, authorship={}, patient_author=False)
    clinical.refresh_from_db()
    assert clinical.value_as_number == 1.0
