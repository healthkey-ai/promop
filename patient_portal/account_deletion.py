"""Deleting a member's account and data, and the log that lets a restore
replay it.

One service, ``delete_member``, serves every path that removes a patient:
the member closing their account (``DELETE /api/v1/phr/account/`` and the
legacy ``DELETE /api/patient-info/me/``), withdrawing the "my record"
permission (``api/phr/consent.py``), and an administrator's
``admin-delete``. In one transaction it removes the rows no cascade reaches
(``patient_cleanup.delete_omop_clinical_rows``: PROlog survey rows, episode
events, measurement ownership, group memberships, representatives; plus
break-glass grants and provenance), the Person (which cascades to the
remaining OMOP rows, the PatientRecord, the PatientUser and the consent
history), the login Identity when one is handed in, and writes a
``DeletionLogEntry``. The ``ConsentLedgerEntry`` rows, which hold no link to
the account, stay.

The log holds identifiers and a time, never content. A backup taken before
a deletion restores the rows; ``replay`` removes them again from the log.
See docs/account-deletion.md.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import logout
from django.db import transaction
from django.utils import timezone

from omop_core.models import PatientRecord, Person, ProvenanceRecord
from omop_core.services.patient_cleanup import delete_omop_clinical_rows, delete_provenance_for
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import BreakGlassGrant, DeletionLogEntry, Identity, PatientUser
from patient_portal.services import has_provider_access

logger = logging.getLogger(__name__)


class AlreadyDeleted(Person.DoesNotExist):
    """The person was deleted by a request that committed first."""


def patient_only_identity(person) -> Identity | None:
    """The login linked to ``person`` when it is purely a patient login.

    An Identity that is staff or holds provider access (``has_provider_access``,
    the test ``patient_person_for`` applies) is kept, so deleting a patient
    never revokes a provider: the person and the PatientUser link go, the
    login stays.
    """
    link = PatientUser.objects.filter(person=person).select_related('identity').first()
    if link is None:
        return None
    identity = link.identity
    if has_provider_access(identity):
        return None
    return identity


LOCAL_ISSUER = 'urn:local'


def subject_hash_of(identity) -> str:
    """SHA-256 over ``issuer|sub`` of a partner login; empty for a local one.

    Deleting the Identity does not revoke the provider's token, and a
    Firebase client refreshes its id token without the person signing in
    again, so the next request would provision a fresh account for the same
    subject. The log keeps this hash and ``PartnerAuthentication`` refuses to
    provision the subject again within ``ACCOUNT_CLOSED_BLOCK_SECONDS``.
    """
    if identity is None or not identity.issuer or identity.issuer == LOCAL_ISSUER:
        return ''
    return hashlib.sha256(f'{identity.issuer}|{identity.sub}'.encode()).hexdigest()


def account_closed_recently(issuer: str, sub: str) -> bool:
    """Whether a login for this partner subject was deleted within the block window."""
    if not issuer or issuer == LOCAL_ISSUER:
        return False
    window = timedelta(seconds=settings.ACCOUNT_CLOSED_BLOCK_SECONDS)
    digest = hashlib.sha256(f'{issuer}|{sub}'.encode()).hexdigest()
    return DeletionLogEntry.objects.filter(
        subject_hash=digest, deleted_at__gte=timezone.now() - window,
    ).exists()


def _delete_person_rows(person) -> None:
    """Remove ``person`` and everything that hangs off it. Inside a transaction."""
    person_id = person.person_id
    # PROlog rows (a response PROTECTs its participant), episode events, and
    # the access rows keyed by a bare person_id that would otherwise outlive
    # the person and grant their old representatives and groups access to
    # whoever gets the id next. The clinical rows' post_delete receivers would
    # re-derive the PatientRecord once per row, for a record about to go.
    with suppress_patient_record_refresh():
        delete_omop_clinical_rows(person)
        BreakGlassGrant.objects.filter(person_id=person_id).delete()
        ProvenanceRecord.objects.filter(target_patient_id=str(person_id)).delete()
        delete_provenance_for(PatientRecord, PatientRecord.objects.filter(person_id=person_id).values_list('pk', flat=True))
        delete_provenance_for(Person, [person_id])
        person.delete()


def delete_member(person, *, reason: str, identity=None) -> DeletionLogEntry:
    """Delete ``person``, their data and (when given) their ``identity``, and
    log it, all in one transaction. Returns the log entry.

    ``reason`` is one of ``DeletionLogEntry.REASONS``. ``identity`` is the
    login to delete with the person, normally ``patient_only_identity(person)``.
    Raises ``AlreadyDeleted`` when another request deleted the person first,
    so a double submit never logs a deletion that removed nothing.
    """
    if reason not in dict(DeletionLogEntry.REASONS):
        raise ValueError(f'Unknown deletion reason {reason!r}.')
    person_id = person.person_id
    identity_id = identity.pk if identity is not None else None
    subject_hash = subject_hash_of(identity)
    with transaction.atomic():
        if not Person.objects.select_for_update().filter(pk=person_id).exists():
            raise AlreadyDeleted(f'Person {person_id} is already deleted.')
        _delete_person_rows(person)
        if identity is not None:
            identity.delete()
        entry = DeletionLogEntry.objects.create(
            person_id=person_id, identity_id=identity_id, reason=reason, subject_hash=subject_hash,
        )
    logger.info(
        'member_deleted person_id=%s identity_id=%s reason=%s log_entry=%s',
        person_id, identity_id, reason, entry.pk,
    )
    return entry


# ------------------------------------------------------------ the request


def actor_snapshot(user) -> SimpleNamespace:
    """What the audit trail needs of the caller, taken before the deletion:
    after ``identity.delete()`` the instance has no pk, and after ``logout``
    the request has no user."""
    return SimpleNamespace(pk=user.pk, email=getattr(user, 'email', '') or '', is_authenticated=True)


def delete_own_account(request, person, *, reason: str) -> DeletionLogEntry:
    """The self-service choreography: snapshot the caller for the audit trail,
    delete the person with their patient-only login, then end the session.
    Raises ``AlreadyDeleted``; the view maps it to its 404.
    """
    actor = actor_snapshot(request.user)
    person_id = person.person_id  # the instance loses its pk on delete
    identity = patient_only_identity(person)
    entry = delete_member(person, reason=reason, identity=identity)
    end_session(request, actor, person_id, identity)
    return entry


def end_session(request, actor, person_id: int, deleted_identity) -> None:
    """After the deletion committed: hand the audit middleware the actor and
    the person it acted on, and log the caller out when a login was deleted
    (on the self paths that is always the caller's own). A login kept for
    its provider access stays signed in.

    Only after the commit: a rolled-back deletion must not have flushed the
    session.
    """
    http_request = getattr(request, '_request', request)
    http_request.audit_user = actor
    http_request.audit_resource_id = str(person_id)
    if deleted_identity is not None:
        logout(request)


# ------------------------------------------------------------------ the log


@dataclass
class ChainReport:
    """What ``verify_deletion_log`` found. ``ok`` when every list is empty."""
    scanned: int = 0
    altered: list[int] = field(default_factory=list)  # signature no longer matches the row
    broken: list[int] = field(default_factory=list)   # chain_hash does not follow its predecessor
    unverifiable: list[int] = field(default_factory=list)  # signed with a key no longer configured

    @property
    def ok(self) -> bool:
        return not self.altered and not self.broken and not self.unverifiable


def verify_deletion_log(entries=None) -> ChainReport:
    """Recompute every entry's signature and chain link, in id order.

    ``entries`` defaults to the whole table; an iterable of entries (for
    example read back from an export) can be verified the same way. The
    first entry is the anchor: its predecessor is the empty string, as it
    was when the chain began, so the table is never pruned.

    A row whose fields changed is ``altered`` (and ``broken``, since its
    signature feeds its link); a row whose ``chain_hash`` alone changed, or
    that follows a removed or inserted row, is ``broken``, as is the row
    after it, which was linked to the hash as it stood. A row signed with a
    key that is neither ``AUDIT_HMAC_KEY`` nor in ``AUDIT_HMAC_PREVIOUS_KEYS``
    is ``unverifiable``: supply the key, do not conclude tampering.
    """
    if entries is None:
        entries = DeletionLogEntry.objects.order_by('id').iterator(chunk_size=2000)
    report = ChainReport()
    prev = ''
    for entry in entries:
        report.scanned += 1
        if not entry.key_known:
            report.unverifiable.append(entry.pk)
            prev = entry.chain_hash
            continue
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
    skipped: list[str] = field(default_factory=list)  # ids held by rows newer than the deletion


def _newer_than(entry, person, identity) -> list[str]:
    """Rows that carry a logged id but were created after the deletion: a
    new account the restored sequence handed the id to, never the one the
    entry names. Person has no timestamp, so its link is the evidence."""
    reasons = []
    if identity is not None and identity.created_at > entry.deleted_at:
        reasons.append(f'identity {identity.pk} created after entry {entry.pk}')
    if person is not None:
        link = PatientUser.objects.filter(person=person).only('created_at').first()
        if link is not None and link.created_at > entry.deleted_at:
            reasons.append(f'person {person.person_id} linked to a login created after entry {entry.pk}')
    return reasons


def replay(entries=None, *, dry_run: bool = False) -> ReplayReport:
    """Re-apply the log against a restored database: delete every Person and
    Identity an entry names that exists again. Writes no new log entries;
    the existing ones already record these deletions.

    A row created after the entry's time is a new account that was handed a
    logged id, so it is skipped and reported, not deleted. Each entry is
    applied in its own transaction so a failure on one leaves the others
    done. Run this immediately after the restore all the same.
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
        newer = _newer_than(entry, person, identity)
        if newer:
            report.skipped.extend(newer)
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
