"""Cell-count source pairs: cells/µL in, thousands/µL out (#5700 follow-up).

EXACT's trial rows and CB speak cells/µL (`absolute_neutrophile_count`,
`absolute_lymphocyte_count`, `platelet_count`), while PROMOP's canonical
columns are thousands/µL. The federated editor writes the field the descriptor
names, so making the legacy count column the writable source keeps the typed
number in the unit the row shows and derives the canonical value.

This is the count counterpart of `hemoglobin_units`: one place owns the unit
spellings and the factor, and the serializer, derivation and both form writers
use it.
"""
from decimal import ROUND_HALF_UP, Decimal

from omop_core.services.clinical_units import blood_count_to_canonical

#: source field -> canonical field, stored unit column and display unit.
#: The canonical columns are `DecimalField(max_digits=8, decimal_places=3)`
#: (0328). A larger count raised on save: a 500 from the form, the serializer
#: or a recompute, instead of a refused value.
CANONICAL_QUANTUM = Decimal('0.001')
CANONICAL_LIMIT = Decimal('99999.999')

PAIRS = {
    'absolute_neutrophile_count': {
        'canonical': 'anc_thousand_per_ul',
        'unit_field': 'absolute_neutrophile_count_units',
        'unit': 'CELLS/UL',
        'canonical_unit': '10*3/uL',
    },
    'absolute_lymphocyte_count': {
        'canonical': 'alc_thousand_per_ul',
        'unit_field': None,
        'unit': 'CELLS/UL',
        'canonical_unit': '10*3/uL',
    },
    'platelet_count': {
        'canonical': 'platelet_count_thousand_per_ul',
        'unit_field': 'platelet_count_units',
        'unit': 'CELLS/UL',
        'canonical_unit': '10*3/uL',
    },
}

#: The source value's unit is the one a count is typed in on a trial row.
UNIT_OPTIONS = [
    {'value': 'CELLS/UL', 'label': 'cells/µL'},
    {'value': '10*3/uL', 'label': '10³/µL'},
    {'value': '10*9/L', 'label': '10⁹/L'},
]

_SOURCE_FIELDS = frozenset(PAIRS) | frozenset(
    spec['unit_field'] for spec in PAIRS.values() if spec.get('unit_field')
)

_UNIT_CODES = {
    'cells/ul': 'CELLS/UL',
    'cell/ul': 'CELLS/UL',
    '/ul': 'CELLS/UL',
    '{cells}/ul': 'CELLS/UL',
    '#/ul': 'CELLS/UL',
    '10*3/ul': '10*3/uL',
    '10^3/ul': '10*3/uL',
    'k/ul': '10*3/uL',
    '10*9/l': '10*9/L',
    '10^9/l': '10*9/L',
    'cells/l': 'CELLS/L',
    'cell/l': 'CELLS/L',
    '/l': 'CELLS/L',
    '{cells}/l': 'CELLS/L',
    '#/l': 'CELLS/L',
}


def _normalize_spelling(value):
    return (
        str(value or '').strip()
        .replace('μ', 'u').replace('µ', 'u')
        .replace('³', '^3').replace('⁹', '^9')
        .replace(' ', '').lower()
    )


def normalize_unit(value):
    """The stored `BloodCountUnits` code for a code or display label.

    Uppercase ``G/L`` is the legacy giga-count spelling. Lowercase ``g/L`` is
    a mass concentration and must not be read as a cell count; the shared
    ``blood_count_to_canonical`` draws the same distinction.
    """
    if value is None:
        return None
    text = (
        str(value).strip()
        .replace('μ', 'u').replace('µ', 'u')
        .replace('³', '^3').replace('⁹', '^9')
        .replace(' ', '')
    )
    if text == 'G/L':
        return '10*9/L'
    return _UNIT_CODES.get(text.lower())


def canonical_from_source(source, value, unit=None):
    """Return ``(recognised, canonical)`` for a source count.

    ``recognised`` is False for a non-numeric value or an unknown unit; the
    caller must not invent a thousands/µL number then. A missing unit is the
    source's default (cells/µL), the unit trial rows use.
    """
    spec = PAIRS[source]
    if value is None:
        return True, None
    if unit is None or str(unit).strip() == '':
        code = spec['unit']
    else:
        code = normalize_unit(unit)
    if code is None:
        return False, None
    canonical = blood_count_to_canonical(value, code)
    if canonical is None:
        return False, None
    if canonical.quantize(CANONICAL_QUANTUM, rounding=ROUND_HALF_UP) > CANONICAL_LIMIT:
        return False, None
    return True, canonical


def source_from_canonical(source, value):
    """The legacy count in cells/µL, or None when the column cannot hold it."""
    if value is None:
        return None
    cells = Decimal(str(value)) * 1000
    if source == 'platelet_count':
        # The legacy platelet column is integer-valued. A fractional result
        # has no faithful representation here; leave it to the canonical only.
        if cells != cells.to_integral_value() or cells > 2_147_483_647:
            return None
        return int(cells)
    return cells


def canonical_fields():
    return {spec['canonical'] for spec in PAIRS.values()}


def source_fields():
    return _SOURCE_FIELDS
