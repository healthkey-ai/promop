"""Refractory Status and Relapse Count are explicit overrides (#5605/#5604).

They are inferred from therapy-line outcomes, but an explicit answer must
survive a refresh. The native serializer keeps it in
``PatientRecord.therapy_overrides``; the shared form must use the same contract
rather than writing the derived columns directly.
"""
import pytest

from omop_core.services.patient_form import apply_patient_form_patch, patient_form_descriptors
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.treatment_catalog import REFRACTORY_STATUSES
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def test_descriptor_advertises_the_overrides():
    descriptors = patient_form_descriptors(build_writable_field_descriptor())

    assert descriptors['refractory_status']['writable'] is True
    assert descriptors['relapse_count']['writable'] is True
    assert [option['value'] for option in descriptors['refractory_status']['options']] == REFRACTORY_STATUSES


def test_refractory_and_relapse_are_stored_as_overrides_and_survive_refresh():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(
        person, {'refractory_status': 'Primary Refractory', 'relapse_count': 2},
    )

    assert 'refractory_status' in result.applied
    assert 'relapse_count' in result.applied
    record.refresh_from_db()
    assert record.treatment_refractory_status == 'Primary Refractory'
    assert record.relapse_count == 2
    assert record.therapy_overrides['treatment_refractory_status'] == 'Primary Refractory'
    assert record.therapy_overrides['relapse_count'] == 2

    refresh_patient_record(person)

    record.refresh_from_db()
    assert record.treatment_refractory_status == 'Primary Refractory'
    assert record.relapse_count == 2


def test_clearing_removes_the_override():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    apply_patient_form_patch(
        person, {'treatment_refractory_status': 'Multi-Refractory', 'relapse_count': 3},
    )

    record, _ = apply_patient_form_patch(
        person, {'refractory_status': '', 'relapse_count': None},
    )

    record.refresh_from_db()
    assert 'treatment_refractory_status' not in (record.therapy_overrides or {})
    assert 'relapse_count' not in (record.therapy_overrides or {})


def test_invalid_values_are_rejected():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    _, result = apply_patient_form_patch(
        person, {'refractory_status': 'Nope', 'relapse_count': 2.5},
    )

    assert 'refractory_status' in result.rejected
    assert 'relapse_count' in result.rejected


def test_zero_is_an_explicit_answer_not_a_clear():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(person, {'relapse_count': 0})

    assert 'relapse_count' in result.applied
    record.refresh_from_db()
    assert record.relapse_count == 0
    assert record.therapy_overrides['relapse_count'] == 0
    assert refresh_patient_record(person).relapse_count == 0

    record, _ = apply_patient_form_patch(person, {'relapse_count': None})
    record.refresh_from_db()
    assert 'relapse_count' not in (record.therapy_overrides or {})


def test_a_correction_replaces_the_previous_override():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    apply_patient_form_patch(person, {'refractory_status': 'Primary Refractory'})
    apply_patient_form_patch(person, {'refractory_status': 'Secondary Refractory'})

    record = refresh_patient_record(person)
    assert record.treatment_refractory_status == 'Secondary Refractory'
    assert record.therapy_overrides['treatment_refractory_status'] == 'Secondary Refractory'


def test_negative_relapse_count_is_rejected():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    _, result = apply_patient_form_patch(person, {'relapse_count': -1})

    assert 'relapse_count' in result.rejected


@pytest.mark.parametrize('value', REFRACTORY_STATUSES)
def test_every_catalog_choice_is_stored_verbatim(value):
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, _ = apply_patient_form_patch(person, {'refractory_status': value})

    record.refresh_from_db()
    assert record.therapy_overrides['treatment_refractory_status'] == value
    assert refresh_patient_record(person).treatment_refractory_status == value
