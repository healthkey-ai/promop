"""Shared-form contract for the six current-diagnosis MM fields (#5521).

Trial Details (CB #5482/#5567) writes through the shared patient form, which
names these rows in camelCase. They must be writable there, resolve through a
binding, and save to PatientRecord even when their OMOP projection is an
occurrence that derivation never reads back.
"""
import pytest

from omop_core.models import ConditionOccurrence, Observation
from omop_core.services.patient_form import (
    apply_patient_form_patch,
    patient_form_descriptors,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import (
    ConceptFactory,
    DomainFactory,
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
