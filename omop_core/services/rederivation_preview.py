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

The row lock ``refresh_patient_record`` takes is held only until that rollback,
one person at a time, as the backfill holds it.
"""
from __future__ import annotations

import contextlib
import dataclasses
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

# Direct identifiers: a change is reported, its values are not.
REDACTED_FIELDS = frozenset({
    'email', 'phone_number', 'date_of_birth', 'city', 'postal_code',
    'latitude', 'longitude', 'facility_name', 'validated_by',
})
REDACTED = '[redacted]'

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


class PreviewWriteBlocked(RuntimeError):
    """The refresh tried to write something other than its own PatientRecord."""


@dataclasses.dataclass(frozen=True)
class FieldChange:
    person_id: int
    field: str
    stored: object
    rederived: object
    user_edited: bool
    redacted: bool


def _statement_is_allowed(sql: str) -> bool:
    if _ALLOWED_WRITE.match(sql):
        return True
    if _READ_OR_CONTROL.match(sql):
        # A SELECT cannot write; a data-modifying CTE starts with WITH and is
        # caught below. FOR UPDATE takes a lock and writes nothing.
        return True
    if re.match(r'^\s*WITH\b', sql, re.IGNORECASE):
        return not _WRITE_KEYWORD.search(sql)
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


def preview_record(record: PatientRecord) -> list[FieldChange]:
    """The changes ``refresh_patient_record`` would make to ``record``, unsaved.

    ``user_edited`` marks a field that held a pending user edit before the
    refresh. The refresh keeps such a value until OMOP represents it, so most
    pending edits produce no change; one that does change shows what the
    refresh actually does to it, and the edit marker's own removal is reported
    under ``user_edited_fields``.
    """
    from omop_core.services.patient_record_service import refresh_patient_record

    stored_record = PatientRecord.objects.select_related('person').get(pk=record.pk)
    stored = _values(stored_record)
    pending = set(stored_record.user_edited_fields or [])

    with read_only_guard() as refusals:
        try:
            with transaction.atomic():
                try:
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
        column = field.split('.', 1)[0]
        redacted = column in REDACTED_FIELDS
        changes.append(FieldChange(
            person_id=stored_record.person_id,
            field=field,
            stored=REDACTED if redacted else before,
            rederived=REDACTED if redacted else after,
            user_edited=field in pending,
            redacted=redacted,
        ))
    return changes
