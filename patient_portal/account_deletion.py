"""Deleting a member's account and data, and the log that lets a restore
replay it.

One service, ``delete_member``, serves every path that removes a patient:
the member closing their account (``DELETE /api/v1/phr/account/`` and the
legacy ``DELETE /api/patient-info/me/``), withdrawing the "my record"
permission (``api/phr/consent.py``), and an administrator's
``admin-delete``. In one transaction it removes the PROlog survey rows, the
Person (which cascades to the OMOP rows, the PatientRecord, the PatientUser
and the consent history), the login Identity when one is handed in, and
writes a ``DeletionLogEntry``. The ``ConsentLedgerEntry`` rows, which hold
no link to the account, stay.

The log holds identifiers and a time, never content. A backup taken before
a deletion restores the rows; ``replay`` removes them again from the log.
See docs/account-deletion.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction

from omop_core.models import GroupAccess, Person
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.prolog_cleanup import delete_prolog_data_for_persons
from patient_portal.models import DeletionLogEntry, Identity, PatientUser

logger = logging.getLogger(__name__)


def patient_only_identity(person) -> Identity | None:
    """The login linked to ``person`` when it is purely a patient login.

    An Identity that also holds provider access (GroupAccess) is kept, so
    deleting a patient never revokes a provider.
    """
    link = PatientUser.objects.filter(person=person).select_related('identity').first()
    if link is None or GroupAccess.objects.filter(identity=link.identity).exists():
        return None
    return link.identity


def _delete_person_rows(person) -> None:
    """Remove ``person`` and everything that hangs off it. Inside a transaction."""
    # EpisodeEvent holds a bare integer episode id, which no CASCADE covers.
    episode_ids = list(Episode.objects.filter(person=person).values_list('episode_id', flat=True))
    if episode_ids:
        EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()
    # A survey response protects its participant, so PROlog's rows go first
    # or person.delete() raises ProtectedError.
    delete_prolog_data_for_persons([person.person_id])
    person.delete()


def delete_member(person, *, reason: str, identity=None) -> DeletionLogEntry:
    """Delete ``person``, their data and (when given) their ``identity``, and
    log it, all in one transaction. Returns the log entry.

    ``reason`` is one of ``DeletionLogEntry.REASONS``. ``identity`` is the
    login to delete with the person: the member's own on a self-deletion,
    ``patient_only_identity(person)`` on an administrator's.
    """
    if reason not in dict(DeletionLogEntry.REASONS):
        raise ValueError(f'Unknown deletion reason {reason!r}.')
    person_id = person.person_id
    identity_id = identity.pk if identity is not None else None
    with transaction.atomic():
        _delete_person_rows(person)
        if identity is not None:
            identity.delete()
        entry = DeletionLogEntry.objects.create(person_id=person_id, identity_id=identity_id, reason=reason)
    logger.info(
        'member_deleted person_id=%s identity_id=%s reason=%s log_entry=%s',
        person_id, identity_id, reason, entry.pk,
    )
    return entry


# ------------------------------------------------------------------ the log


@dataclass
class ChainReport:
    """What ``verify_deletion_log`` found. ``ok`` when both lists are empty."""
    scanned: int = 0
    altered: list[int] = field(default_factory=list)  # signature no longer matches the row
    broken: list[int] = field(default_factory=list)   # chain_hash does not follow its predecessor

    @property
    def ok(self) -> bool:
        return not self.altered and not self.broken


def verify_deletion_log(entries=None) -> ChainReport:
    """Recompute every entry's signature and chain link, in id order.

    ``entries`` defaults to the whole table; an iterable of entries (for
    example read back from an export) can be verified the same way. The
    first entry is the anchor: its predecessor is the empty string, as it
    was when the chain began.
    """
    if entries is None:
        entries = DeletionLogEntry.objects.order_by('id').iterator(chunk_size=2000)
    report = ChainReport()
    prev = ''
    for entry in entries:
        report.scanned += 1
        if not entry.signature_valid():
            report.altered.append(entry.pk)
        if entry.chain_hash != entry.compute_chain_hash(prev):
            report.broken.append(entry.pk)
        prev = entry.chain_hash
    return report


@dataclass
class ReplayReport:
    entries: int = 0
    persons_deleted: list[int] = field(default_factory=list)
    identities_deleted: list[int] = field(default_factory=list)


def replay(entries=None, *, dry_run: bool = False) -> ReplayReport:
    """Re-apply the log against a restored database: delete every Person and
    Identity an entry names that exists again. Writes no new log entries;
    the existing ones already record these deletions.

    Each entry is applied in its own transaction so a failure on one leaves
    the others done. Run this immediately after the restore, before any new
    account could take an id the log names.
    """
    if entries is None:
        entries = DeletionLogEntry.objects.order_by('id').iterator(chunk_size=2000)
    report = ReplayReport()
    for entry in entries:
        report.entries += 1
        person = Person.objects.filter(person_id=entry.person_id).first()
        identity = Identity.objects.filter(pk=entry.identity_id).first() if entry.identity_id else None
        if person is None and identity is None:
            continue
        if person is not None:
            report.persons_deleted.append(entry.person_id)
        if identity is not None:
            report.identities_deleted.append(entry.identity_id)
        if dry_run:
            continue
        with transaction.atomic():
            if person is not None:
                _delete_person_rows(person)
            if identity is not None:
                identity.delete()
        logger.info(
            'deletion_log_replayed entry=%s person_id=%s identity_id=%s',
            entry.pk, entry.person_id if person else None, entry.identity_id if identity else None,
        )
    return report
