"""Instance-selected LOINC units and write-time normalization.

Conversions are deliberately limited to explicit, property-compatible UCUM scales.
No inference of molecular weight, assay calibration, missing units or free text.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging
import re

logger = logging.getLogger(__name__)

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


#: Words labs spell units with, and the UCUM symbol each stands for. Matched
#: whole and case-folded, longest first, so "milligrams" is never read as
#: "milli" + "grams".
_UNIT_WORDS = {
    'milligrams': 'mg', 'milligram': 'mg', 'micrograms': 'ug', 'microgram': 'ug', 'mcg': 'ug',
    'nanograms': 'ng', 'nanogram': 'ng', 'picograms': 'pg', 'picogram': 'pg',
    'grams': 'g', 'gram': 'g', 'gms': 'g', 'gm': 'g', 'mgs': 'mg',
    'deciliters': 'dL', 'deciliter': 'dL', 'decilitres': 'dL', 'decilitre': 'dL',
    'milliliters': 'mL', 'milliliter': 'mL', 'millilitres': 'mL', 'millilitre': 'mL',
    'microliters': 'uL', 'microliter': 'uL', 'microlitres': 'uL', 'microlitre': 'uL',
    'liters': 'L', 'liter': 'L', 'litres': 'L', 'litre': 'L',
    'millimoles': 'mmol', 'millimole': 'mmol', 'micromoles': 'umol', 'micromole': 'umol',
    'nanomoles': 'nmol', 'nanomole': 'nmol', 'per': '/',
}
_UNIT_WORD = re.compile(r'[a-z]+')
#: A lab's own annotation after the unit: "g/dL-Historical".
_ANNOTATIONS = ('historical)', 'historical')
#: Longer than any unit; such text is passed through without being read.
_MAX_UNIT_LENGTH = 40


def _without_annotation(folded):
    # String operations, not a regex: units come from uploads, and a pattern
    # with a repeated separator class backtracks on long runs of whitespace.
    for annotation in _ANNOTATIONS:
        if folded.endswith(annotation):
            return folded[:-len(annotation)].rstrip(' \t-_(')
    return folded


def _spelling_index():
    """Case-folded (and slashless) spelling -> the one known unit it names.

    A spelling two units share is left out rather than guessed: ``g/l`` is
    gram per litre, but ``G/L`` (giga per litre) is an alias of ``10*9/L``.
    """
    known = {unit for units in UNIT_GROUPS.values() for unit in units} | set(UNIT_ALIASES.values())
    index = {}
    for unit in known:
        for spelling in {unit.casefold(), unit.casefold().replace('/', '')}:
            index.setdefault(spelling, set()).add(unit)
    for alias in UNIT_ALIASES:
        index.setdefault(alias.casefold(), set()).add(UNIT_ALIASES[alias])
    return {spelling: units.pop() for spelling, units in index.items() if len(units) == 1}


_SPELLINGS = None


def unit_code(unit):
    """The UCUM code for a source unit, or the unit unchanged when unknown.

    Exact UCUM and listed aliases first; then the spellings labs send --
    "gm/dL", "Grams/Deciliter", "GDL", "g/dL-Historical" -- when they name
    exactly one known unit. One and two character units are left as they
    are, because case is all they have (mM is not mm, S is not s).
    """
    global _SPELLINGS
    unit = (unit or '').strip().replace('µ', 'u').replace('μ', 'u')
    if unit in UNIT_ALIASES:
        return UNIT_ALIASES[unit]
    if len(unit) <= 2 or len(unit) > _MAX_UNIT_LENGTH or any(unit in units for units in UNIT_GROUPS.values()):
        return unit
    if _SPELLINGS is None:
        _SPELLINGS = _spelling_index()
    folded = _without_annotation(unit.casefold())
    spelled = _UNIT_WORD.sub(lambda word: _UNIT_WORDS.get(word.group(), word.group()).casefold(), folded)
    spelled = re.sub(r'\s+', '', spelled)
    return _SPELLINGS.get(spelled, unit)


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


# ---------------------------------------------------------------------------
# Write-time unit normalization
# ---------------------------------------------------------------------------

def _resolve_target_unit(mapping, target_concept_id=None):
    """Determine the target UCUM unit and property code.

    Returns ``(target_unit, property_code)`` or ``(None, None)`` when no
    conversion target can be determined.

    *target_concept_id* overrides ``mapping.target_concept_id`` — needed when
    an axis variant is being approved: the variant's target concept differs
    from the parent mapping's, and the mapping's ``destination_unit_concept``
    was chosen for the mapping's own LOINC code, not the variant's.  When an
    override is provided, ``mapping.destination_unit_concept`` is skipped.

    Precedence:
    1. ``mapping.destination_unit_concept`` — curator-set UCUM concept on the
       SCCM row (skipped when *target_concept_id* overrides).
    2. ``CanonicalUnitPreference`` for the target concept.
    3. LOINC property base unit — derived from the target concept's axes.
    """
    effective_concept_id = target_concept_id or getattr(
        mapping, 'target_concept_id', None)

    # 1. Explicit destination_unit_concept on the mapping row.
    #    Skipped for variant overrides: the mapping's unit was picked for its
    #    own LOINC, not the variant's (code_mapping_lookup does the same).
    if target_concept_id is None:
        duc = getattr(mapping, 'destination_unit_concept', None)
        if duc is not None:
            dst = unit_code(duc.concept_code)
            for prop, units in UNIT_GROUPS.items():
                if dst in units:
                    return dst, prop
            return None, None

    # 2. CanonicalUnitPreference for the target concept.
    if effective_concept_id:
        from omop_core.models import CanonicalUnitPreference
        try:
            pref = CanonicalUnitPreference.objects.get(
                concept_id=effective_concept_id)
            if pref.unit and pref.property:
                return pref.unit, pref.property
        except CanonicalUnitPreference.DoesNotExist:
            pass

    # 3. LOINC property base unit — the same fallback resolve_mapping_conversion
    #    uses when destination_unit_concept is None.
    if effective_concept_id:
        from omop_core.models import Concept
        try:
            concept = Concept.objects.get(pk=effective_concept_id)
        except Concept.DoesNotExist:
            return None, None
        axes = loinc_axes_for_concepts([concept])
        prop = property_for(concept, axes=axes.get(concept.pk))
        if prop:
            base = _base_unit(prop)
            if base:
                return base, prop

    return None, None


def _ucum_concept(unit_str):
    """Look up the UCUM Concept for a unit string, or None."""
    from omop_core.models import Concept
    return (
        Concept.objects
        .filter(vocabulary_id='UCUM', concept_code=unit_str,
                standard_concept='S', invalid_reason__isnull=True)
        .first()
    )


def normalize_measurement_units(qs, mapping, target_concept_id=None):
    """Convert value/unit on Measurement rows to the mapping's target unit.

    *qs* is a queryset of ``Measurement`` rows (already filtered to the rows
    that were just repointed or resolved).  *mapping* is the
    ``SourceCodeConceptMapping`` that was applied.

    *target_concept_id*, when provided, overrides the mapping's own target
    concept for unit resolution — needed when an axis variant is approved,
    because the variant's destination LOINC differs from the mapping's.

    Only rows with a non-null ``value_as_number`` and a recognisable
    ``unit_source_value`` are touched.  ``unit_source_value`` is never changed
    (it records the hospital's original unit).  Rows whose source unit is
    already the target, or whose source unit is incompatible, are silently
    skipped.

    Only rows with ``units_as_sent=False`` are converted (#2104).  ``True``
    rows came through the bulk OMOP write, whose caller (the ETL) chose the
    value and unit itself; ``None`` rows predate the column, so who chose them
    is unknown, and they are left as stored too.  Converting either would
    rewrite a value its writer still matches on.

    Returns the number of rows whose ``value_as_number`` was rewritten.
    """
    target_unit, prop = _resolve_target_unit(mapping, target_concept_id)
    if not target_unit or not prop:
        return 0

    target_ucum = _ucum_concept(target_unit)

    unknown = qs.filter(units_as_sent__isnull=True).count()
    if unknown:
        logger.info(
            'Left %d measurement row(s) as stored: written before units_as_sent '
            'was recorded (see classify_measurement_units).', unknown)
    qs = qs.filter(units_as_sent=False)

    # Group rows by their raw unit_source_value so we compute the conversion
    # factor once per spelling, not once per row.
    raw_units = list(
        qs.filter(value_as_number__isnull=False)
        .exclude(unit_source_value__isnull=True)
        .exclude(unit_source_value='')
        .values_list('unit_source_value', flat=True)
        .distinct()
    )
    if not raw_units:
        return 0

    # Skip rows already normalized to the target unit (idempotency guard).
    # Without this, a re-approve would convert already-converted values again
    # using unit_source_value (which is never changed), producing a double
    # conversion.
    if target_ucum:
        qs = qs.exclude(unit_concept_id=target_ucum.concept_id)

    total_normalized = 0
    for raw in raw_units:
        src = unit_code(raw)
        if src == target_unit:
            # Already in the target unit — just update unit_concept_id if needed.
            if target_ucum:
                qs.filter(unit_source_value=raw).update(
                    unit_concept_id=target_ucum.concept_id)
            continue
        if not _find_property(src, target_unit):
            continue

        # Fetch rows in batches, convert individually, bulk_update.
        batch_qs = qs.filter(
            unit_source_value=raw, value_as_number__isnull=False,
        ).order_by('pk')
        BATCH = 1000
        offset = 0
        while True:
            rows = list(batch_qs[offset:offset + BATCH])
            if not rows:
                break
            changed = []
            for row in rows:
                try:
                    new_val = convert(
                        row.value_as_number, src, target_unit, prop)
                    row.value_as_number = new_val
                    if row.range_low is not None:
                        row.range_low = convert(
                            row.range_low, src, target_unit, prop)
                    if row.range_high is not None:
                        row.range_high = convert(
                            row.range_high, src, target_unit, prop)
                    if target_ucum:
                        row.unit_concept_id = target_ucum.concept_id
                    changed.append(row)
                except (ValueError, InvalidOperation):
                    continue
            if changed:
                fields = ['value_as_number', 'range_low', 'range_high',
                          'unit_concept_id']
                from omop_core.models import Measurement
                Measurement.objects.bulk_update(changed, fields)
                total_normalized += len(changed)
            offset += BATCH

    if total_normalized:
        logger.info(
            'Normalized %d measurement row(s) to %s (%s).',
            total_normalized, target_unit, prop,
        )
    return total_normalized


def normalize_measurement_instances(instances, mapping=None,
                                    target_unit=None, property_code=None):
    """Convert value/unit on in-memory Measurement instances before bulk_create.

    Either pass *mapping* (a ``SourceCodeConceptMapping``) to derive the
    target, or pass *target_unit* and *property_code* directly.

    Mutates instances in place.  Returns the count of instances converted.
    """
    if mapping is not None and target_unit is None:
        target_unit, property_code = _resolve_target_unit(mapping)
    if not target_unit or not property_code:
        return 0

    target_ucum = _ucum_concept(target_unit)
    converted = 0
    for inst in instances:
        raw = getattr(inst, 'unit_source_value', None)
        if not raw or inst.value_as_number is None:
            continue
        if getattr(inst, 'units_as_sent', False) is not False:
            continue  # the writer's unit, or unknown (#2104)
        src = unit_code(raw)
        if src == target_unit:
            if target_ucum:
                inst.unit_concept_id = target_ucum.concept_id
            continue
        if not _find_property(src, target_unit):
            continue
        try:
            inst.value_as_number = convert(
                inst.value_as_number, src, target_unit, property_code)
            if inst.range_low is not None:
                inst.range_low = convert(
                    inst.range_low, src, target_unit, property_code)
            if inst.range_high is not None:
                inst.range_high = convert(
                    inst.range_high, src, target_unit, property_code)
            if target_ucum:
                inst.unit_concept_id = target_ucum.concept_id
            converted += 1
        except (ValueError, InvalidOperation):
            continue
    return converted
