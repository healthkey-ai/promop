"""Erasing again after a backup restore (docs/member_erasure_architecture.md).

A database restored from a backup has every member who closed their account,
or withdrew their record's permission, since the backup was taken; and the
audit rows that recorded those erasures are lost with everything else written
after it. So the deletion log is exported to somewhere outside the database
(``export``), and after a restore the export says what to erase again
(``replay``).

An entry is acted on only when its signature verifies under this
deployment's audit key, so a file cannot be made up to erase somebody; only
when the restored database does not already hold it, since an erasure from
before the backup was already applied to what the backup holds; and never
against a row made after the entry, which holds a reused id.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from omop_core.models import Person
from patient_portal.models import AuditEvent, ConsentEvent, ConsentPurpose, Identity, PatientUser

# What an audit row's signature covers (AuditEvent.compute_signature).
_SIGNED = (
    'timestamp', 'event_type', 'method', 'path', 'status_code', 'user_id', 'user_email',
    'client_id', 'resource_id', 'ip_address', 'duration_ms', 'detail',
)
_PATH = 'patient_portal.erasure:'
_KINDS = ('account', 'record')


def _log_rows():
    return AuditEvent.objects.filter(method='ERASE', path__startswith=_PATH)


def export(since: datetime | None = None) -> Iterator[str]:
    """The deletion log as JSON lines, oldest first: each row with what its
    signature covers, and the signature."""
    rows = _log_rows().order_by('id')
    if since is not None:
        rows = rows.filter(timestamp__gte=since)
    for row in rows.iterator(chunk_size=2000):
        line = {name: getattr(row, name) for name in _SIGNED}
        line['timestamp'] = row.timestamp.isoformat()
        line['signature'] = row.signature
        yield json.dumps(line, sort_keys=True)


def parse_time(text: str) -> datetime:
    """An ISO 8601 date or time from the command line, in this deployment's
    time zone when it names none. Raises ``ValueError``."""
    when = datetime.fromisoformat(text)
    return timezone.make_aware(when) if timezone.is_naive(when) else when


class BadExport(Exception):
    """The export holds a line that is not a signed erasure of this
    deployment's. ``lines`` are the 1-based line numbers."""

    def __init__(self, lines: list[int]):
        super().__init__(f'{len(lines)} line(s) do not verify')
        self.lines = lines


def read(lines: Iterable[str]) -> list[AuditEvent]:
    """The export's entries, unsaved. Raises ``BadExport`` when any line is
    not JSON, is not an erasure, or does not carry the signature this
    deployment's key gives its content."""
    entries, bad = [], []
    for number, text in enumerate(lines, 1):
        if not text.strip():
            continue
        try:
            entry = _entry(json.loads(text))
        except (ValueError, TypeError, KeyError):
            entry = None
        if entry is None or not entry.signature_valid():
            bad.append(number)
        else:
            entries.append(entry)
    if bad:
        raise BadExport(bad)
    return entries


def _entry(line) -> AuditEvent | None:
    if not isinstance(line, dict) or not isinstance(line.get('detail'), dict):
        return None
    if line.get('method') != 'ERASE' or not str(line.get('path', '')).startswith(_PATH):
        return None
    values = {name: line.get(name) for name in _SIGNED}
    values['timestamp'] = datetime.fromisoformat(line['timestamp'])
    return AuditEvent(signature=str(line['signature']), **values)


@dataclass
class Report:
    entries: int = 0
    known: int = 0        # the restored database already holds the entry, or its replay
    gone: int = 0         # nothing to erase: the person is not here
    accounts: list[int] = field(default_factory=list)   # person ids closed again
    records: list[int] = field(default_factory=list)    # person ids whose record was erased again
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (person id, why)
    older: int = 0        # entries from before ``since``, not looked at
    newest_older: datetime | None = None
    # Person ids whose record was erased again but whose withdrawal could not
    # be recorded: the permission still reads granted and needs staff.
    unrestored: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)  # (person id, exception class)


# Why an entry was left alone.
REUSED = 'the id belongs to an account made after the erasure'
UNLINKED = 'a person with no sign-in cannot be told from a reused id (--include-unlinked)'
ANSWERED = 'the member has answered this permission since'
NO_PURPOSE = 'the permission the entry names is not defined here'


