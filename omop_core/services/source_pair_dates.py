"""The day a pending source pair was entered (#2426).

A source pair (the number a reader typed and the unit they chose) stays pending
in ``PatientRecord.user_edited_fields`` until an authoritative event retires
it; a plain import never did, so a newer imported result was hidden behind an
older pair forever (``#2426``). Deciding "newer" needs the day the pair was
entered, which the record did not keep: the canonical projection carries it,
but the pair is what stays pending.

``PatientRecord.pending_source_pair_dates`` is that memory: a server-managed
mapping of the pair's source field to an ISO ``YYYY-MM-DD`` day. It is not a
client field — the serializer exposes it read-only and omits it from responses;
the pair write paths maintain it, and derivation removes an entry once its pair
is no longer pending.

A legacy pair with no recorded day stays pending. Guessing a day from a
revision timestamp or from a matching value would retire a patient's answer on
evidence the record never had.

This module owns the pair inventory so the view, the shared form, the OMOP
applier and derivation agree on which fields belong to one pair.
"""

from __future__ import annotations

from datetime import date, datetime

from omop_core.services import (
    chemistry_units,
    creatinine_units,
    direct_bilirubin_units,
    hemoglobin_units,
)
from omop_core.services.blood_count_source_pairs import PAIRS as COUNT_PAIRS

#: Serializer/patient-record field that stores the dates. Server-managed.
FIELD = 'pending_source_pair_dates'

#: Pair source field -> the canonical result derivation reads for it. Direct
#: bilirubin has no canonical of its own: the pair is the field.
SOURCE_PAIR_CANONICALS: dict[str, str] = {
    hemoglobin_units.PAIR_FIELD: hemoglobin_units.CANONICAL_FIELD,
    creatinine_units.PAIR_FIELD: creatinine_units.CANONICAL_FIELD,
    direct_bilirubin_units.PAIR_FIELD: direct_bilirubin_units.PAIR_FIELD,
    **{
        spec['value']: canonical
        for canonical, spec in chemistry_units.PAIRS.items()
    },
    **{
        source: spec['canonical']
        for source, spec in COUNT_PAIRS.items()
    },
}

#: Pair source field -> every record field that is pending with it. A pair is
#: one assertion: its unit column (when it has one) is not an independent edit.
SOURCE_PAIR_FIELDS: dict[str, frozenset[str]] = {
    hemoglobin_units.PAIR_FIELD: frozenset({
        hemoglobin_units.PAIR_FIELD, hemoglobin_units.UNIT_FIELD,
    }),
    creatinine_units.PAIR_FIELD: frozenset({
        creatinine_units.PAIR_FIELD, creatinine_units.UNIT_FIELD,
    }),
    direct_bilirubin_units.PAIR_FIELD: frozenset({
        direct_bilirubin_units.PAIR_FIELD, direct_bilirubin_units.UNIT_FIELD,
    }),
    **{
        spec['value']: frozenset({spec['value'], spec['unit']})
        for spec in chemistry_units.PAIRS.values()
    },
    **{
        source: frozenset(
            {source} | ({spec['unit_field']} if spec.get('unit_field') else set())
        )
        for source, spec in COUNT_PAIRS.items()
    },
}

#: Any field that belongs to a pair -> the pair's source field.
_PAIR_OF_FIELD = {
    field: source
    for source, fields in SOURCE_PAIR_FIELDS.items()
    for field in fields
}


def fields_for(source: str) -> frozenset[str]:
    """Every record field pending with ``source`` (the pair and its unit)."""
    return SOURCE_PAIR_FIELDS[source]


def pair_of(field: str) -> str | None:
    """The pair a field belongs to, or ``None`` for an ordinary field."""
    return _PAIR_OF_FIELD.get(field)


def pair_sources_in(fields) -> set[str]:
    """The pair source fields named by any of ``fields``."""
    return {source for source in map(pair_of, fields) if source is not None}


def _parse_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def stored_dates(record) -> dict[str, date]:
    """The recorded day per pending pair; unreadable entries are ignored."""
    raw = getattr(record, FIELD, None) or {}
    if not isinstance(raw, dict):
        return {}
    dates = {}
    for source, value in raw.items():
        if source not in SOURCE_PAIR_CANONICALS:
            continue
        day = _parse_day(value)
        if day is not None:
            dates[source] = day
    return dates


def remember_dates(record, dates: dict[str, date]) -> bool:
    """Record the entered day of each pending pair; true when it changed."""
    if not dates:
        return False
    raw = dict(getattr(record, FIELD, None) or {})
    changed = False
    for source, day in dates.items():
        if source not in SOURCE_PAIR_CANONICALS or day is None:
            continue
        iso = day.isoformat()
        if raw.get(source) != iso:
            raw[source] = iso
            changed = True
    if changed:
        record.pending_source_pair_dates = raw
    return changed


def forget_dates(record, sources) -> bool:
    """Drop the recorded day of pairs that are no longer pending."""
    raw = dict(getattr(record, FIELD, None) or {})
    if not raw:
        return False
    changed = False
    for source in sources:
        if raw.pop(source, None) is not None:
            changed = True
    if changed:
        record.pending_source_pair_dates = raw
    return changed


def forget_unedited_dates(record, pending_fields) -> bool:
    """Keep a recorded day only while its pair is still pending.

    Every path that retires a pair does so by removing its fields from
    ``user_edited_fields``; this keeps the two stores consistent without each
    of them having to remember the date store.
    """
    pending = set(pending_fields or ())
    return forget_dates(
        record,
        [source for source in stored_dates(record) if source not in pending],
    )


def superseded_sources(pending_fields, record_dates, derived_dates,
                       derived_values) -> set[str]:
    """Pairs a full derivation retires because a newer result now exists.

    ``derived_dates`` is what ``_get_laboratory_data(..., dates_out=...)``
    recorded for the winning fact of each canonical field this refresh, so this
    uses derivation's own selection rather than a second implementation of its
    rules. A same-day result does not retire the pair, an unusable newer result
    leaves it pending (the extractor records no date for one), and a legacy
    pair with no recorded day is never retired here.
    """
    pending = set(pending_fields or ())
    superseded = set()
    for source, canonical in SOURCE_PAIR_CANONICALS.items():
        if source not in pending:
            continue
        entered = record_dates.get(source)
        if entered is None:
            continue
        derived = derived_dates.get(canonical)
        if derived is None or derived <= entered:
            continue
        if derived_values.get(canonical) is None:
            continue
        superseded.add(source)
    return superseded
