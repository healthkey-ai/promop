"""Deleting patient invitations that were never accepted (#2786).

An invitation holds an email address and a token, and may put the address on
the person it was sent for (``_issue_invitation``). When it is never accepted,
a deployment's privacy policy may promise to delete that after a while:
``PATIENT_INVITATION_RETENTION_DAYS`` days after it was last sent, once it
has expired or been cancelled. ``purge_unaccepted_invitations`` runs this
daily and writes one audit event per run.

What goes: the invitation row, and what it did to the person, undone: when it
put its address on a person who never made an account, the address it
replaced comes back (or none). An address the record held before the
invitation is the hospital's and stays; so does an address another invitation
not being deleted was sent to. The person stays: in a hospital deployment
they can be a real patient with a record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from omop_core.models import PatientRecord, Person
from patient_portal.models import AuditEvent, PatientInvitation, PatientUser


@dataclass
class Purged:
    invitations: int = 0
    emails_cleared: int = 0
    persons: list[int] = field(default_factory=list)


def stale_invitations(days: int, now=None):
    """Invitations never accepted, last sent more than ``days`` days ago, and
    no longer usable (expired or cancelled)."""
    now = now or timezone.now()
    return PatientInvitation.objects.filter(
        Q(expires_at__lt=now) | Q(cancelled_at__isnull=False),
        accepted_at__isnull=True,
        sent_at__lt=now - timedelta(days=days),
    )


def purge_unaccepted_invitations(days: int, *, now=None, dry_run: bool = False) -> Purged:
    """Delete stale invitations and undo the address they put on persons who
    never made an account. ``dry_run`` counts without changing anything."""
    now = now or timezone.now()
    with transaction.atomic():
        # Locked, and deleted only if still stale: a resend between the read
        # and the delete keeps its row and its address.
        stale = list(stale_invitations(days, now).select_for_update().order_by('sent_at', 'id'))
        if not stale:
            return Purged()
        ids = [i.id for i in stale]
        person_ids = {i.person_id for i in stale}
        with_account = set(PatientUser.objects.filter(person_id__in=person_ids).values_list('person_id', flat=True))
        kept = {
            (person_id, email.lower())
            for person_id, email in PatientInvitation.objects.filter(person_id__in=person_ids, accepted_at__isnull=True)
            .exclude(id__in=ids).values_list('person_id', 'email')
        }
        # Per person, what to put back: the earliest stale invitation that set
        # the address knows what was there before any of them.
        restore: dict[int, tuple[str, str | None]] = {}
        for inv in stale:
            if (not inv.set_person_email or inv.person_id in with_account or inv.person_id in restore
                    or (inv.person_id, inv.email.lower()) in kept):
                continue
            restore[inv.person_id] = (inv.email, inv.replaced_email)
        persons = {
            p.person_id: p for p in Person.objects.select_for_update().filter(person_id__in=restore)
            if p.email and p.email.lower() == restore[p.person_id][0].lower()
        }
        result = Purged(invitations=len(ids), emails_cleared=len(persons), persons=sorted(persons))
        if dry_run:
            transaction.set_rollback(True)
            return result
        for person_id in persons:
            previous = restore[person_id][1]
            Person.objects.filter(person_id=person_id).update(email=previous)
            PatientRecord.objects.filter(person_id=person_id).update(email=previous)
        stale_invitations(days, now).filter(id__in=ids).delete()
        AuditEvent.objects.create(
            event_type=AuditEvent.EVENT_ADMIN, method='COMMAND', path='manage.py purge_unaccepted_invitations',
            user_id='system', detail={
                'invitations_deleted': result.invitations, 'emails_restored': result.emails_cleared,
                'person_ids': result.persons, 'retention_days': days, 'now': now.isoformat(),
            },
        )
    return result
