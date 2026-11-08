"""Direct bilirubin's source pair (#2318): mg/dL or umol/L, no canonical column.

Unlike the other chemistry pairs, direct bilirubin has no separate canonical
read-model column: `serum_bilirubin_level_direct` + its unit are the stored
fact, and the matcher converts them itself (`BilirubinConvertor`, cf 17.1). The
legacy `serum_bilirubin_level` alias has no unit of its own, so it is a
read-only mirror of the **normalised mg/dL** value; otherwise a number in
umol/L would be indistinguishable from mg/dL to its consumers.

The pair column is `Decimal(max_digits=10, decimal_places=2)`, so the storage
quantum is 0.01 and the limit 99999999.99 — not the `(5, 1)` chemistry limit.

A missing unit on the known mass/volume sources is the documented default
mg/dL, matching the descriptor's recipe and the matcher's `default_unit`. An
explicitly named, unsupported unit is refused rather than guessed.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

#: The writable source value and the unit stored beside it.
PAIR_FIELD = 'serum_bilirubin_level_direct'
UNIT_FIELD = 'serum_bilirubin_level_direct_units'
#: The unitless legacy mirror, normalised to mg/dL.
ALIAS_FIELD = 'serum_bilirubin_level'

DEFAULT_UNIT = 'MG/DL'

#: Stored `TextChoices` code -> divisor into the mg/dL mirror.
UNIT_DIVISORS = {'MG/DL': Decimal('1'), 'UMOL/L': Decimal('17.1')}

#: Stored code -> display notation for OMOP `unit_source_value`.
UNIT_LABELS = {'MG/DL': 'mg/dL', 'UMOL/L': 'umol/L'}

#: `PatientRecord.serum_bilirubin_level_direct` is `Decimal(10, 2)`, as is the
#: alias column. The alias gets this precision; the pair is stored as reported
#: (quantised to what the column can hold).
CANONICAL_QUANTUM = Decimal('0.01')
CANONICAL_LIMIT = Decimal('99999999.99')

_SPELLINGS = {
    spelling.casefold(): code
    for code, label in (('MG/DL', 'mg/dL'), ('UMOL/L', 'umol/L'))
    for spelling in (code, label)
}


def _normalise(unit):
    if unit is None:
        return ''
    return (str(unit).strip().casefold()
            .replace('μ', 'u').replace('µ', 'u')
            .replace(' ', ''))


def normalize_unit(unit):
    """The stored code for a unit code or display label, or None if unknown."""
    return _SPELLINGS.get(_normalise(unit))


def unit_label(code):
    """The OMOP display notation for a stored unit code, or None."""
    return UNIT_LABELS.get(code or '')


def pair_value(value):
    """Return ``(recognised, value)`` for the stored pair column.

    A clear is recognised and returns ``None``. The pair keeps the reported
    number, quantised to the column's two decimals, so the value read back is
    the value the caller wrote.
    """
    if value is None:
        return True, None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False, None
    if isinstance(value, bool) or not number.is_finite():
        return False, None
    if number < 0 or number > CANONICAL_LIMIT:
        return False, None
    return True, number.quantize(CANONICAL_QUANTUM, rounding=ROUND_HALF_UP)


def has_excess_precision(value):
    """Whether a write carries more precision than the column stores.

    Imports are quantised once when materialised; a client write is refused so
    the value it reads back is the value it sent.
    """
    if value is None:
        return False
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if isinstance(value, bool) or not number.is_finite():
        return False
    try:
        return number != number.quantize(CANONICAL_QUANTUM)
    except InvalidOperation:
        return True


def alias_from_pair(value, unit):
    """Return ``(recognised, mg/dL alias)`` for a stored pair.

    ``recognised`` is False when the unit is explicitly unsupported or the value
    is not a usable number; the alias must then stay unknown rather than repeat
    a number whose scale is not established.

    A missing unit is the documented mg/dL default of the mass/volume sources.
    A clear returns ``None``.
    """
    if value is None:
        return True, None
    code = normalize_unit(unit)
    if code is None and _normalise(unit) == '':
        code = DEFAULT_UNIT
    divisor = UNIT_DIVISORS.get(code or '')
    if divisor is None:
        return False, None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False, None
    if isinstance(value, bool) or not number.is_finite():
        return False, None
    # Check the source range before quantizing: an out-of-range value would
    # otherwise raise InvalidOperation on the quantize, not return "unknown".
    if number < 0 or number > CANONICAL_LIMIT:
        return False, None
    alias = number / divisor
    if not alias.is_finite() or alias < 0 or alias > CANONICAL_LIMIT:
        return False, None
    try:
        alias = alias.quantize(CANONICAL_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return False, None
    return True, alias
