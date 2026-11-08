"""Unit normalisation for the chemistry source pairs (#2318).

Albumin, serum calcium and total bilirubin arrive from labs in either the
conventional (g/dL, mg/dL) or the SI (g/L, mmol/L, umol/L) scale. The record
keeps the number the lab reported in the source pair; these canonical columns
are the normalised storage/projection read-model the matcher and CRAB read:

    albumin            albumin_level            -> albumin_g_dl
    serum calcium      serum_calcium_level      -> serum_calcium_mg_dl
    total bilirubin    serum_bilirubin_level_total -> bilirubin_total_mg_dl

The factors are the ones the installed matcher already uses, so a value the
canonical is derived from converts exactly the way EXACT converts the pair:

    creatinine  88.42  umol/L per mg/dL     (creatinine_units.py)
    bilirubin   17.1   umol/L per mg/dL     (BilirubinConvertor)
    calcium     0.2495 mmol/L per mg/dL     (SerumCalciumConvertor)
    albumin     10     g/L per g/dL         (BaseConvertor/pint)

OMOP imports do not always carry the canonical unit: the LOINC and display-name
projections copy `value_as_number` straight into these columns. Without this
module a lab reported as `40 g/L` would be stored as `40` g/dL — a 10x error in
the read-model and, once the pair is the write target, in the pair too. The
normaliser refuses an unknown unit rather than guessing a scale.

A missing unit is not an unknown one: an unqualified import is the established
contract for a value already recorded in the canonical scale (the display-name
mappings name mass/volume, not SI), so it normalises as the identity. Callers
that see an explicit but unsupported unit must treat that result as unknown and
must not fall back to an older, conveniently scaled fact.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

#: The canonical columns this module normalises, by stored pair shape. All three
#: canonical columns are `DecimalField(max_digits=5, decimal_places=1)`; the
#: source pair values are `(10, 2)`.
PAIRS = {
    'albumin_g_dl': {
        'value': 'albumin_level',
        'unit': 'albumin_level_units',
        'default_unit': 'G/DL',
        'canonical_unit': 'G/DL',
        'label': 'Albumin',
        'divisors': {'G/DL': Decimal('1'), 'G/L': Decimal('10')},
        'units': {'G/DL': 'g/dL', 'G/L': 'g/L'},
    },
    'serum_calcium_mg_dl': {
        'value': 'serum_calcium_level',
        'unit': 'serum_calcium_level_units',
        'default_unit': 'MG/DL',
        'canonical_unit': 'MG/DL',
        'label': 'Serum calcium',
        # mg/dL = mmol/L / 0.2495, the matcher's SerumCalciumConvertor factor.
        'divisors': {'MG/DL': Decimal('1'), 'MMOL/L': Decimal('0.2495')},
        'units': {'MG/DL': 'mg/dL', 'MMOL/L': 'mmol/L'},
    },
    'bilirubin_total_mg_dl': {
        'value': 'serum_bilirubin_level_total',
        'unit': 'serum_bilirubin_level_total_units',
        'default_unit': 'MG/DL',
        'canonical_unit': 'MG/DL',
        'label': 'Total bilirubin',
        # mg/dL = umol/L / 17.1, the matcher's BilirubinConvertor factor.
        'divisors': {'MG/DL': Decimal('1'), 'UMOL/L': Decimal('17.1')},
        'units': {'MG/DL': 'mg/dL', 'UMOL/L': 'umol/L'},
    },
}

#: `DecimalField(max_digits=5, decimal_places=1)` on every canonical column.
CANONICAL_QUANTUM = Decimal('0.1')
CANONICAL_LIMIT = Decimal('999.9')

#: Stored unit code spellings, with the micro sign folded to `u`.
_SPELLINGS = {
    'g/dl': 'G/DL', 'g/l': 'G/L',
    'mg/dl': 'MG/DL',
    'mmol/l': 'MMOL/L',
    'umol/l': 'UMOL/L',
}


def _normalise(unit):
    if unit is None:
        return ''
    return (str(unit).strip().casefold()
            .replace('μ', 'u').replace('µ', 'u')
            .replace(' ', ''))


def normalize_unit(canonical_field, unit):
    """The stored code for a unit code or display label, or None if unknown."""
    spec = PAIRS.get(canonical_field)
    code = _SPELLINGS.get(_normalise(unit))
    if spec is None or code not in spec['divisors']:
        return None
    return code


def canonical_from_pair(canonical_field, value, unit):
    """Return ``(recognised, canonical)`` for a stored source pair.

    ``recognised`` is False when the unit is unknown or the value is not
    numeric. Callers must not invent a canonical value then: the pair is
    preserved as a legacy fact but cannot satisfy a threshold.

    A missing unit means the canonical unit, the scale every unqualified import
    is recorded in. A clear (``value is None``) is recognised and normalises to
    ``None`` whatever the unit says.

    The result is quantised to the canonical column's one decimal with half-up
    rounding, the same rounding the database applies.
    """
    spec = PAIRS.get(canonical_field)
    if spec is None:
        return False, None
    if value is None:
        return True, None
    code = normalize_unit(canonical_field, unit)
    if code is None and (unit is None or _normalise(unit) == ''):
        code = spec['default_unit']
    divisor = spec['divisors'].get(code or '')
    if divisor is None:
        return False, None
    try:
        canonical = (Decimal(str(value)) / divisor).quantize(
            CANONICAL_QUANTUM, rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError):
        return False, None
    if not canonical.is_finite():
        return False, None
    return True, canonical


def canonical_in_range(canonical_field, value):
    """Whether a canonical value fits the destination column."""
    if PAIRS.get(canonical_field) is None:
        return False
    if value is None:
        return True
    return 0 <= value <= CANONICAL_LIMIT


def source_fields(canonical_field):
    """The ``(value, unit)`` record fields of one pair, or ``()``."""
    spec = PAIRS.get(canonical_field)
    if spec is None:
        return ()
    return spec['value'], spec['unit']


def unit_hint(canonical_field):
    """The refusal message listing the units this pair can convert."""
    spec = PAIRS.get(canonical_field)
    if spec is None:
        return ''
    return 'Choose one of: ' + ', '.join(spec['units'].values()) + '.'
