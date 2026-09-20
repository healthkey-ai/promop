"""Instance-selected LOINC units; read-time normalization never changes source facts.

Conversions are deliberately limited to explicit, property-compatible UCUM scales.
No inference of molecular weight, assay calibration, missing units or free text.
"""
from decimal import Decimal, InvalidOperation
import re

# Factors to a base unit within each measured property. UCUM is case-sensitive.
UNIT_GROUPS = {
    'MCnc': {'g/L': '1', 'g/dL': '10', 'mg/L': '.001', 'mg/dL': '.01',
             'mg/mL': '1', 'ug/L': '.000001', 'ug/mL': '.001', 'ng/mL': '.000001', 'pg/mL': '.000000001'},
    'SCnc': {'mol/L': '1', 'mmol/L': '.001', 'umol/L': '.000001', 'nmol/L': '.000000001'},
    'NCnc': {'/L': '1', '/uL': '1000000', '10*3/uL': '1000000000', '10*9/L': '1000000000', '10*6/uL': '1000000000000', '10*12/L': '1000000000000'},
    'CCnc': {'kat/L': '60000000', 'ukat/L': '60', 'U/L': '1', 'U/mL': '1000'},
    'Time': {'s': '1', 'min': '60', 'h': '3600', 'd': '86400'},
    'Mass': {'g': '1', 'mg': '.001', 'ug': '.000001', 'kg': '1000'},
    'Len': {'m': '1', 'cm': '.01', 'mm': '.001'},
    'Vol': {'L': '1', 'dL': '.1', 'mL': '.001', 'uL': '.000001'},
    'Temp': {'Cel': '1', '[degF]': '1'},
}
PROPERTY_NAMES = {
    'mass/volume': 'MCnc', 'moles/volume': 'SCnc', '#/volume': 'NCnc',
    'catalytic activity/volume': 'CCnc', 'time': 'Time', 'mass': 'Mass',
    'length': 'Len', 'volume': 'Vol', 'temperature': 'Temp',
}
UNIT_ALIASES = {'cells/uL': '/uL', 'cell/uL': '/uL', '{cells}/uL': '/uL', '#/uL': '/uL',
                'cells/L': '/L', 'cell/L': '/L', '{cells}/L': '/L', '#/L': '/L',
                '10^3/uL': '10*3/uL', '10^9/L': '10*9/L', '10^6/uL': '10*6/uL',
                '10^12/L': '10*12/L', 'K/uL': '10*3/uL', 'G/L': '10*9/L'}


def unit_code(unit):
    unit = (unit or '').strip().replace('µ', 'u').replace('μ', 'u')
    return UNIT_ALIASES.get(unit, unit)


def property_for(concept, metadata=None):
    if concept.vocabulary_id != 'LOINC' or concept.domain_id != 'Measurement':
        return ''
    # Authoritative imported PROPERTY takes precedence; unknown properties fail closed.
    if metadata and metadata.scale_type not in ('', 'Qn'):
        return ''
    if metadata and metadata.property:
        return metadata.property
    match = re.search(r'\[([^]]+)\]', concept.concept_name or '')
    return PROPERTY_NAMES.get(match.group(1).lower(), '') if match else ''


def convert(value, source, target, property_code):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('Invalid numeric value.')
    units = UNIT_GROUPS.get(property_code, {})
    source, target = unit_code(source), unit_code(target)
    if source not in units or target not in units:
        raise ValueError('Source unit is missing, unsupported or incompatible with the measurement property.')
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError('Invalid numeric value.')
        if source == target:
            result = number
        elif property_code == 'Temp':
            celsius = (number - 32) * Decimal(5) / 9 if source == '[degF]' else number
            result = celsius * 9 / 5 + 32 if target == '[degF]' else celsius
        else:
            result = number * Decimal(units[source]) / Decimal(units[target])
        if not result.is_finite() or abs(result) > Decimal('1e100'):
            raise ValueError('Converted value is outside the supported range.')
        return result
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError('Invalid numeric value.') from exc


def policies():
    """A fresh request snapshot; never cache instance choices across requests/workers."""
    from omop_core.models import CanonicalUnitPreference, LoincCodeClass
    configured = list(CanonicalUnitPreference.objects.exclude(unit='').select_related('concept'))
    metadata = LoincCodeClass.objects.filter(loinc_num__in=[p.concept.concept_code for p in configured]).in_bulk()
    for preference in configured:
        concept = preference.concept
        prop = property_for(concept, metadata.get(concept.concept_code))
        if (concept.standard_concept != 'S' or concept.invalid_reason or prop != preference.property
                or preference.unit not in UNIT_GROUPS.get(prop, {})):
            preference.validation_error = 'The vocabulary changed; an administrator must review the canonical unit setting.'
    return {p.concept_id: p for p in configured}


def normalize(preference, value, source_unit, low=None, high=None):
    if preference is None:
        return None
    result = {'unit': preference.unit, 'revision': preference.revision,
              'value': None, 'range_low': None, 'range_high': None, 'error': None}
    try:
        if getattr(preference, 'validation_error', None):
            raise ValueError(preference.validation_error)
        if value is None:
            raise ValueError('No numeric result is available to convert; inspect the original result.')
        # Never reinterpret value_string or infer missing units.
        convert(0, source_unit, preference.unit, preference.property)
        result.update(value=convert(value, source_unit, preference.unit, preference.property),
                      range_low=convert(low, source_unit, preference.unit, preference.property),
                      range_high=convert(high, source_unit, preference.unit, preference.property))
    except ValueError as exc:
        result['error'] = str(exc)
    return result


def measurement_normalized(measurement, preferences):
    unit = measurement.unit_source_value
    if not unit or not unit.strip():
        concept = measurement.unit_concept
        unit = concept.concept_code if concept and concept.vocabulary_id == 'UCUM' else None
    concept_id = measurement.measurement_concept_id or measurement.measurement_source_concept_id
    return normalize(preferences.get(concept_id),
                     measurement.value_as_number, unit, measurement.range_low, measurement.range_high)
