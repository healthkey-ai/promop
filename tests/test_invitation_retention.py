"""Patient invitations never accepted are deleted after the retention period (#2786)."""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, Organization, PatientRecord
from patient_portal.invitation_retention import purge_unaccepted_invitations
from patient_portal.models import AuditEvent, Identity, PatientInvitation, PatientUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(settings):
    settings.INVITE_TTL_DAYS = 7
    org = Organization.objects.create(name='Hosp', slug='hosp')
    admin = Identity.objects.create_user(email='admin@hosp.test', password='pw-Strong-12345')
    admin.mark_email_verified()
    GroupAccess.objects.create(identity=admin, org=org, role='org_admin')
    client = APIClient()
    client.force_authenticate(admin)
    return client


def create_and_invite(staff, email='rae@example.test'):
    """A patient created and invited by staff: the invitation puts the address on them."""
    r = staff.post('/api/v1/patients/', {'given_name': 'Rae', 'email': email, 'invite': True}, format='json')
    assert r.status_code == 201, r.data
    return PatientRecord.objects.get(person_id=r.data['person_id']).person


def invite(staff, person, email=None):
    r = staff.post(f'/api/v1/patients/{person.person_id}/invite/', {'email': email} if email else {}, format='json')
    assert r.status_code == 201, r.data
    return PatientInvitation.objects.get(person=person, accepted_at__isnull=True, cancelled_at__isnull=True)


def age(person, days, *, cancel=False):
    """Every invitation of ``person`` last sent ``days`` days ago, past its 7 days (or cancelled)."""
    sent = timezone.now() - timedelta(days=days)
    PatientInvitation.objects.filter(person=person).update(
        sent_at=sent, expires_at=sent + timedelta(days=7), **({'cancelled_at': sent} if cancel else {}))


def address(person):
    person.refresh_from_db()
    return person.email, PatientRecord.objects.get(person=person).email


def test_an_invitation_never_accepted_goes_with_the_address_it_added(staff):
    person = create_and_invite(staff)
    age(person, 40)

    purged = purge_unaccepted_invitations(30)

    assert (purged.invitations, purged.emails_cleared) == (1, 1)
    assert not PatientInvitation.objects.filter(person=person).exists()
    assert address(person) == (None, None)
    assert PatientRecord.objects.filter(person=person).exists()  # the person stays
    event = AuditEvent.objects.get(path='manage.py purge_unaccepted_invitations')
    assert event.detail['person_ids'] == [person.person_id] and event.detail['invitations_deleted'] == 1


def test_an_address_the_hospital_recorded_stays(staff):
    """Review of #2787: an invitation sent to the address on record did not add it."""
    person = create_and_invite(staff, 'other@example.test')
    PatientInvitation.objects.filter(person=person).delete()
    person.email = 'jane@hospital.test'
    person.save(update_fields=['email'])
    invite(staff, person)  # no address given: the one on record
    age(person, 40)

    purged = purge_unaccepted_invitations(30)

    assert (purged.invitations, purged.emails_cleared) == (1, 0)
    assert address(person)[0] == 'jane@hospital.test'


def test_an_address_an_invitation_replaced_comes_back(staff):
    person = create_and_invite(staff, 'first@example.test')
    PatientInvitation.objects.filter(person=person).delete()
    person.email = 'jane@hospital.test'
    person.save(update_fields=['email'])
    invite(staff, person, 'new@example.test')
    age(person, 40)

    assert purge_unaccepted_invitations(30).emails_cleared == 1
    assert address(person)[0] == 'jane@hospital.test'


def test_retention_counts_from_the_last_send(staff):
    """Review of #2787: a resend reuses the row, and the clock starts again."""
    person = create_and_invite(staff)
    age(person, 400)
    invite(staff, person)  # resent today
    PatientInvitation.objects.filter(person=person).update(expires_at=timezone.now() - timedelta(days=1))

    assert purge_unaccepted_invitations(30).invitations == 0


def test_another_invitation_to_the_address_keeps_it(staff):
    """Review of #2787: a cancelled old one goes; a newer one, lapsed but kept, still holds the address."""
    person = create_and_invite(staff)
    old = PatientInvitation.objects.get(person=person)
    PatientInvitation.objects.filter(pk=old.pk).update(
        cancelled_at=timezone.now() - timedelta(days=60), sent_at=timezone.now() - timedelta(days=60))
    newer = invite(staff, person)
    PatientInvitation.objects.filter(pk=newer.pk).update(
        sent_at=timezone.now() - timedelta(days=10), expires_at=timezone.now() - timedelta(days=3))

    purged = purge_unaccepted_invitations(30)

    assert (purged.invitations, purged.emails_cleared) == (1, 0)
    assert address(person)[0] == 'rae@example.test'
    assert PatientInvitation.objects.filter(pk=newer.pk).exists()


def test_what_is_recent_still_usable_or_accepted_stays(staff):
    recent = create_and_invite(staff, 'recent@example.test')
    age(recent, 10)
    usable = create_and_invite(staff, 'usable@example.test')
    PatientInvitation.objects.filter(person=usable).update(sent_at=timezone.now() - timedelta(days=40))
    joined = create_and_invite(staff, 'joined@example.test')
    age(joined, 40)
    PatientInvitation.objects.filter(person=joined).update(accepted_at=timezone.now() - timedelta(days=35))

    assert purge_unaccepted_invitations(30).invitations == 0


def test_a_cancelled_invitation_goes_once_the_period_has_passed(staff):
    person = create_and_invite(staff)
    age(person, 40, cancel=True)

    assert purge_unaccepted_invitations(30).invitations == 1
    assert address(person) == (None, None)


def test_a_person_with_an_account_keeps_their_address(staff):
    person = create_and_invite(staff)
    age(person, 40)
    PatientUser.objects.create(identity=Identity.objects.create_user(email='rae@example.test'), person=person)

    purged = purge_unaccepted_invitations(30)

    assert (purged.invitations, purged.emails_cleared) == (1, 0)
    assert address(person)[0] == 'rae@example.test'


def test_a_dry_run_changes_nothing(staff):
    person = create_and_invite(staff)
    age(person, 40)

    purged = purge_unaccepted_invitations(30, dry_run=True)

    assert (purged.invitations, purged.emails_cleared) == (1, 1)
    assert PatientInvitation.objects.filter(person=person).exists()
    assert address(person)[0] == 'rae@example.test'
    assert not AuditEvent.objects.filter(path='manage.py purge_unaccepted_invitations').exists()


def test_the_command_reads_the_setting_and_keeps_everything_at_zero(staff, settings):
    person = create_and_invite(staff)
    age(person, 40)
    out = StringIO()
    settings.PATIENT_INVITATION_RETENTION_DAYS = 0
    call_command('purge_unaccepted_invitations', stdout=out)
    assert 'kept' in out.getvalue() and PatientInvitation.objects.count() == 1

    settings.PATIENT_INVITATION_RETENTION_DAYS = 30
    call_command('purge_unaccepted_invitations', '--dry-run', stdout=out)
    assert 'Would delete 1 invitation(s)' in out.getvalue() and PatientInvitation.objects.count() == 1
    call_command('purge_unaccepted_invitations', stdout=out)
    assert 'Deleted 1 invitation(s)' in out.getvalue() and PatientInvitation.objects.count() == 0
