"""Closing an account: ``DELETE /api/v1/phr/account/``, the ``my_record``
withdrawal hook, the deletion log and its replay after a restore."""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import (
    ConditionOccurrence, GroupAccess, PatientGroup, PatientGroupMembership, PatientRecord, Person,
    PersonalRepresentative,
)
from omop_core.signals import suppress_patient_record_refresh
from patient_portal import account_deletion as svc
from patient_portal import consent as consent_svc
from patient_portal.models import (
    AuditEvent, ConsentEvent, ConsentLedgerEntry, ConsentPurpose, ConsentText, DeletionLogEntry, Identity,
    PatientUser,
)
from prolog_surveys.definitions.loader import load_definition
from prolog_surveys.models import ResponseStatus, SurveyResponse
from tests.factories import ConditionOccurrenceFactory, OrganizationFactory, PatientRecordFactory, PersonFactory


def signed_in(record):
    """A real session login, not ``force_authenticate``: the account and
    consent writes accept only a session or partner login, and forcing
    bypasses every authenticator."""
    identity = Identity.objects.create_user(email=f'p{record.person_id}@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_login(identity)
    return client


def oauth_client(identity, scope='patient/*.read'):
    """A client presenting an OAuth2 access token the member delegated."""
    from oauth2_provider.models import AccessToken, Application
    app = Application.objects.create(name='smart-app', user=identity,
                                     authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE)
    token = AccessToken.objects.create(application=app, user=identity, scope=scope,
                                       expires=timezone.now() + timedelta(hours=1), token=f'tok-{identity.pk}')
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.token}')
    return client

pytestmark = pytest.mark.django_db

EARLIER = timezone.now() - timedelta(days=30)


def survey_version():
    return load_definition({
        'schema_version': 1, 'slug': 'wellbeing', 'version': '1.0', 'status': 'draft',
        'default_language': 'en', 'languages': ['en'], 'title': {'en': 'Wellbeing'}, 'estimated_minutes': 5,
        'sections': [{'key': 's1', 'title': {'en': 'Wellbeing'}, 'questions': [
            {'key': 'q1', 'type': 'single', 'text': {'en': 'Tired?'},
             'options': [{'key': 'no', 'label': {'en': 'No'}}, {'key': 'yes', 'label': {'en': 'Yes'}}]}]}],
    }, activate=True).version


def member_with_data():
    """A signed-in member with a record, a condition, a survey response and a
    granted ``my_record`` consent. Returns (client, person, identity)."""
    record = PatientRecordFactory(country='GB')
    client = signed_in(record)
    person = record.person
    identity = PatientUser.objects.get(person=person).identity
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(person=person)
    SurveyResponse.objects.create(
        survey_version=survey_version(), participant_id=person.person_id, language='en',
        status=ResponseStatus.SUBMITTED, submitted_at=timezone.now(),
    )
    purpose = ConsentPurpose.objects.create(key='my_record', kind='permission')
    ConsentText.objects.create(purpose=purpose, version='1.0', language='en', title='t', body='b', published_at=EARLIER)
    consent_svc.record_consent(person, purpose, ConsentEvent.GRANTED, version='1.0')
    return client, person, identity


def assert_gone(person_id, identity_id):
    assert not Person.objects.filter(person_id=person_id).exists()
    assert not PatientRecord.objects.filter(person_id=person_id).exists()
    assert not ConditionOccurrence.objects.filter(person_id=person_id).exists()
    assert not SurveyResponse.objects.filter(participant_id=person_id).exists()
    assert not ConsentEvent.objects.filter(person_id=person_id).exists()
    assert not PatientUser.objects.filter(person_id=person_id).exists()
    assert not Identity.objects.filter(pk=identity_id).exists()


# --------------------------------------------------------------- the endpoint


