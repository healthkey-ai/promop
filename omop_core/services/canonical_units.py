"""Instance-selected LOINC units; read-time normalization never changes source facts.

Conversions are deliberately limited to explicit, property-compatible UCUM scales.
No inference of molecular weight, assay calibration, missing units or free text.
"""
from dataclasses import dataclass
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
RELATIONSHIP_PROPERTY_NAMES = {
    **PROPERTY_NAMES,
    'mass concentration': 'MCnc',
    'substance concentration': 'SCnc',
    'number concentration': 'NCnc',
    'catalytic activity concentration': 'CCnc',
}
RELATIONSHIP_SCALE_TYPES = {
    'qn': 'Qn',
    'quantitative': 'Qn',
    'ord': 'Ord',
    'ordinal': 'Ord',
    'nom': 'Nom',
    'nominal': 'Nom',
    'nar': 'Nar',
    'narrative': 'Nar',
    'doc': 'Doc',
    'document': 'Doc',
    'multi': 'Multi',
    'multi-dimensional': 'Multi',
}
UNIT_ALIASES = {'cells/uL': '/uL', 'cell/uL': '/uL', '{cells}/uL': '/uL', '#/uL': '/uL',
                'cells/L': '/L', 'cell/L': '/L', '{cells}/L': '/L', '#/L': '/L',
                '10^3/uL': '10*3/uL', '10^9/L': '10*9/L', '10^6/uL': '10*6/uL',
                '10^12/L': '10*12/L', 'K/uL': '10*3/uL', 'G/L': '10*9/L'}


@dataclass(frozen=True)
class LoincAxes:
    """Relationship-backed LOINC axes used by normalization and concept APIs.

    ``None`` means Athena supplied no active relationship, so callers may use
    the legacy Loinc.csv/name fallbacks. An unrecognized non-None value is
    authoritative and therefore fails closed.
    """

    property_name: str | None = None
    scale_type_name: str | None = None
    class_code: str | None = None
    class_name: str | None = None

    @property
    def property_code(self):
        if self.property_name is None:
            return None
        return RELATIONSHIP_PROPERTY_NAMES.get(self.property_name.strip().casefold(), '')

    @property
    def scale_type_code(self):
        if self.scale_type_name is None:
            return None
        return RELATIONSHIP_SCALE_TYPES.get(self.scale_type_name.strip().casefold(), '')


def loinc_axes_for_concepts(concepts):
    """Load property, scale and class axes for many LOINC concepts in one query."""
    from django.db.models import Q
    from omop_core.models import ConceptRelationship

    concept_ids = {
        concept.pk for concept in concepts
        if concept.vocabulary_id == 'LOINC' and concept.domain_id == 'Measurement'
    }
    if not concept_ids:
        return {}

    values = {}
    rows = (
        ConceptRelationship.objects
        .filter(
            concept_1_id__in=concept_ids,
            invalid_reason__isnull=True,
        )
        .filter(
            Q(relationship_id__in=('Has property', 'Has scale type'))
            | Q(relationship_id='Is a', concept_2__concept_class_id='LOINC Class')
        )
        .order_by('concept_1_id', 'relationship_id', 'concept_2_id')
        .values_list(
            'concept_1_id', 'relationship_id',
            'concept_2__concept_code', 'concept_2__concept_name',
        )
    )
    for concept_id, relationship_id, target_code, target_name in rows:
        axes = values.setdefault(concept_id, {})
        if relationship_id == 'Has property':
            axes.setdefault('property_name', target_name)
        elif relationship_id == 'Has scale type':
            axes.setdefault('scale_type_name', target_name)
        else:
            axes.setdefault('class_code', target_code)
            axes.setdefault('class_name', target_name)
    return {concept_id: LoincAxes(**axes) for concept_id, axes in values.items()}


def unit_code(unit):
    unit = (unit or '').strip().replace('µ', 'u').replace('μ', 'u')
    return UNIT_ALIASES.get(unit, unit)


def property_for(concept, metadata=None, axes=None):
    if concept.vocabulary_id != 'LOINC' or concept.domain_id != 'Measurement':
        return ''
    if axes and axes.scale_type_name is not None:
        if axes.scale_type_code != 'Qn':
            return ''
    elif metadata and metadata.scale_type not in ('', 'Qn'):
        return ''
    if axes and axes.property_name is not None:
        return axes.property_code
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
    axes = loinc_axes_for_concepts(p.concept for p in configured)
    for preference in configured:
        concept = preference.concept
        prop = property_for(concept, metadata.get(concept.concept_code), axes.get(concept.pk))
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


def _base_unit(property_code):
    """Return the base unit (factor '1') for a property group, or None."""
    for unit, factor in UNIT_GROUPS.get(property_code, {}).items():
        if factor == '1':
            return unit
    return None


def _find_property(src, dst):
    """Return the property code whose group contains both units, or ''."""
    for prop, units in UNIT_GROUPS.items():
        if src in units and dst in units:
            return prop
    return ''


def resolve_mapping_conversion(concept, destination_unit_concept=None,
                               source_unit='', quantity=None):
    """Compute unit conversion info for a resolved code mapping.

    Returns a dict with: source_unit, destination_unit, destination_unit_concept_id,
    destination_unit_source, source_quantity, converted_quantity, property_code, match_type.
    """
    src = unit_code(source_unit)
    result = {
        'source_unit': src or None,
        'destination_unit': None,
        'destination_unit_concept_id': None,
        'destination_unit_source': None,
        'source_quantity': quantity,
        'converted_quantity': None,
        'property_code': None,
        'match_type': None,
    }

    # Determine destination unit
    if destination_unit_concept is not None:
        dst = unit_code(destination_unit_concept.concept_code)
        result['destination_unit'] = dst
        result['destination_unit_concept_id'] = destination_unit_concept.concept_id
        result['destination_unit_source'] = 'mapping'
    else:
        axes = loinc_axes_for_concepts([concept])
        prop = property_for(concept, axes=axes.get(concept.pk))
        if prop:
            dst = _base_unit(prop)
            result['destination_unit'] = dst
            result['destination_unit_source'] = 'property'
            result['property_code'] = prop
        else:
            dst = None

    if not src or not dst:
        return result

    # Determine match type and convert
    src_norm = unit_code(src)
    dst_norm = unit_code(dst)

    if src_norm == dst_norm:
        result['match_type'] = 'exact'
        if quantity is not None:
            result['converted_quantity'] = str(Decimal(str(quantity)))
        prop = _find_property(src_norm, dst_norm) if not result['property_code'] else result['property_code']
        result['property_code'] = prop or result['property_code']
        return result

    prop = _find_property(src_norm, dst_norm)
    if prop:
        result['match_type'] = 'convertible'
        result['property_code'] = prop
        if quantity is not None:
            try:
                converted = convert(quantity, src_norm, dst_norm, prop)
                result['converted_quantity'] = str(converted)
            except ValueError:
                result['match_type'] = 'incompatible'
    else:
        result['match_type'] = 'incompatible'

    return result


def measurement_normalized(measurement, preferences):
    unit = measurement.unit_source_value
    if not unit or not unit.strip():
        concept = measurement.unit_concept
        unit = concept.concept_code if concept and concept.vocabulary_id == 'UCUM' else None
    concept_id = measurement.measurement_concept_id or measurement.measurement_source_concept_id
    return normalize(preferences.get(concept_id),
                     measurement.value_as_number, unit, measurement.range_low, measurement.range_high)
