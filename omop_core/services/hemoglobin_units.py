"""The hemoglobin source pair (#2318): g/L or g/dL, normalised to g/dL.

The record keeps what the lab reported. Matching, CRAB and the OMOP projection
read the normalised g/dL value, so the conversion lives here: the serializer,
derivation and both form writers use the same helpers rather than each spelling
the factor and the accepted labels again.
"""

#: The writable source value and the unit stored beside it.
PAIR_FIELD = 'hemoglobin_level'
UNIT_FIELD = 'hemoglobin_level_units'
#: The normalised read-model projected to OMOP.
CANONICAL_FIELD = 'hemoglobin_g_dl'

DEFAULT_UNIT = 'G/DL'

#: Stored `TextChoices` code -> multiplier into the canonical g/dL value.
UNIT_FACTORS = {'G/DL': 1.0, 'G/L': 0.1}

_SPELLINGS = {
    spelling.casefold(): code
    for code, label in (('G/DL', 'g/dL'), ('G/L', 'g/L'))
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
    """Return ``(recognised, canonical)`` for a stored pair.

    ``recognised`` is False when this slice cannot convert the unit (mmol/L) or
    the value is not numeric. Callers must not invent a g/dL number then: the
    pair is preserved as a legacy fact but cannot satisfy a threshold.

    A missing unit means the default g/dL, the unit every canonical value is
    recorded in. A clear (``value is None``) is recognised and normalises to
    ``None`` whatever the unit says.
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
        return True, round(float(value) * factor, 4)
    except (TypeError, ValueError):
        return False, None


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
