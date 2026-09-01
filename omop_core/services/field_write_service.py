"""Write-side validation for assertion fields.

The read path (_assertion_value in patient_record_service.py) silently drops
values it cannot parse as boolean for boolean/inverse_boolean assertion fields.
That means a write of ``value_as_string='maybe'`` succeeds at the OMOP layer
but disappears on the next PatientRecord derivation -- the edit does not
round-trip.

This module exposes ``coerce_assertion_value`` which normalises recognised
boolean inputs to their canonical storage form and rejects everything else,
so an "applied" write always survives read-back.
"""

from omop_core.services.patient_record_service import _ASSERTION_FIELDS

# Build a lookup from concept_code -> value_kind for boolean assertion fields.
# Only boolean and inverse_boolean kinds need coercion; string kinds accept
# anything and round-trip as-is.
_BOOLEAN_ASSERTION_CODES: dict[str, str] = {
    code: kind
    for code, (_field, kind) in _ASSERTION_FIELDS.items()
    if kind in ('boolean', 'inverse_boolean')
}

_TRUTHY = frozenset({'true', 'yes', '1'})
_FALSY = frozenset({'false', 'no', '0'})


def coerce_assertion_value(source_value, value_as_number, value_as_string):
    """Validate and coerce a write to a boolean assertion field.

    Parameters
    ----------
    source_value : str or None
        The measurement_source_value or observation_source_value. Used to
        decide whether this write targets a boolean assertion field.
    value_as_number : any
        The numeric value being written (may be None).
    value_as_string : str or None
        The string value being written (may be None).

    Returns
    -------
    tuple (value_as_number, value_as_string, error)
        On success, returns the coerced (number, string, None).
        On failure, returns (None, None, error_message).
        If the source_value is not a boolean assertion code, returns the
        inputs unchanged with no error.
    """
    if source_value not in _BOOLEAN_ASSERTION_CODES:
        return value_as_number, value_as_string, None

    # Determine the raw value from whichever column carries it.
    # value_as_string takes precedence (matches _assertion_value read order).
    raw = None
    if value_as_string not in (None, ''):
        raw = value_as_string
    elif value_as_number is not None:
        raw = value_as_number

    if raw is None:
        # No value provided -- nothing to coerce; let the write proceed
        # (an assertion with no answer is valid and means "unknown").
        return value_as_number, value_as_string, None

    # Python bool is a subclass of int, so check it before numeric.
    if isinstance(raw, bool):
        canonical = raw
    elif isinstance(raw, (int, float)):
        if raw == 1:
            canonical = True
        elif raw == 0:
            canonical = False
        else:
            return None, None, (
                f'Boolean assertion field (code {source_value}) '
                f'requires a boolean value. Got numeric {raw!r}; '
                f'only 0 and 1 are accepted.'
            )
    elif isinstance(raw, str):
        normalised = raw.strip().casefold()
        if normalised in _TRUTHY:
            canonical = True
        elif normalised in _FALSY:
            canonical = False
        else:
            return None, None, (
                f'Boolean assertion field (code {source_value}) '
                f'requires a boolean value. Got {raw!r}; '
                f'accepted values are: true, false, yes, no, 1, 0.'
            )
    else:
        return None, None, (
            f'Boolean assertion field (code {source_value}) '
            f'requires a boolean value. Got {type(raw).__name__}.'
        )

    # Store as canonical string 'True'/'False' in value_as_string,
    # and 1.0/0.0 in value_as_number (matching FHIR valueBoolean convention).
    return (1.0 if canonical else 0.0), str(canonical), None
