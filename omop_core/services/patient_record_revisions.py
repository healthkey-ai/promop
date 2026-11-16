"""Field-level revision history shared by every PatientRecord write path.

The native PATCH used to keep this in its view; the shared form needs the same
rows for a same-day correction, where the OMOP assertion is updated in place
and the previous answer would otherwise be unrecoverable.
"""
import contextlib
import contextvars

from omop_core.models import RecordRevision

#: Suppresses revision rows inside a preview, whose transaction rolls back.
#: `RecordRevision.id` is a database sequence (it does not roll back), and the
#: preview guard refuses the INSERT anyway; the audit row is not a derived
#: value, so there is nothing to simulate.
_SUPPRESS = contextvars.ContextVar('suppress_record_revisions', default=False)


@contextlib.contextmanager
def suppress_record_revisions():
    """Run a block that must not write revision rows (a re-derivation preview)."""
    token = _SUPPRESS.set(True)
    try:
        yield
    finally:
        _SUPPRESS.reset(token)


def _norm(value):
    # Normalize to a stable string form for storage + comparison.
    return None if value is None else str(value)


def write_record_revisions(patient_record, previous_values, *, changed_by='system'):
    """Persist one row per field whose value actually changed.

    PHR-S FM TI.1.2#04. ``previous_values`` maps field name -> value BEFORE the
    update. The caller has already saved; this compares against the saved
    instance so an echoed value creates no revision.

    Returns no rows while `suppress_record_revisions()` is active: a preview
    re-derives the record only to compare it, and its transaction rolls back.
    The record is still refreshed, so a caller that reads it afterwards sees
    the stored row.
    """
    if _SUPPRESS.get():
        # Keep the function's documented post-condition: callers read the
        # instance after this returns.
        patient_record.refresh_from_db()
        return []
    patient_record.refresh_from_db()
    rows = []
    for field, old in previous_values.items():
        # Resolve the new value the same way the PATCH path captured the old one
        # (FK fields are stored/compared via their {field}_id attribute).
        fk_id = f'{field}_id'
        new = (getattr(patient_record, fk_id, None)
               if hasattr(patient_record, fk_id) else getattr(patient_record, field, None))
        old_s, new_s = _norm(old), _norm(new)
        if old_s == new_s:
            continue
        rows.append(RecordRevision(
            patient_record=patient_record,
            changed_by=changed_by,
            field=field,
            old_value=old_s,
            new_value=new_s,
        ))
    if rows:
        RecordRevision.objects.bulk_create(rows)
    return rows
