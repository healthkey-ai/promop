"""The explicit "no pre-existing conditions" answer and its provenance (#1988).

#1989 makes the flag authored but leaves the old formula rows behind. The repair
turns those into unknown, so an answer must exist again only because someone gave
it: a ``PreexistingConditionsAttestation`` row is that evidence, and it is kept in
step with the projection by both the API write path and the projection itself.
"""

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import PreexistingConditionsAttestation
from omop_core.services.patient_record_service import recompute_patient_record_fields
from omop_core.services.pre_existing_conditions import (
    attest_no_preexisting_conditions, is_no_preexisting_conditions_attested,
    sync_no_preexisting_conditions, withdraw_no_preexisting_conditions,
)
from patient_portal.models import Identity
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def editor(settings):
    settings.CELERY_BROKER_URL = ''
    record = PatientRecordFactory()
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='conditions@example.test', is_staff=True))
    return record, client


def test_an_explicit_answer_is_recorded_with_its_source():
    record = PatientRecordFactory(no_pre_existing_conditions=None)

    attestation = attest_no_preexisting_conditions(record.person, source='patient')

    record.refresh_from_db()
    assert attestation.person == record.person
    assert attestation.source == 'patient'
    assert record.no_pre_existing_conditions is True
    assert is_no_preexisting_conditions_attested(record.person)


def test_an_answer_is_refused_while_categories_are_recorded():
    record = PatientRecordFactory(preexisting_conditions=['infections'])

    with pytest.raises(ValueError, match='contradict'):
        attest_no_preexisting_conditions(record.person)

    assert not PreexistingConditionsAttestation.objects.filter(
        person=record.person).exists()


def test_withdrawing_the_answer_makes_the_flag_unknown_not_false():
    record = PatientRecordFactory(no_pre_existing_conditions=None)
    attest_no_preexisting_conditions(record.person)

    withdraw_no_preexisting_conditions(record.person)

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None
    assert not is_no_preexisting_conditions_attested(record.person)


def test_recording_a_category_withdraws_the_blanket_answer():
    record = PatientRecordFactory(preexisting_conditions=[], no_pre_existing_conditions=True)
    attest_no_preexisting_conditions(record.person)

    record.preexisting_conditions = ['infections']
    record.save(update_fields=['preexisting_conditions'])
    recompute_patient_record_fields(record, changed_fields={'preexisting_conditions'})

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None
    assert not is_no_preexisting_conditions_attested(record.person)


def test_sync_does_not_record_an_answer_nobody_gave():
    record = PatientRecordFactory(no_pre_existing_conditions=None)

    sync_no_preexisting_conditions(record)

    assert not is_no_preexisting_conditions_attested(record.person)


def test_sync_records_an_answer_written_through_another_path():
    """A direct write of True is still a person's answer; make the evidence match."""
    record = PatientRecordFactory(no_pre_existing_conditions=None)
    record.no_pre_existing_conditions = True
    record.save(update_fields=['no_pre_existing_conditions'])

    sync_no_preexisting_conditions(record, source='clinician')

    attestation = PreexistingConditionsAttestation.objects.get(person=record.person)
    assert attestation.source == 'clinician'


def test_a_patch_of_the_answer_creates_the_attestation(editor):
    record, client = editor

    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'no_pre_existing_conditions': True}, format='json')

    assert response.status_code == 200, response.data
    attestation = PreexistingConditionsAttestation.objects.get(person=record.person)
    assert attestation.source == 'clinician'


def test_autosave_echo_does_not_attest_a_legacy_true(editor):
    record, client = editor
    record.no_pre_existing_conditions = True
    record.save(update_fields=['no_pre_existing_conditions'])

    response = client.patch(f'/api/patient-info/{record.person_id}/', {
        'no_pre_existing_conditions': True,
        'preexisting_conditions': [],
        'ecog_performance_status': 1,
    }, format='json')

    assert response.status_code == 200, response.data
    assert not PreexistingConditionsAttestation.objects.filter(person=record.person).exists()


def test_category_echo_alone_does_not_attest_a_legacy_true(editor):
    record, client = editor
    record.no_pre_existing_conditions = True
    record.save(update_fields=['no_pre_existing_conditions'])

    response = client.patch(f'/api/patient-info/{record.person_id}/', {
        'preexisting_conditions': [],
    }, format='json')

    assert response.status_code == 200, response.data
    assert not PreexistingConditionsAttestation.objects.filter(person=record.person).exists()


def test_changing_empty_category_representation_does_not_attest_legacy_true(editor):
    record, client = editor
    record.no_pre_existing_conditions = True
    record.preexisting_conditions = None
    record.save(update_fields=['no_pre_existing_conditions', 'preexisting_conditions'])

    response = client.patch(f'/api/patient-info/{record.person_id}/', {
        'preexisting_conditions': [],
    }, format='json')

    assert response.status_code == 200, response.data
    assert not PreexistingConditionsAttestation.objects.filter(person=record.person).exists()


def test_echo_does_not_renew_existing_attestation(editor):
    record, client = editor
    attestation = attest_no_preexisting_conditions(record.person)
    original = timezone.now() - timezone.timedelta(days=30)
    PreexistingConditionsAttestation.objects.filter(pk=attestation.pk).update(
        confirmed_at=original)

    response = client.patch(f'/api/patient-info/{record.person_id}/', {
        'no_pre_existing_conditions': True,
        'preexisting_conditions': [],
    }, format='json')

    assert response.status_code == 200, response.data
    attestation.refresh_from_db()
    assert attestation.confirmed_at == original


def test_clearing_the_answer_withdraws_the_attestation(editor):
    record, client = editor
    attest_no_preexisting_conditions(record.person)
    record.refresh_from_db()

    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'no_pre_existing_conditions': None}, format='json')

    assert response.status_code == 200, response.data
    assert not PreexistingConditionsAttestation.objects.filter(
        person=record.person).exists()


def test_an_unrelated_patch_does_not_renew_the_attestation(editor):
    record, client = editor
    attestation = attest_no_preexisting_conditions(record.person)
    original = timezone.now() - timezone.timedelta(days=30)
    PreexistingConditionsAttestation.objects.filter(pk=attestation.pk).update(
        confirmed_at=original)
    record.refresh_from_db()

    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'ecog_performance_status': 1}, format='json')

    assert response.status_code == 200, response.data
    attestation.refresh_from_db()
    assert attestation.confirmed_at == original


def test_a_one_sided_category_write_is_refused_rather_than_silently_repaired(editor):
    """#1989 B2: the client chooses; the server does not guess which answer wins."""
    record, client = editor
    attest_no_preexisting_conditions(record.person)
    record.refresh_from_db()

    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'preexisting_conditions': ['infections']}, format='json')

    assert response.status_code == 400
    assert 'no_pre_existing_conditions' in response.data
    assert PreexistingConditionsAttestation.objects.filter(person=record.person).exists()


def test_clearing_the_answer_while_adding_a_category_withdraws_it(editor):
    record, client = editor
    attest_no_preexisting_conditions(record.person)
    record.refresh_from_db()

    response = client.patch(f'/api/patient-info/{record.person_id}/', {
        'no_pre_existing_conditions': None,
        'preexisting_conditions': ['infections'],
    }, format='json')

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None
    assert record.preexisting_conditions == ['infections']
    assert not PreexistingConditionsAttestation.objects.filter(
        person=record.person).exists()
