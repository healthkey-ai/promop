"""Organization-selectable clinical unit policy for derived compatibility fields."""

import logging

logger = logging.getLogger(__name__)

US_ONCOLOGY = 'US_ONCOLOGY'
SI = 'SI'

# mCODE/US oncology CBC convention and its SI equivalent.  The old CELLS/*
# values remain readable model choices for historical rows but are never emitted.
WBC_UNIT_BY_SYSTEM = {
    US_ONCOLOGY: '10*3/uL',
    SI: '10*9/L',
}


def canonical_wbc_unit(unit_system):
    return WBC_UNIT_BY_SYSTEM.get(unit_system, WBC_UNIT_BY_SYSTEM[US_ONCOLOGY])


def wbc_to_canonical(value, source_unit):
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
