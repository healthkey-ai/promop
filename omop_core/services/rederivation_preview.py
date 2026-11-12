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
import logging
import math
import os
import re
import traceback
from typing import Iterator
from unittest import mock

from django.db import DatabaseError, connections, transaction

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
# is reported as a field of its own -- by name only when an administrator
# defined it (CustomPatientField). A key with no definition is whatever was
# written into the JSON, so it is reported under this placeholder instead.
_EXPANDED_JSON_FIELDS = ('custom_fields',)
UNDEFINED_CUSTOM_FIELD = 'custom_fields.<undefined>'

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
    change: str             # SET (empty -> value), CLEARED (value -> empty), CHANGED
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


_STATEMENT_TARGET = re.compile(
    r'^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|COPY)\s+("?[\w.]+"?)',
    re.IGNORECASE,
)


def _statement_kind(sql: str) -> str:
    """The statement's verb and table, never its text: a literal could be a value."""
    match = _STATEMENT_TARGET.match(sql)
    if match:
        return f'{" ".join(match.group(1).upper().split())} {match.group(2)}'
    word = sql.split(None, 1)[0].upper() if sql.strip() else ''
    return word if word.isalpha() else 'statement'


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
            refuse(f'a write outside patient_record: {_statement_kind(sql)}')
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


def describe_failure(person_id: int, exc: BaseException) -> dict:
    """What can be said about a failed refresh without quoting the record.

    Not the message or the traceback: a constraint violation's message carries
    PostgreSQL's "Failing row contains (...)", which is every column of the
    patient's record. A database error is described by its SQLSTATE and
    constraint name, a refused preview write by its verb and table. Shared by
    preview_rederivation and backfill_patient_records.
    """
    failure = {'person_id': person_id, 'error': type(exc).__name__}
    # Where it was raised -- file, line, function, never a value -- so a code
    # bug can still be found without the traceback.
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        frame = frames[-1]
        failure['at'] = f'{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}'
    if isinstance(exc, PreviewWriteBlocked):
        # Built from the statement's verb and table only (see read_only_guard).
        failure['refused'] = str(exc).removeprefix('preview refused ')
    cause = exc if isinstance(exc, DatabaseError) else None
    while cause is not None and not hasattr(cause, 'sqlstate'):
        cause = cause.__cause__
    if cause is not None:
        if cause.sqlstate:
            failure['sqlstate'] = cause.sqlstate
        constraint = getattr(getattr(cause, 'diag', None), 'constraint_name', None)
        if constraint:
            failure['constraint'] = constraint
    return failure


def failure_detail(failure: dict) -> str:
    """``key=value`` pairs of a describe_failure() result, person_id aside."""
    return ' '.join(f'{key}={value}' for key, value in failure.items() if key != 'person_id')


def _is_empty(value) -> bool:
    """The derivation's own "no data" values: 0 and False are answers."""
    from omop_core.services.patient_record_service import _is_empty as derivation_is_empty

    return derivation_is_empty(value)


def stale_record_cohort(target_version: int, every_record: bool = False,
                        organization: str | None = None):
    """``(queryset, label)``: the PatientRecords a backfill re-derives.

    One definition for backfill_patient_records and preview_rederivation, so
    the preview cannot quietly report on a different cohort.
    """
    qs = PatientRecord.objects.all()
    if every_record:
        label = 'all'
    else:
        qs = qs.filter(derivation_version__lt=target_version)
        label = f'derivation_version < {target_version}'
    if organization:
        qs = qs.filter(organization__slug=organization)
        label += f' in organization {organization}'
    return qs, label


def _is_number(value) -> bool:
    return isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool)


def _is_nan(value) -> bool:
    return _is_number(value) and value != value


def same_value(before, after) -> bool:
    """Equality as the stored JSON or column sees it.

    Python's == calls True equal to 1 and False to 0, and NaN unequal to
    itself; a flag that becomes a number is a change, and NaN staying NaN is
    not.
    """
    if isinstance(before, bool) or isinstance(after, bool):
        return type(before) is type(after) and before == after
    if _is_nan(before) or _is_nan(after):
        return _is_nan(before) and _is_nan(after)
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        return len(before) == len(after) and all(map(same_value, before, after))
    if isinstance(before, dict) and isinstance(after, dict):
        return before.keys() == after.keys() and all(
            same_value(before[key], after[key]) for key in before)
    return before == after


def describe_change(before, after) -> tuple[str, str | None]:
    """How a value changed, without the values: (change, direction).

    SET and CLEARED use the derivation's emptiness, so a refresh that empties
    a list of genomic findings is a wipe, not an edit. Only a number has a
    direction: a flag's direction would be its value.
    """
    if _is_empty(before):
        return SET, None
    if _is_empty(after):
        return CLEARED, None
    if _is_number(before) and _is_number(after) and _finite(before) and _finite(after):
        return CHANGED, UP if after > before else DOWN
    return CHANGED, None


def _finite(value) -> bool:
    if isinstance(value, int):
        return True  # math.isfinite overflows on an int beyond float range
    return math.isfinite(value)


def custom_field_names() -> frozenset[str]:
    from omop_core.models import CustomPatientField

    return frozenset(
        f'custom_fields.{name}'
        for name in CustomPatientField.objects.values_list('field_name', flat=True))


def _reported_name(field: str, defined: frozenset[str]) -> str:
    if field.startswith('custom_fields.') and field not in defined:
        return UNDEFINED_CUSTOM_FIELD
    return field


@contextlib.contextmanager
def _refresh_logging_silenced():
    """Drop every log record while the refresh runs.

    The refresh's own diagnostics quote what they diagnose -- a source unit's
    text, an overflowing value -- next to the person_id, and with no handler
    configured Python prints them on the operator's stderr. They belong to
    the backfill, which runs the same refresh for real; the preview reports
    its failures itself, by type (see the command).
    """
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def preview_record(record: PatientRecord,
                   defined: frozenset[str] | None = None) -> list[FieldChange]:
    """The changes ``refresh_patient_record`` would make to ``record``, unsaved.

    ``user_edited`` marks a field that held a pending user edit before the
    refresh. The refresh keeps such a value until OMOP represents it, so most
    pending edits produce no change; one that does change shows what the
    refresh actually does to it, and the edit marker's own removal is reported
    under ``user_edited_fields``.

    ``defined`` is ``custom_field_names()``; a caller previewing many records
    passes it once.

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
                    with _refresh_logging_silenced():
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

    if defined is None:
        defined = custom_field_names()
    changes = []
    for field in stored.keys() | rederived.keys():
        before, after = stored.get(field), rederived.get(field)
        if same_value(before, after) or (_is_empty(before) and _is_empty(after)):
            # '' -> null is no change to the derivation either
            # (_derived_value_matches).
            continue
        change, direction = describe_change(before, after)
        changes.append(FieldChange(
            person_id=stored_record.person_id,
            field=_reported_name(field, defined),
            change=change,
            direction=direction,
            user_edited=field in pending,
        ))
    # Sorted by the reported name: sorting by the raw key would place an
    # undefined key's row by its text among the defined ones.
    return sorted(changes, key=lambda change: (change.field, change.change, change.user_edited))