def test_delete_account_removes_everything_and_leaves_the_ledger_and_the_log():
    client, person, identity = member_with_data()
    representative = Identity.objects.create_user(email='rep@example.test')
    PersonalRepresentative.objects.create(
        representative=representative, person_id=person.person_id, relationship='spouse',
        verification_status='VERIFIED',
    )
    group = PatientGroup.objects.create(organization=OrganizationFactory(), name='G', slug='g')
    PatientGroupMembership.objects.create(group=group, person_id=person.person_id)
    assert ConsentLedgerEntry.objects.count() == 1

    response = client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json')

    assert response.status_code == 200, response.data
    assert 'deleted_at' in response.data
    assert_gone(person.person_id, identity.pk)
    # Access rows keyed by a bare person_id would otherwise outlive the person.
    assert not PersonalRepresentative.objects.filter(person_id=person.person_id).exists()
    assert not PatientGroupMembership.objects.filter(person_id=person.person_id).exists()
    assert ConsentLedgerEntry.objects.count() == 1
    entry = DeletionLogEntry.objects.get()
    assert (entry.person_id, entry.identity_id, entry.reason) == (
        person.person_id, identity.pk, DeletionLogEntry.ACCOUNT_CLOSED,
    )
    assert svc.verify_deletion_log().ok
    # The audit trail still names the actor and the record, although the
    # login is gone and the session was logged out.
    audit = AuditEvent.objects.get(path='/api/v1/phr/account/', method='DELETE')
    assert (audit.user_id, audit.resource_id) == (str(identity.pk), str(person.person_id))


def test_delete_account_keeps_a_login_that_holds_provider_access():
    client, person, identity = member_with_data()
    GroupAccess.objects.create(identity=identity, org=OrganizationFactory(), role='doctor')

    response = client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json')

    assert response.status_code == 200, response.data
    assert not Person.objects.filter(person_id=person.person_id).exists()
    assert not PatientUser.objects.filter(identity=identity).exists()
    assert Identity.objects.filter(pk=identity.pk).exists()
    assert GroupAccess.objects.filter(identity=identity).exists()
    assert DeletionLogEntry.objects.get().identity_id is None
    # Still signed in as a provider.
    assert client.get('/api/v1/phr/account/').status_code == 405


def test_a_second_delete_after_the_first_committed_is_a_404_and_logs_nothing_more():
    client, person, identity = member_with_data()
    assert client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json').status_code == 200
    with pytest.raises(svc.AlreadyDeleted):
        svc.delete_member(person, reason=DeletionLogEntry.ACCOUNT_CLOSED)
    assert DeletionLogEntry.objects.count() == 1


def test_head_is_not_served():
    client, person, identity = member_with_data()
    assert client.head('/api/v1/phr/account/').status_code == 405


def test_delete_account_needs_the_confirmation():
    client, person, identity = member_with_data()
    response = client.delete('/api/v1/phr/account/', {}, format='json')
    assert response.status_code == 400
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert not DeletionLogEntry.objects.exists()


def test_delete_account_only_removes_the_callers_own_account():
    client, person, identity = member_with_data()
    other = PatientRecordFactory()
    other_client = signed_in(other)

    response = other_client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json')

    assert response.status_code == 200
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert Identity.objects.filter(pk=identity.pk).exists()
    assert not Person.objects.filter(person_id=other.person.person_id).exists()
    assert DeletionLogEntry.objects.get().person_id == other.person.person_id


def test_anonymous_and_staff_callers_are_refused():
    client, person, identity = member_with_data()
    assert APIClient().delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json').status_code in (401, 403)
    staff = APIClient()
    staff.force_login(Identity.objects.create_user(email='staff@example.test', is_staff=True))
    assert staff.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json').status_code == 404
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert not DeletionLogEntry.objects.exists()


@pytest.mark.parametrize('scope', ['patient/*.read', 'patient/*.write'])
def test_an_oauth_token_cannot_close_the_account(scope):
    client, person, identity = member_with_data()
    delegated = oauth_client(identity, scope)
    response = delegated.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json')
    assert response.status_code == 403
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert Identity.objects.filter(pk=identity.pk).exists()
    assert not DeletionLogEntry.objects.exists()


def test_an_oauth_token_may_read_consent_but_not_answer_it():
    client, person, identity = member_with_data()
    delegated = oauth_client(identity, 'patient/*.write')
    assert delegated.get('/api/v1/phr/consent/').status_code == 200
    response = delegated.post('/api/v1/phr/consent/', {
        'decisions': [{'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'}],
    }, format='json')
    assert response.status_code == 403
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert not DeletionLogEntry.objects.exists()
    assert [e.decision for e in ConsentEvent.objects.filter(person=person)] == ['granted']


