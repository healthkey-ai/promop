"""The hemoglobin source pair (#2318): g/L or g/dL, normalised to g/dL.

The record keeps what the lab reported. Matching, CRAB and the OMOP projection
read the normalised g/dL value, so the conversion lives here: the serializer,
derivation and both form writers use the same helpers rather than each spelling
the factor and the accepted labels again.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

#: The writable source value and the unit stored beside it.
PAIR_FIELD = 'hemoglobin_level'
UNIT_FIELD = 'hemoglobin_level_units'
#: The normalised read-model projected to OMOP.
CANONICAL_FIELD = 'hemoglobin_g_dl'

DEFAULT_UNIT = 'G/DL'

#: Stored `TextChoices` code -> multiplier into the canonical g/dL value.
UNIT_FACTORS = {'G/DL': Decimal('1'), 'G/L': Decimal('0.1')}

#: `PatientRecord.hemoglobin_g_dl` is `DecimalField(max_digits=5,
#: decimal_places=1)`. The derived value must already have that precision:
#: persistence, projection and matching read the same number, so rounding only
#: at the database would let the three disagree. `tests/test_hemoglobin_source_pair.py`
#: asserts these constants against the model field.
CANONICAL_QUANTUM = Decimal('0.1')
CANONICAL_LIMIT = Decimal('9999.9')

_SPELLINGS = {
    spelling.casefold(): code
    for code, label in (('G/DL', 'g/dL'), ('G/L', 'g/L'))
    for spelling in (code, label)
}
# Unambiguous lab spellings of g/dL seen in imports.
_SPELLINGS.update({'gm/dl': 'G/DL', 'g/100ml': 'G/DL'})


def normalize_unit(value):
    """The stored code for a unit code or display label, or None if unknown."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _SPELLINGS.get(text.casefold())


def canonical_from_pair(value, unit):
    """Return ``(recognised, canonical)`` for a stored pair.

    ``recognised`` is False when this slice cannot convert the unit (mmol/L) or
    the value is not numeric. Callers must not invent a g/dL number then: the
    pair is preserved as a legacy fact but cannot satisfy a threshold.

    A missing unit means the default g/dL, the unit every canonical value is
    recorded in. A clear (``value is None``) is recognised and normalises to
    ``None`` whatever the unit says.

    The result is quantised to ``CANONICAL_QUANTUM`` with half-up rounding, the
    same rounding the database applies, so the value the caller projects is
    exactly the value stored in ``hemoglobin_g_dl``.
    """
    if value is None:
        return True, None
    code = normalize_unit(unit)
    if code is None and unit in (None, ''):
        code = DEFAULT_UNIT
    factor = UNIT_FACTORS.get(code or '')
    if factor is None:
        return False, None
    try:
        canonical = (Decimal(str(value)) * factor).quantize(
            CANONICAL_QUANTUM, rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError):
        return False, None
    if not canonical.is_finite():
        return False, None
    return True, canonical


def canonical_in_range(value):
    """Whether a canonical value fits the `hemoglobin_g_dl` column.

    A concentration is never negative; creatinine and chemistry already refuse
    one, and a negative Hb would satisfy every "< N g/dL" anaemia threshold.
    """
    if value is None:
        return True
    return 0 <= value <= CANONICAL_LIMIT


def same_number(left, right):
    """Compare a stored pair value with a canonical value without float noise."""
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return False


#: canonical field -> (source value field, source unit field)
SOURCE_PAIRS = {CANONICAL_FIELD: (PAIR_FIELD, UNIT_FIELD)}
