import pytest
from rest_framework.test import APIClient

from omop_core.models import PreExistingConditionCategory, PreexistingConditionsAttestation
from patient_portal.models import Identity, PatientUser
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def editor():
    record = PatientRecordFactory(no_pre_existing_conditions=True, preexisting_conditions=[])
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='explicit@example.test', is_staff=True))
    return record, client


def answer(editor, action, **extra):
    record, client = editor
    return client.post(
        f'/api/v1/patient-records/{record.person_id}/preexisting-conditions-answer/', {
            'action': action, 'expected_flag': record.no_pre_existing_conditions,
            'expected_categories': record.preexisting_conditions, **extra,
        }, format='json')


def test_explicit_reconfirmation_of_unchanged_true(editor):
    record, _ = editor
    response = answer(editor, 'confirm_none')
    assert response.status_code == 200, response.data
    row = PreexistingConditionsAttestation.objects.get(person=record.person)
    assert row.source == 'clinician-explicit'
    assert answer(editor, 'confirm_none').status_code == 200
    row.refresh_from_db()
    assert PreexistingConditionsAttestation.objects.count() == 1


def test_withdraw_is_unknown_and_preserves_categories(editor):
    record, _ = editor
    answer(editor, 'confirm_none')
    response = answer(editor, 'withdraw')
    assert response.status_code == 200
    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None
    assert record.preexisting_conditions == []
    assert not PreexistingConditionsAttestation.objects.exists()


def test_categories_supersede_none_in_one_atomic_action(editor):
    record, _ = editor
    PreExistingConditionCategory.objects.create(code='infections', title='Infections')
    answer(editor, 'confirm_none')
    response = answer(editor, 'set_categories', categories=['infections'])
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None
    assert record.preexisting_conditions == ['infections']
    assert not PreexistingConditionsAttestation.objects.exists()
    assert answer(editor, 'confirm_none').status_code == 400
    assert answer(editor, 'set_categories', categories=[]).status_code == 200
    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None


def test_stale_snapshot_is_refused_without_creating_attestation(editor):
    record, _ = editor
    response = answer(editor, 'confirm_none', expected_flag=None)
    assert response.status_code == 409
    assert not PreexistingConditionsAttestation.objects.exists()


@pytest.mark.parametrize('action', ['confirm_none', 'withdraw', 'set_categories'])
def test_all_actions_accept_null_category_snapshot(editor, action):
    record, _ = editor
    record.preexisting_conditions = None
    record.save(update_fields=['preexisting_conditions'])
    extra = {'categories': []} if action == 'set_categories' else {}
    response = answer(editor, action, **extra)
    assert response.status_code == 200, response.data


@pytest.mark.parametrize('extra', [
    {'categories': ['not-a-category']}, {'categories': 'infections'}, {},
])
def test_invalid_categories_refused(editor, extra):
    assert answer(editor, 'set_categories', **extra).status_code == 400
    assert not PreexistingConditionsAttestation.objects.exists()


def test_patient_can_confirm_own_record_but_not_someone_elses(editor):
    record, client = editor
    patient = Identity.objects.create_user(email='patient-explicit@example.test')
    PatientUser.objects.create(identity=patient, person=record.person)
    client.force_authenticate(patient)
    response = answer(editor, 'confirm_none')
    assert response.status_code == 200, response.data
    assert PreexistingConditionsAttestation.objects.get(person=record.person).source == 'patient-explicit'
    other = PatientRecordFactory()
    response = client.post(
        f'/api/v1/patient-records/{other.person_id}/preexisting-conditions-answer/', {}, format='json')
    assert response.status_code in (403, 404)


def test_snapshot_and_deliberate_action_are_required(editor):
    record, client = editor
    response = client.post(
        f'/api/v1/patient-records/{record.person_id}/preexisting-conditions-answer/',
        {'no_pre_existing_conditions': True}, format='json')
    assert response.status_code == 400
    assert not PreexistingConditionsAttestation.objects.exists()


def test_snapshot_get_is_read_only_and_v1_only(editor):
    record, client = editor
    response = client.get(
        f'/api/v1/patient-records/{record.person_id}/preexisting-conditions-answer/')
    assert response.status_code == 200
    assert response.data == {'no_pre_existing_conditions': True, 'preexisting_conditions': []}
    assert not PreexistingConditionsAttestation.objects.exists()
    from django.urls import resolve
    legacy = resolve(f'/api/patient-info/{record.person_id}/preexisting-conditions-answer/')
    assert getattr(legacy.func, 'actions', {}).get('get') != 'preexisting_conditions_answer'


def test_analyst_cannot_confirm(editor):
    from omop_core.models import GroupAccess
    record, client = editor
    analyst = Identity.objects.create_user(email='analyst-explicit@example.test')
    GroupAccess.objects.create(identity=analyst, org=record.organization, role='analyst')
    client.force_authenticate(analyst)
    assert answer(editor, 'confirm_none').status_code == 403
    assert not PreexistingConditionsAttestation.objects.exists()


@pytest.mark.parametrize('scope,expected', [('patient/*.read', 403), ('patient/*.write', 200)])
def test_oauth_requires_write_scope_even_for_staff(editor, scope, expected):
    from datetime import timedelta
    from django.utils import timezone
    from oauth2_provider.models import AccessToken, Application
    _, client = editor
    user = Identity.objects.get(email='explicit@example.test')
    app = Application.objects.create(
        name='Explicit answer test', client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE, user=user)
    token = AccessToken.objects.create(
        application=app, user=user, token=f'explicit-{scope}', scope=scope,
        expires=timezone.now() + timedelta(hours=1))
    client.force_authenticate(user, token=token)
    assert answer(editor, 'confirm_none').status_code == expected


def test_audit_accepts_only_the_explicit_endpoint_provenance(editor, tmp_path):
    from django.core.management import call_command
    from io import StringIO
    import json
    record, _ = editor
    assert answer(editor, 'confirm_none').status_code == 200
    output = tmp_path / 'audit.json'
    call_command('audit_pre_existing_conditions', output=str(output), stdout=StringIO())
    row = next(row for row in json.loads(output.read_text())['records'] if row['id'] == record.id)
    assert row['classification'] == 'authored_true'
