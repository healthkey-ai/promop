"""LOINC concept unit and measurement-type classification.

Pure functions, no view or request dependency. Used by the concept search view
and by code-mapping serialisation to show curators the expected unit for a
LOINC Measurement concept.
"""
from functools import lru_cache


@lru_cache(maxsize=1)
def get_loinc_to_unit() -> dict[str, str]:
    """Lazily build LOINC-code -> unit mapping from LAB_FIELD_TO_LOINC."""
    from omop_core.services.mappings import LAB_FIELD_TO_LOINC
    return {
        code: unit for code, unit, _display in LAB_FIELD_TO_LOINC.values() if unit
    }


_QUANTITATIVE_LOINC_NAME_MARKERS = (
    'mass/', 'moles/', '#/', 'units/', 'volume fraction', 'catalytic activity',
    'ratio', 'fraction', 'clearance', ' rate', ' score', ' time',
)
_QUALITATIVE_LOINC_NAME_MARKERS = (
    '[presence]', '[ordinal]', '[narrative]', '[interpretation]', '[type]',
    '[finding]', 'susceptibility',
)


def measurement_input_type(concept_name, suggested_unit):
    """Return the safe qualitative/quantitative cue available in OMOP data.

    OMOP's Concept table does not retain LOINC's Scale Type. A curated unit is
    conclusive, and common LOINC display-name markers cover unitless numeric
    measurements (for example, renal clearance) and qualitative Presence
    results without pretending an unknown result has a known scale.
    """
    if suggested_unit:
        return 'quantitative'
    name = (concept_name or '').casefold()
    if any(marker in name for marker in _QUANTITATIVE_LOINC_NAME_MARKERS):
        return 'quantitative'
    if any(marker in name for marker in _QUALITATIVE_LOINC_NAME_MARKERS):
        return 'qualitative'
    return ''


def concept_unit_fields(concept):
    """Return measurement_type/suggested_unit dict for a LOINC Measurement concept.

    Returns an empty dict for non-LOINC or non-Measurement concepts so callers
    can unconditionally unpack it into their serialisation dicts.
    """
    if (
        concept is None
        or getattr(concept, 'domain_id', None) != 'Measurement'
        or getattr(concept, 'vocabulary_id', None) != 'LOINC'
    ):
        return {}
    loinc_units = get_loinc_to_unit()
    unit = loinc_units.get(concept.concept_code, '')
    mtype = measurement_input_type(concept.concept_name, unit)
    result = {}
    if mtype:
        result['measurement_type'] = mtype
    if unit:
        result['suggested_unit'] = unit
    return result
