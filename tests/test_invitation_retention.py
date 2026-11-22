"""Patient invitations never accepted are deleted after the retention period (#2786)."""
import secrets
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from omop_core.models import PatientRecord
from patient_portal.invitation_retention import purge_unaccepted_invitations
from patient_portal.models import Identity, PatientInvitation, PatientUser
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

NOW = timezone.now()


def invited(email='rae@example.test', *, sent_days_ago=40, ttl_days=30, accepted=False, cancelled=False,
            person=None):
    """A person with an invitation sent ``sent_days_ago`` days ago, the address on them and their record."""
    if person is None:
        record = PatientRecordFactory()
        person = record.person
    person.email = email
    person.save(update_fields=['email'])
    PatientRecord.objects.filter(person=person).update(email=email)
    sent = NOW - timedelta(days=sent_days_ago)
    invitation = PatientInvitation.objects.create(
        person=person, email=email, token=secrets.token_hex(32),
        expires_at=sent + timedelta(days=ttl_days),
        accepted_at=sent + timedelta(days=1) if accepted else None,
        cancelled_at=sent + timedelta(days=1) if cancelled else None,
    )
    PatientInvitation.objects.filter(pk=invitation.pk).update(created_at=sent)
    return person


def address(person):
    person.refresh_from_db()
    return person.email, PatientRecord.objects.get(person=person).email


def test_an_invitation_never_accepted_goes_after_the_period_with_its_address():
    person = invited()

    purged = purge_unaccepted_invitations(30, now=NOW)

    assert (purged.invitations, purged.emails_cleared) == (1, 1)
    assert not PatientInvitation.objects.filter(person=person).exists()
    assert address(person) == (None, None)
    # The person stays: only what the invitation added goes.
    assert PatientRecord.objects.filter(person=person).exists()


def test_what_is_recent_still_usable_or_accepted_stays():
    recent = invited('recent@example.test', sent_days_ago=10)
    usable = invited('usable@example.test', sent_days_ago=40, ttl_days=60)  # sent long ago, still valid
    joined = invited('joined@example.test', accepted=True)

    assert purge_unaccepted_invitations(30, now=NOW).invitations == 0
    for person in (recent, usable, joined):
        assert PatientInvitation.objects.filter(person=person).exists()
        assert address(person)[0] is not None


def test_a_cancelled_invitation_goes_once_the_period_has_passed():
    person = invited(cancelled=True, ttl_days=90)

    assert purge_unaccepted_invitations(30, now=NOW).invitations == 1
    assert address(person) == (None, None)


def test_a_person_with_an_account_keeps_their_address():
    person = invited()
    identity = Identity.objects.create_user(email='rae@example.test')
    PatientUser.objects.create(identity=identity, person=person)

    purged = purge_unaccepted_invitations(30, now=NOW)

    assert (purged.invitations, purged.emails_cleared) == (1, 0)
    assert address(person) == ('rae@example.test', 'rae@example.test')


def test_an_address_the_record_holds_differently_is_not_the_invitations():
    person = invited()
    person.email = 'hospital@example.test'
    person.save(update_fields=['email'])

    assert purge_unaccepted_invitations(30, now=NOW).emails_cleared == 0
    person.refresh_from_db()
    assert person.email == 'hospital@example.test'


def test_a_dry_run_changes_nothing():
    person = invited()

    purged = purge_unaccepted_invitations(30, now=NOW, dry_run=True)

    assert (purged.invitations, purged.emails_cleared) == (1, 1)
    assert PatientInvitation.objects.filter(person=person).exists()
    assert address(person)[0] == 'rae@example.test'


def test_the_command_reads_the_setting_and_keeps_everything_at_zero(settings):
    invited()
    out = StringIO()
    settings.PATIENT_INVITATION_RETENTION_DAYS = 0
    call_command('purge_unaccepted_invitations', stdout=out)
    assert 'kept' in out.getvalue() and PatientInvitation.objects.count() == 1

    settings.PATIENT_INVITATION_RETENTION_DAYS = 30
    call_command('purge_unaccepted_invitations', '--dry-run', stdout=out)
    assert 'Would delete 1 invitation(s)' in out.getvalue() and PatientInvitation.objects.count() == 1
    call_command('purge_unaccepted_invitations', stdout=out)
    assert 'Deleted 1 invitation(s)' in out.getvalue() and PatientInvitation.objects.count() == 0