# ----------------------------------------------------------- the consent hook


def test_withdrawing_my_record_closes_the_account():
    client, person, identity = member_with_data()

    response = client.post('/api/v1/phr/consent/', {
        'decisions': [{'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'}],
    }, format='json')

    assert response.status_code == 200, response.data
    assert response.data['account_deleted'] is True
    assert_gone(person.person_id, identity.pk)
    ledger = list(ConsentLedgerEntry.objects.order_by('id').values_list('decision', flat=True))
    assert ledger == ['granted', 'withdrawn']
    assert DeletionLogEntry.objects.get().reason == DeletionLogEntry.PERMISSION_WITHDRAWN


def test_withdrawing_my_record_keeps_a_provider_login():
    client, person, identity = member_with_data()
    GroupAccess.objects.create(identity=identity, org=OrganizationFactory(), role='doctor')

    response = client.post('/api/v1/phr/consent/', {
        'decisions': [{'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'}],
    }, format='json')

    assert response.status_code == 200, response.data
    assert response.data['account_deleted'] is True
    assert not Person.objects.filter(person_id=person.person_id).exists()
    assert Identity.objects.filter(pk=identity.pk).exists()
    assert GroupAccess.objects.filter(identity=identity).exists()
    assert DeletionLogEntry.objects.get().identity_id is None


def test_withdrawing_another_purpose_does_not(settings):
    settings.CONSENT_RECORD_PURPOSE_KEY = 'keep_record'
    client, person, identity = member_with_data()

    response = client.post('/api/v1/phr/consent/', {
        'decisions': [{'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'}],
    }, format='json')

    assert response.status_code == 200, response.data
    assert 'account_deleted' not in response.data
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert not DeletionLogEntry.objects.exists()


def test_a_refused_decision_in_the_batch_deletes_nothing():
    client, person, identity = member_with_data()

    response = client.post('/api/v1/phr/consent/', {
        'decisions': [
            {'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'},
            {'purpose': 'no_such_purpose', 'decision': 'granted', 'version': '1.0'},
        ],
    }, format='json')

    assert response.status_code == 400
    assert Person.objects.filter(person_id=person.person_id).exists()
    assert ConsentLedgerEntry.objects.count() == 1
    assert not DeletionLogEntry.objects.exists()


# ----------------------------------------------------------- the legacy paths


def test_legacy_me_delete_uses_the_same_service():
    client, person, identity = member_with_data()
    response = client.delete('/api/patient-info/me/', {'confirm': 'DELETE'}, format='json')
    assert response.status_code == 200, response.data
    assert_gone(person.person_id, identity.pk)
    assert DeletionLogEntry.objects.get().reason == DeletionLogEntry.ACCOUNT_CLOSED


def test_admin_delete_logs_the_reason():
    client, person, identity = member_with_data()
    staff = APIClient()
    staff.force_authenticate(Identity.objects.create_user(email='admin@example.test', is_staff=True))
    response = staff.delete(
        f'/api/v1/patient-records/{person.person_id}/admin-delete/', {'confirm': 'DELETE'}, format='json',
    )
    assert response.status_code == 200, response.data
    assert_gone(person.person_id, identity.pk)
    assert DeletionLogEntry.objects.get().reason == DeletionLogEntry.ADMIN


# ------------------------------------------------------------------ the log


def test_log_is_append_only_and_refuses_an_unknown_reason():
    with pytest.raises(ValueError):
        svc.delete_member(PersonFactory(), reason='whim')
    entry = svc.delete_member(PersonFactory(), reason=DeletionLogEntry.INACTIVITY)
    with pytest.raises(ValidationError):
        entry.delete()
    with pytest.raises(ValidationError):
        DeletionLogEntry.objects.filter(pk=entry.pk).update(reason='admin')
    entry.reason = 'admin'
    with pytest.raises(ValidationError):
        entry.save()


