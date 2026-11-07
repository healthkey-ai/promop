"""The creatinine source pair (#2318): mg/dL or umol/L, normalised to mg/dL.

The record keeps what the lab reported, and the canonical mg/dL column is the
storage/projection read-model derived from it. The conversion lives here so the
serializer, derivation and both form writers use one factor rather than each
spelling it again.

The divisor is **88.42**, not 88.4: that is the factor the installed
`exact_matching` CKD-EPI calculator already uses.

The canonical is a storage and projection read-model, quantised to the column's
two decimals. eGFR must be computed from the **pair**, not from the canonical:
the 0.01 rounding can move a value across a threshold (an 18-year-old male at
148.63 umol/L gives 59.99 from the pair and 60.03 from the rounded 1.68 mg/dL).
That is why the matcher reads `serum_creatinine_level` and why the consuming
adapter keeps it as the eGFR source.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

#: The writable source value and the unit stored beside it.
PAIR_FIELD = 'serum_creatinine_level'
UNIT_FIELD = 'serum_creatinine_level_units'
#: The normalised read-model projected to OMOP. Not the eGFR source: see below.
CANONICAL_FIELD = 'serum_creatinine_mg_dl'

DEFAULT_UNIT = 'MG/DL'

#: Stored `TextChoices` code -> divisor into the canonical mg/dL value.
UNIT_DIVISORS = {'MG/DL': Decimal('1'), 'UMOL/L': Decimal('88.42')}

#: `PatientRecord.serum_creatinine_mg_dl` is `DecimalField(max_digits=5,
#: decimal_places=2)`. The derived value must already have that precision:
#: persistence and projection read the same number. eGFR does not: it reads the
#: pair, because this rounding can cross a threshold.
CANONICAL_QUANTUM = Decimal('0.01')
CANONICAL_LIMIT = Decimal('999.99')

_SPELLINGS = {
    spelling.casefold(): code
    for code, label in (('MG/DL', 'mg/dL'), ('UMOL/L', 'umol/L'))
    for spelling in (code, label)
}


def normalize_unit(value):
    """The stored code for a unit code or display label, or None if unknown."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _SPELLINGS.get(text.casefold())


def canonical_from_pair(value, unit):
    """Return ``(recognised, canonical)`` for a stored creatinine pair.

    ``recognised`` is False when the unit is unknown or the value is not
    numeric. Callers must not invent an mg/dL number then: the pair is preserved
    as a legacy fact but cannot satisfy a threshold.

    A missing unit means the default mg/dL, the unit every canonical value is
    recorded in. A clear (``value is None``) is recognised and normalises to
    ``None`` whatever the unit says.

    The result is quantised to ``CANONICAL_QUANTUM`` with half-up rounding, the
    same rounding the database applies, so the value the caller projects is
    exactly the value stored in ``serum_creatinine_mg_dl``.
    """
    if value is None:
        return True, None
    code = normalize_unit(unit)
    if code is None and unit in (None, ''):
        code = DEFAULT_UNIT
    divisor = UNIT_DIVISORS.get(code or '')
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


def canonical_in_range(value):
    """Whether a canonical value fits the `serum_creatinine_mg_dl` column."""
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
