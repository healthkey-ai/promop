"""Organization-selectable clinical unit policy for derived compatibility fields."""

import logging
from decimal import Decimal, InvalidOperation

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


def blood_count_to_canonical(value, source_unit):
    """ANC/platelets in 10^3/uL. Unknown scale or invalid counts stay unknown.

    Keep Decimal precision until the destination field applies its declared
    precision. Uppercase G/L is a legacy giga-count spelling; lowercase g/L
    denotes mass and is deliberately not accepted for cell counts.
    """
    unit = (source_unit or '').strip().replace('μ', 'u').replace('µ', 'u')
    unit = unit.replace(' ', '').replace('³', '^3').replace('⁹', '^9')
    if unit == 'G/L':
        unit = '10^9/L'
    unit = unit.lower()
    factors = {
        'cells/ul': '0.001', 'cell/ul': '0.001', '/ul': '0.001',
        '{cells}/ul': '0.001', '#/ul': '0.001',
        'cells/l': '0.000000001', 'cell/l': '0.000000001', '/l': '0.000000001',
        '{cells}/l': '0.000000001', '#/l': '0.000000001',
        '10*3/ul': '1', '10^3/ul': '1', 'k/ul': '1',
        '10*9/l': '1', '10^9/l': '1',
    }
    if unit not in factors or value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number * Decimal(factors[unit])


def measurement_count_unit(row):
    """Prefer explicit source units; use a UCUM concept only when text is absent."""
    if row.unit_source_value and row.unit_source_value.strip():
        return row.unit_source_value
    unit = row.unit_concept
    if unit is not None and unit.vocabulary_id == 'UCUM':
        return unit.concept_code
    return None


def blood_count_projection(field, row):
    """Return coherent canonical and legacy pairs, including explicit unknowns."""
    unit = measurement_count_unit(row)
    value = blood_count_to_canonical(row.value_as_number, unit)
    if value is None:
        logger.warning('%s projection skipped: unsupported/absent unit or invalid count (unit=%r)', field, unit)
    if field == 'anc_thousand_per_ul':
        return {
            field: value,
            'absolute_neutrophile_count': value,
            'absolute_neutrophile_count_units': '10*3/uL' if value is not None else None,
        }
    # The legacy platelet column is integer-valued. Reporting cells/uL retains
    # fractional thousands and matches the downstream compatibility contract.
    cells = value * 1000 if value is not None else None
    if cells is not None and (cells != cells.to_integral_value() or cells > 2_147_483_647):
        cells = None
    return {
        field: value,
        'platelet_count': int(cells) if cells is not None else None,
        'platelet_count_units': 'CELLS/UL' if cells is not None else None,
    }


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
