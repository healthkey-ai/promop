"""What a re-derivation would change, computed without keeping any of it.

``refresh_patient_record`` is the only definition of a derived value: the
extractors, the pending-edit rules, the lab aliases, ``PatientRecord.save``'s
own calculations (age, BMI, therapy counts, relapse) and the active field
formulas all run inside it, partly after the row is written. Re-implementing
that chain without the save would be a second derivation that drifts from the
first, so the preview runs the real refresh and throws the result away:

1. The refresh runs inside ``transaction.atomic()`` marked for rollback, and the
   record is read back from the database inside that transaction, so the
   "after" side is exactly what the refresh stored.
2. While it runs, every SQL statement on every connection passes a guard that
   allows reads, savepoints and an ``UPDATE`` of ``patient_record`` only. Any
   other write -- an OMOP row, a ``RecordRevision``, a projection, even an
   ``INSERT`` into ``patient_record`` (which would draw a sequence value, and
   sequences do not roll back) -- raises ``PreviewWriteBlocked`` before it
   reaches the database, and fails the preview even if the refresh catches it.
3. Publishing a Celery message raises the same way. ``on_commit`` callbacks
   need nothing extra: Django discards those registered in a block that rolls
   back.

The preview locks the record (``select_for_update``) before reading the stored
values, and the refresh re-takes that lock in the same transaction; it is held
only until the rollback, one person at a time, as the backfill holds it.
"""
from __future__ import annotations

import contextlib
import dataclasses
import decimal
import importlib.util
import re
from typing import Iterator
from unittest import mock

from django.db import connections, transaction

from omop_core.models import PatientRecord

# Bookkeeping that every refresh changes, or that refresh never sets. Reporting
# them would put a line on every record and say nothing about its values.
IGNORED_FIELDS = frozenset({
    'id', 'person', 'organization', 'derivation_version', 'derived_at',
    'created_at', 'updated_at',
})

# The preview prints no values, stored or re-derived: only which field of
# which person_id would change, and how (set, cleared, changed, and for a
# number whether it goes up or down). Three review rounds each found another
# column printing identifying text through a list of "safe" fields -- unknown
# diagnoses kept verbatim, race and stage source text, unchecked choices -- so
# the question of which values are safe to print is not answered here at all.
SET, CLEARED, CHANGED = 'set', 'cleared', 'changed'
UP, DOWN = 'up', 'down'

# custom_fields is one JSON column holding one value per custom field; each key
# is reported as a field of its own.
_EXPANDED_JSON_FIELDS = ('custom_fields',)

_ALLOWED_WRITE = re.compile(r'^\s*UPDATE\s+"?patient_record"?\s', re.IGNORECASE)
_READ_OR_CONTROL = re.compile(
    r'^\s*(SELECT|SAVEPOINT|RELEASE\s+SAVEPOINT|ROLLBACK\s+TO\s+SAVEPOINT|SHOW)\b',
    re.IGNORECASE,
)
_WRITE_KEYWORD = re.compile(
    r'\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|COPY|CREATE|ALTER|DROP)\b', re.IGNORECASE,
)
# SELECT ... INTO creates a table.
_INTO = re.compile(r'\bINTO\b', re.IGNORECASE)
# Functions a SELECT can call that write or reach outside the transaction:
# sequences and session-level advisory locks outlive a rollback, dblink and
# server-side lo_import/lo_export reach other databases or files. set_config
# and lo_create are transactional but refused anyway (fail closed). The guard
# catches accidental writes by the refresh code; it is not a sandbox -- the
# real boundary is a role with SELECT plus UPDATE on patient_record only.
_SIDE_EFFECT_FUNCTION = re.compile(
    r'\b(nextval|setval|pg_advisory_\w*lock\w*|pg_try_advisory_\w*|set_config|lo_\w+|dblink\w*)\s*\(',
    re.IGNORECASE,
)


class PreviewWriteBlocked(RuntimeError):
    """The refresh tried to write something other than its own PatientRecord."""


@dataclasses.dataclass(frozen=True)
class FieldChange:
    person_id: int
    field: str
    change: str             # SET (null -> value), CLEARED (value -> null), CHANGED
    direction: str | None   # UP or DOWN for a number that changed, else None
    user_edited: bool


