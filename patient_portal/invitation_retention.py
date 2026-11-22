"""Deleting patient invitations that were never accepted (#2786).

An invitation holds an email address and a token, and puts the address on
the person it was sent for (``_issue_invitation``). When it is never accepted,
a deployment's privacy policy may promise to delete that after a while:
``PATIENT_INVITATION_RETENTION_DAYS`` days after it was sent, once it has
expired or been cancelled. ``purge_unaccepted_invitations`` runs this daily.

What goes: the invitation row, and the email on the person and their
patient record when it is the invitation's address and the person never made
an account. A later invitation still pending for that address keeps it. The
person stays: in a hospital deployment they can be a real patient with a
record, and only what the invitation added is removed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from omop_core.models import PatientRecord, Person
from patient_portal.models import PatientInvitation, PatientUser


@dataclass
class Purged:
    invitations: int = 0
    emails_cleared: int = 0


def stale_invitations(days: int, now=None):
    """Invitations never accepted, sent more than ``days`` days ago, and no
    longer usable (expired or cancelled)."""
    now = now or timezone.now()
    return PatientInvitation.objects.filter(
        Q(expires_at__lt=now) | Q(cancelled_at__isnull=False),
        accepted_at__isnull=True,
        created_at__lt=now - timedelta(days=days),
    )


def purge_unaccepted_invitations(days: int, *, now=None, dry_run: bool = False) -> Purged:
    """Delete stale invitations and the address they left on persons who never
    made an account. ``dry_run`` counts without changing anything."""
    now = now or timezone.now()
    stale = list(stale_invitations(days, now).values_list('id', 'person_id', 'email'))
    if not stale:
        return Purged()
    ids = [i for i, _, _ in stale]
    person_ids = {p for _, p, _ in stale}
    with_account = set(PatientUser.objects.filter(person_id__in=person_ids).values_list('person_id', flat=True))
    # A person invited again, with that invitation still usable, keeps the address.
    still_invited = {
        (person_id, email.lower())
        for person_id, email in PatientInvitation.objects.filter(
            person_id__in=person_ids, accepted_at__isnull=True, cancelled_at__isnull=True, expires_at__gte=now,
        ).exclude(id__in=ids).values_list('person_id', 'email')
    }
    clear: dict[int, set[str]] = {}
    for _, person_id, email in stale:
        if person_id in with_account or not email:
            continue
        if (person_id, email.lower()) in still_invited:
            continue
        clear.setdefault(person_id, set()).add(email.lower())
    # Only where the address held is the invitation's: an address the hospital
    # recorded differently is theirs, not the invitation's.
    persons = [p for p in Person.objects.filter(person_id__in=clear, email__isnull=False)
               if p.email.lower() in clear[p.person_id]]
    result = Purged(invitations=len(ids), emails_cleared=len(persons))
    if dry_run:
        return result
    with transaction.atomic():
        cleared = [p.person_id for p in persons]
        Person.objects.filter(person_id__in=cleared).update(email=None)
        PatientRecord.objects.filter(person_id__in=cleared).update(email=None)
        PatientInvitation.objects.filter(id__in=ids).delete()
    return result