def test_chain_verifies_and_detects_a_tampered_or_removed_row():
    for _ in range(3):
        svc.delete_member(PersonFactory(), reason=DeletionLogEntry.ACCOUNT_CLOSED)
    first, second, third = DeletionLogEntry.objects.order_by('id')
    assert second.chain_hash == second.compute_chain_hash(first.chain_hash)
    assert svc.verify_deletion_log().ok

    with connection.cursor() as cur:
        cur.execute('UPDATE deletion_log_entry SET reason = %s WHERE id = %s', ['admin', second.pk])
    report = svc.verify_deletion_log()
    assert report.altered == [second.pk]
    assert report.broken == []

    with connection.cursor() as cur:
        cur.execute('UPDATE deletion_log_entry SET reason = %s, chain_hash = %s WHERE id = %s',
                    ['account_closed', 'f' * 64, second.pk])
    report = svc.verify_deletion_log()
    assert report.altered == []
    assert report.broken == [second.pk, third.pk]

    with connection.cursor() as cur:
        cur.execute('DELETE FROM deletion_log_entry WHERE id = %s', [second.pk])
    report = svc.verify_deletion_log()
    assert report.altered == []
    assert report.broken == [third.pk]


# --------------------------------------------------------------- the replay


def restored(person_id, identity_id, created_at=None):
    """What a backup restore brings back: the person, record and login,
    created before the deletion unless ``created_at`` says otherwise."""
    record = PatientRecordFactory(person=PersonFactory(person_id=person_id))
    identity = Identity.objects.create_user(email=f'back{person_id}@example.test')
    Identity.objects.filter(pk=identity.pk).update(id=identity_id, created_at=created_at or EARLIER)
    identity = Identity.objects.get(pk=identity_id)
    link = PatientUser.objects.create(identity=identity, person=record.person)
    PatientUser.objects.filter(pk=link.pk).update(created_at=created_at or EARLIER)
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(person=record.person)
    return record


def test_replay_removes_a_person_the_restore_brought_back():
    client, person, identity = member_with_data()
    person_id, identity_id = person.person_id, identity.pk
    assert client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json').status_code == 200
    restored(person_id, identity_id)
    untouched = PatientRecordFactory()

    report = svc.replay(dry_run=True)
    assert (report.persons_deleted, report.identities_deleted) == ([person_id], [identity_id])
    assert Person.objects.filter(person_id=person_id).exists()

    report = svc.replay()
    assert (report.persons_deleted, report.identities_deleted) == ([person_id], [identity_id])
    assert_gone(person_id, identity_id)
    assert Person.objects.filter(person_id=untouched.person.person_id).exists()
    assert DeletionLogEntry.objects.count() == 1
    assert svc.replay().persons_deleted == []


def test_replay_skips_a_reused_id_held_by_a_newer_account():
    client, person, identity = member_with_data()
    person_id, identity_id = person.person_id, identity.pk
    assert client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json').status_code == 200
    # A sign-up after the deletion that the restored sequences handed both ids.
    newcomer = restored(person_id, identity_id, created_at=timezone.now() + timedelta(seconds=1))

    report = svc.replay()

    assert (report.persons_deleted, report.identities_deleted) == ([], [])
    assert len(report.skipped) == 2
    assert Person.objects.filter(person_id=person_id).exists()
    assert Identity.objects.filter(pk=identity_id).exists()
    assert PatientRecord.objects.filter(pk=newcomer.pk).exists()


def test_replay_command_reports_and_refuses_a_broken_chain(capsys):
    client, person, identity = member_with_data()
    person_id = person.person_id
    assert client.delete('/api/v1/phr/account/', {'confirm': 'DELETE'}, format='json').status_code == 200
    restored(person_id, identity.pk)

    with pytest.raises(SystemExit) as exc:
        call_command('replay_deletion_log', '--verify-only')
    assert exc.value.code == 0

    call_command('replay_deletion_log', '--dry-run')
    assert Person.objects.filter(person_id=person_id).exists()
    assert 'would delete 1 person(s)' in capsys.readouterr().out

    with connection.cursor() as cur:
        cur.execute('UPDATE deletion_log_entry SET reason = %s', ['admin'])
    with pytest.raises(SystemExit) as exc:
        call_command('replay_deletion_log')
    assert exc.value.code == 1
    assert Person.objects.filter(person_id=person_id).exists()

    call_command('replay_deletion_log', '--ignore-chain')
    assert not Person.objects.filter(person_id=person_id).exists()
    assert 'deleted 1 person(s)' in capsys.readouterr().out