def _statement_is_allowed(sql: str) -> bool:
    """Whether ``sql`` can only read, or update patient_record.

    Errs towards refusing: a refused statement fails one person's preview,
    a wrongly allowed one could write.
    """
    body = sql.strip().rstrip(';').rstrip()
    if ';' in body:
        # More than one statement: psycopg can send them together, and only the
        # first would have been checked.
        return False
    if _SIDE_EFFECT_FUNCTION.search(body):
        return False
    if _ALLOWED_WRITE.match(body):
        return True
    if _READ_OR_CONTROL.match(body):
        # FOR UPDATE takes a lock, released by the rollback, and writes nothing.
        return not _INTO.search(body)
    if re.match(r'^\s*WITH\b', body, re.IGNORECASE):
        # A data-modifying CTE also starts with WITH.
        return not (_WRITE_KEYWORD.search(body) or _INTO.search(body))
    return False


@contextlib.contextmanager
def read_only_guard() -> Iterator[list[str]]:
    """Refuse every write except the PatientRecord update, and every task send.

    Yields the list of refusals. A refusal raises where it happens, but code
    under the guard could catch that and carry on; the caller checks the list
    afterwards so a swallowed refusal still fails the preview.
    """
    refusals: list[str] = []

    def refuse(reason):
        refusals.append(reason)
        raise PreviewWriteBlocked(f'preview refused {reason}')

    def write_guard(execute, sql, params, many, context):
        if not _statement_is_allowed(sql):
            # Only the leading keywords: the statement can carry values.
            refuse(f'a write outside patient_record: {" ".join(sql.split(None, 3)[:3])} ...')
        return execute(sql, params, many, context)

    def refuse_task(*args, **kwargs):
        refuse('to publish a Celery task')

    with contextlib.ExitStack() as stack:
        for connection in connections.all():
            stack.enter_context(connection.execute_wrapper(write_guard))
        # Every publish, Task.apply_async and .delay included, goes through
        # Celery.send_task. A host that embeds omop_core without Celery has no
        # broker to publish to.
        if importlib.util.find_spec('celery') is not None:
            stack.enter_context(mock.patch('celery.app.base.Celery.send_task', refuse_task))
        yield refusals


def _values(record: PatientRecord) -> dict[str, object]:
    values = {}
    for field in PatientRecord._meta.concrete_fields:
        if field.name in IGNORED_FIELDS:
            continue
        if field.name in _EXPANDED_JSON_FIELDS:
            for key, value in (getattr(record, field.attname) or {}).items():
                values[f'{field.name}.{key}'] = value
            continue
        values[field.name] = getattr(record, field.attname)
    return values


def _is_number(value) -> bool:
    return isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool)


def describe_change(before, after) -> tuple[str, str | None]:
    """How a value changed, without the values: (change, direction)."""
    if before is None:
        return SET, None
    if after is None:
        return CLEARED, None
    if _is_number(before) and _is_number(after):
        return CHANGED, UP if after > before else DOWN
    return CHANGED, None


def preview_record(record: PatientRecord) -> list[FieldChange]:
    """The changes ``refresh_patient_record`` would make to ``record``, unsaved.

    ``user_edited`` marks a field that held a pending user edit before the
    refresh. The refresh keeps such a value until OMOP represents it, so most
    pending edits produce no change; one that does change shows what the
    refresh actually does to it, and the edit marker's own removal is reported
    under ``user_edited_fields``.

    The stored side is read under the same row lock the refresh takes, so a
    save that lands while the preview runs cannot pose as a derivation change.

    Not for a threaded server: the Celery guard patches ``Celery.send_task`` on
    the class, process-wide, for as long as the refresh runs, so another thread
    publishing a task at that moment would be refused too. A management
    command is single-threaded.
    """
    from omop_core.services.patient_record_service import refresh_patient_record

    with read_only_guard() as refusals:
        try:
            with transaction.atomic():
                try:
                    # Lock first, then read: refresh_patient_record takes the
                    # same lock again, which the holder is granted at once.
                    stored_record = PatientRecord.objects.select_for_update().get(pk=record.pk)
                    stored = _values(stored_record)
                    pending = set(stored_record.user_edited_fields or [])
                    refresh_patient_record(stored_record.person)
                    rederived = _values(PatientRecord.objects.get(pk=record.pk))
                finally:
                    transaction.set_rollback(True)
        except Exception as exc:
            # A refusal the refresh caught usually resurfaces as a broken
            # transaction; name the cause rather than the symptom.
            if refusals:
                raise PreviewWriteBlocked(f'preview refused {refusals[0]}') from exc
            raise
    if refusals:
        raise PreviewWriteBlocked(f'preview refused {refusals[0]}')

    changes = []
    for field in sorted(stored.keys() | rederived.keys()):
        before, after = stored.get(field), rederived.get(field)
        if before == after:
            continue
        change, direction = describe_change(before, after)
        changes.append(FieldChange(
            person_id=stored_record.person_id,
            field=field,
            change=change,
            direction=direction,
            user_edited=field in pending,
        ))
    return changes