def replay(entries: Iterable[AuditEvent], *, since: datetime, dry_run: bool = False,
           include_unlinked: bool = False) -> Report:
    """Erase again what ``entries`` say was erased and this database does not
    know about. Each entry is its own transaction, and leaves a row in the
    deletion log saying it was replayed, so a second run does nothing.
    ``since`` is when the backup was taken: older entries are counted and
    not looked at (the audit retention may have pruned them from the
    database, which would make them look lost). An entry that fails is rolled
    back, listed in ``failed`` and can be replayed again."""
    report = Report()
    known = set(_log_rows().values_list('signature', flat=True))
    known.update(
        _log_rows().filter(detail__erasure='replay').values_list('detail__replay_of', flat=True))
    for entry in entries:
        kind = entry.detail.get('erasure')
        if kind not in _KINDS:
            continue  # a replay's own marker, exported from an earlier restore
        if entry.timestamp < since:
            report.older += 1
            report.newest_older = max(report.newest_older or entry.timestamp, entry.timestamp)
            continue
        report.entries += 1
        if entry.signature in known:
            report.known += 1
            continue
        known.add(entry.signature)
        mark = _Mark(len(report.accounts), len(report.records), len(report.unrestored))
        try:
            with transaction.atomic():
                done = _replay_one(entry, kind, report, dry_run=dry_run, include_unlinked=include_unlinked)
                if done and not dry_run:
                    AuditEvent.objects.create(
                        event_type=AuditEvent.EVENT_DELETE, method='ERASE', path=f'{_PATH}replay',
                        user_id='system', resource_id=str(entry.detail.get('person_id')),
                        detail={'erasure': 'replay', 'person_id': entry.detail.get('person_id'),
                                'replay_of': entry.signature},
                    )
        except Exception as error:  # noqa: BLE001 — one member's failure must not stop the others' erasure
            # Rolled back: take it out of what the report says was done. The
            # class only; a message can quote a row.
            del report.accounts[mark.accounts:], report.records[mark.records:], report.unrestored[mark.unrestored:]
            report.failed.append((entry.detail.get('person_id'), type(error).__name__))
    return report


@dataclass
class _Mark:
    accounts: int
    records: int
    unrestored: int


def _replay_one(entry: AuditEvent, kind: str, report: Report, *, dry_run: bool, include_unlinked: bool) -> bool:
    person_id = entry.detail.get('person_id')
    person = Person.objects.select_for_update().filter(person_id=person_id).first() if isinstance(person_id, int) else None
    if person is None:
        report.gone += 1
        return False
    link = PatientUser.objects.filter(person=person).first()
    if link is None and not include_unlinked:
        report.skipped.append((person_id, UNLINKED))
        return False
    if link is not None and link.created_at > entry.timestamp:
        report.skipped.append((person_id, REUSED))
        return False
    if kind == 'account':
        return _close_again(entry, person, report, dry_run)
    return _erase_again(entry, person, report, dry_run)


def _close_again(entry: AuditEvent, person: Person, report: Report, dry_run: bool) -> bool:
    from patient_portal.erasure import close_account

    # The sign-in went with the account only when the entry names it; and it
    # goes again only while it is still this person's.
    identity = Identity.objects.filter(
        pk=entry.detail.get('identity_id'), created_at__lte=entry.timestamp, patient_user__person=person,
    ).first() if entry.detail.get('identity_id') else None
    report.accounts.append(person.person_id)
    if not dry_run:
        close_account(person, identity)
    return True


def _erase_again(entry: AuditEvent, person: Person, report: Report, dry_run: bool) -> bool:
    from patient_portal import consent
    from patient_portal.erasure import erase_record

    key = entry.detail.get('purpose') or ''
    purpose = ConsentPurpose.objects.filter(key=key).first() if key else None
    if key and purpose is None:
        # Without it nobody can say whether the member has answered since.
        report.skipped.append((person.person_id, NO_PURPOSE))
        return False
    latest = (
        ConsentEvent.objects.filter(person=person, purpose=purpose).order_by('-recorded_at', '-id').first()
        if purpose else None
    )
    if latest is not None and latest.recorded_at > entry.timestamp:
        report.skipped.append((person.person_id, ANSWERED))
        return False
    report.records.append(person.person_id)
    granted = latest is not None and latest.decision == ConsentEvent.GRANTED
    if dry_run:
        return True
    erased = False
    if granted:
        # The withdrawal was lost with the restore as well: put it back at
        # its own time. It ends the grant and what depends on it, and erases
        # the record when the purpose still says a withdrawal does.
        try:
            with transaction.atomic():
                consent.record_consent(
                    person, purpose, ConsentEvent.WITHDRAWN, source=ConsentEvent.SYSTEM, recorded_at=entry.timestamp)
            erased = purpose.erases_record_on_withdrawal
        except ValidationError:
            # No published text, or no address for the ledger: the record is
            # erased all the same, and the standing grant is reported.
            report.unrestored.append(person.person_id)
    if not erased:
        erase_record(person, purpose=key)
    return True
