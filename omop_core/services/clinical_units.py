"""Organization-selectable clinical unit policy for derived compatibility fields."""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

US_ONCOLOGY = 'US_ONCOLOGY'
SI = 'SI'

# mCODE/US oncology CBC convention and its SI equivalent.  The old CELLS/*
# values remain readable model choices for historical rows but are never emitted.
WBC_UNIT_BY_SYSTEM = {
    US_ONCOLOGY: '10*3/uL',
    SI: '10*9/L',
}


def canonical_wbc_unit(unit_system: str | None) -> str:
    """Return the WBC unit this organization's unit system reports in."""
    return WBC_UNIT_BY_SYSTEM.get(unit_system, WBC_UNIT_BY_SYSTEM[US_ONCOLOGY])


def wbc_to_canonical(
    value: Decimal | float, source_unit: str | None,
) -> float | None:
    """Convert an incoming WBC result to 10^3/uL (numerically equal to 10^9/L)."""
    normalized = (source_unit or '').lower().replace('μ', 'u').replace('µ', 'u').replace(' ', '')
    value = float(value)
    if normalized in {'cells/ul', 'cell/ul', '/ul'}:
        return value / 1000
    if normalized in {'cells/l', 'cell/l', '/l'}:
        return value / 1_000_000_000
    if normalized in {
        '10*3/ul', '10^3/ul', 'k/ul', '10*9/l', '10^9/l', 'g/l',
    }:
        # These canonical expressions are numerically equivalent.
        return value
    return None


FLC_CANONICAL_UNIT = 'mg/L'

_FLC_FACTOR_TO_MG_L = {
    'mg/l': 1.0,
    'mg/dl': 10.0,       # 1 mg/dL = 10 mg/L
    'mg/100ml': 10.0,
    'ug/ml': 1.0,        # 1 ug/mL = 1 mg/L
    'mcg/ml': 1.0,
    'g/l': 1000.0,
}


def flc_to_canonical(
    value: Decimal | float | None, source_unit: str | None,
) -> float | None:
    """Convert a free light chain result to mg/L, None if the unit is unknown.

    None means "cannot be established", not "zero". Labs report these in mg/L
    and mg/dL, so guessing is a 10x error half the time.
    """
    if value is None:
        return None
    normalized = (
        (source_unit or '').lower()
        .replace('μ', 'u').replace('µ', 'u').replace(' ', '')
    )
    factor = _FLC_FACTOR_TO_MG_L.get(normalized)
    if factor is None:
        return None
    return float(value) * factor
