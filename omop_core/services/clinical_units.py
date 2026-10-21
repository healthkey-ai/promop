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
    'mg/l': Decimal('1'),
    'mg/dl': Decimal('10'),       # 1 mg/dL = 10 mg/L
    'mg/100ml': Decimal('10'),
    'ug/ml': Decimal('1'),        # 1 ug/mL = 1 mg/L
    'mcg/ml': Decimal('1'),
    'g/l': Decimal('1000'),
}


def flc_to_canonical(
    value: Decimal | float | None, source_unit: str | None,
) -> Decimal | None:
    """Convert a free light chain result to mg/L, None if the unit is unknown.

    None means "cannot be established", not "zero". Labs report these in mg/L
    and mg/dL, so guessing is a 10x error half the time. Decimal arithmetic is
    deliberate: the IMWG window is inclusive at 0.26/1.65, and a binary-float
    conversion such as 10.23 mg/dL -> 102.30000000000001 would move an exact
    boundary result outside that window.
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
    return Decimal(str(value)) * factor
