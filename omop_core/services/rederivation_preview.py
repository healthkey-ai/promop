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
import importlib.util
import re
from typing import Iterator
from unittest import mock

from django.core.exceptions import FieldDoesNotExist
from django.db import connections, transaction

from omop_core.models import PatientRecord

# Bookkeeping that every refresh changes, or that refresh never sets. Reporting
# them would put a line on every record and say nothing about its values.
IGNORED_FIELDS = frozenset({
    'id', 'person', 'organization', 'derivation_version', 'derived_at',
    'created_at', 'updated_at',
})

# Default-deny: a change is always reported, but its VALUES are shown only
# for fields that cannot carry identifying text or a personal date: numbers,
# booleans, coded fields with fixed choices, and user_edited_fields (a list of
# field names). Everything else -- free text, every date, every JSON column
# (genomic findings keep the source report text), custom-field text -- shows
# "[redacted]" instead. A null is still shown as null, so a field being wiped
# stays visible. A deny-list of "identifying" columns could never be complete;
# this inverts it.
_SHOWN_TYPES = frozenset({
    'BooleanField', 'NullBooleanField', 'IntegerField', 'SmallIntegerField',
    'BigIntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField',
    'PositiveBigIntegerField', 'FloatField', 'DecimalField',
})
# Explicitly shown: field names, not values (which columns hold a pending user
# edit), and text columns that hold a vocabulary term, not free text -- the
# diagnosis and demographics a derivation change most often moves.
SHOWN_FIELDS = frozenset({
    'user_edited_fields', 'disease', 'disease_slug', 'stage', 'race', 'ethnicity',
})
# Numbers that are identifiers all the same.
NEVER_SHOWN_FIELDS = frozenset({'latitude', 'longitude'})
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
    stored: object
    rederived: object
    user_edited: bool
    redacted: bool


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


def value_is_shown(field: str) -> bool:
    """Whether a PatientRecord field's values may be printed (see above)."""
    if field in NEVER_SHOWN_FIELDS:
        return False
    if field in SHOWN_FIELDS:
        return True
    try:
        model_field = PatientRecord._meta.get_field(field)
    except FieldDoesNotExist:
        return False
    if getattr(model_field, 'choices', None):
        return True
    return model_field.get_internal_type() in _SHOWN_TYPES


def _redacted(field: str, value) -> bool:
    if value is None:
        return False
    if field.startswith('custom_fields.'):
        # A custom field's numbers and flags are shown; anything else is not.
        return not isinstance(value, (bool, int, float))
    if field == 'patient_age':
        # An age over 89 is itself a Safe Harbor identifier.
        try:
            return float(value) > 89
        except (TypeError, ValueError):
            return True
    return not value_is_shown(field)


def _shown(field: str, value):
    return REDACTED if _redacted(field, value) else value


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
        changes.append(FieldChange(
            person_id=stored_record.person_id,
            field=field,
            stored=_shown(field, before),
            rederived=_shown(field, after),
            user_edited=field in pending,
            redacted=_redacted(field, before) or _redacted(field, after),
        ))
    return changes
