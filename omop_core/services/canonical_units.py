"""Instance-selected LOINC units and write-time normalization.

Conversions are deliberately limited to explicit, property-compatible UCUM scales.
No inference of molecular weight, assay calibration, missing units or free text.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
_KNOWN_UNITS = frozenset(unit for units in UNIT_GROUPS.values() for unit in units) | frozenset(UNIT_ALIASES.values())


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
    'micromols': 'umol', 'micromol': 'umol',
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
    index = {}
    for unit in _KNOWN_UNITS:
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


def is_known_unit(unit):
    """Whether ``unit_code`` reads this source unit as a unit promop knows."""
    return unit_code(unit) in _KNOWN_UNITS


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
    concept_id = measurement.measurement_concept_id or measurement.measurement_source_concept_id
    return normalize(preferences.get(concept_id), measurement.value_as_number,
                     measurement_stored_unit(measurement),
                     measurement.range_low, measurement.range_high)


# ---------------------------------------------------------------------------
# Which unit a stored value is in (#2249)
# ---------------------------------------------------------------------------
#
# Write-time conversion rewrites value_as_number, its range and unit_concept,
# but keeps unit_source_value. A UCUM unit_concept that names a different,
# convertible unit is a hint of conversion, not proof: some rows carry one
# beside the number as sent. The sent number in value_source_value decides.
# A row it cannot decide is unknown, and its value is not read as either unit.

_SOURCE, _CONVERTED, _UNKNOWN = 'source', 'converted', 'unknown'
_STORED_SCALE = Decimal('0.00001')  # Measurement.value_as_number


def _as_stored(number):
    """The number as the column stores it; PostgreSQL rounds half away from zero."""
    try:
        return number.quantize(_STORED_SCALE, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _sent_number(text):
    try:
        number = Decimal((text or '').strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _reading(value, unit_source_value, unit_concept_code, unit_vocabulary_id, value_source_value):
    """Whether *value* is in the source unit, converted to the concept's, or unknown.

    Returns the verdict and the number as sent. A converted value matches the
    conversion of the sent number at the column's scale, from the number as
    sent or as first stored, since older writes converted the rounded value.
    """
    source = (unit_source_value or '').strip()
    if value is None or not source or not unit_concept_code or unit_vocabulary_id != 'UCUM':
        return _SOURCE, None
    source, stored = unit_code(source), unit_code(unit_concept_code)
    prop = _find_property(source, stored) if source != stored else ''
    if not prop:
        return _SOURCE, None  # one unit spelled two ways, or labels this module cannot relate
    sent = _sent_number(value_source_value)
    if sent is None:
        return _UNKNOWN, None
    value = Decimal(str(value))
    candidates = {sent, _as_stored(sent)} - {None}
    if value in candidates:
        return _SOURCE, sent
    for number in candidates:
        try:
            if value == _as_stored(convert(number, source, stored, prop)):
                return _CONVERTED, sent
        except ValueError:
            continue
    return _UNKNOWN, sent


def _reading_args(measurement):
    unit = measurement.unit_concept if measurement.unit_concept_id else None
    return {
        'value': measurement.value_as_number,
        'unit_source_value': measurement.unit_source_value,
        'unit_concept_code': unit.concept_code if unit else None,
        'unit_vocabulary_id': unit.vocabulary_id if unit else None,
        'value_source_value': measurement.value_source_value,
    }


def stored_unit(**reading):
    """The unit value_as_number and its range are stored in, or None when unknown or absent."""
    verdict, _ = _reading(**reading)
    if verdict == _CONVERTED:
        return reading['unit_concept_code']
    if verdict == _UNKNOWN:
        return None
    if (reading['unit_source_value'] or '').strip():
        return reading['unit_source_value']
    return reading['unit_concept_code']


def measurement_stored_unit(measurement):
    """stored_unit() for a Measurement instance."""
    return stored_unit(**_reading_args(measurement))


def is_unproven(measurement):
    """True when no unit can be shown for value_as_number: show neither it nor its range."""
    return _reading(**_reading_args(measurement))[0] == _UNKNOWN


def value_in_source_unit(measurement):
    """value_as_number in unit_source_value's unit, or None when that cannot be shown."""
    verdict, sent = _reading(**_reading_args(measurement))
    if verdict == _CONVERTED:
        return sent
    if verdict == _UNKNOWN:
        return None
    return measurement.value_as_number


# ---------------------------------------------------------------------------
# Write-time unit normalization
# ---------------------------------------------------------------------------

def _curated_target_unit(mapping):
    """The unit a curator chose for the mapping's rows: ``(unit, property)``.

    Write-time conversion happens only on this explicit choice (#2157). An
    inferred LOINC base unit is not one: g/L and /L do not fit the column for
    ng/mL or 10*3/uL results. Nor is a ``CanonicalUnitPreference``, which is a
    read-time display setting over stored values. Returns ``(None, None)`` when
    the mapping names no convertible unit.
    """
    duc = getattr(mapping, 'destination_unit_concept', None)
    if duc is None:
        return None, None
    dst = unit_code(duc.concept_code)
    for prop, units in UNIT_GROUPS.items():
        if dst in units:
            return dst, prop
    return None, None


# value_as_number, range_low and range_high are Decimal(15, 5).
_STORED_LIMIT = Decimal('1e10')
_STORED_STEP = Decimal('0.00001')
_VALUE_FIELDS = ('value_as_number', 'range_low', 'range_high')


def _storable(value):
    """True when the column holds *value* exactly: no overflow and no rounding."""
    return value is None or (abs(value) < _STORED_LIMIT and value == value.quantize(_STORED_STEP))


def _convert_row(row, src, target_unit, prop, target_ucum):
    """Convert the row's value and range in place.

    Returns False, leaving the row untouched, when any of them cannot be
    converted or stored exactly: a value moved without its unit, or a unit
    without its value, would mislabel the result.
    """
    try:
        converted = [convert(getattr(row, f), src, target_unit, prop) for f in _VALUE_FIELDS]
    except (ValueError, InvalidOperation):
        return False
    if not all(_storable(v) for v in converted):
        return False
    if not (row.value_source_value or '').strip():
        # The number as sent is what proves the conversion on read (#2249).
        row.value_source_value = format(Decimal(str(row.value_as_number)).normalize(), 'f')
    for field, value in zip(_VALUE_FIELDS, converted):
        setattr(row, field, value)
    if target_ucum:
        row.unit_concept_id = target_ucum.concept_id
    return True


def _ucum_concept(unit_str):
    """Look up the UCUM Concept for a unit string, or None."""
    from omop_core.models import Concept
    return (
        Concept.objects
        .filter(vocabulary_id='UCUM', concept_code=unit_str,
                standard_concept='S', invalid_reason__isnull=True)
        .first()
    )


def normalize_measurement_units(qs, mapping):
    """Convert value/unit on Measurement rows to the mapping's curated unit.

    *qs* is a queryset of ``Measurement`` rows (already filtered to the rows
    that were just written, repointed or resolved).  *mapping* is the
    ``SourceCodeConceptMapping`` they belong to; only its
    ``destination_unit_concept`` is a target (see ``_curated_target_unit``).
    Rows an axis variant owns are the caller's to leave out: a variant has
    no curated unit of its own.

    Only rows with a non-null ``value_as_number`` and a recognisable
    ``unit_source_value`` are touched.  ``unit_source_value`` is never changed
    (it records the hospital's original unit).  Rows whose source unit is
    already the target, whose source unit is incompatible, or whose converted
    value or range the column cannot hold exactly are skipped.

    Returns the number of rows whose ``value_as_number`` was rewritten.
    """
    target_unit, prop = _curated_target_unit(mapping)
    if not target_unit or not prop:
        return 0

    target_ucum = _ucum_concept(target_unit)

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

        # Fetch rows in batches, convert individually, bulk_update. Paged by
        # pk, not offset: a converted row leaves qs (its unit_concept is now
        # the target), so an offset would skip as many rows as it converted.
        batch_qs = qs.filter(
            unit_source_value=raw, value_as_number__isnull=False,
        ).order_by('pk')
        BATCH = 1000
        last_pk = None
        while True:
            page = batch_qs if last_pk is None else batch_qs.filter(pk__gt=last_pk)
            rows = list(page[:BATCH])
            if not rows:
                break
            last_pk = rows[-1].pk
            changed = [row for row in rows
                       if _convert_row(row, src, target_unit, prop, target_ucum)]
            if changed:
                fields = [*_VALUE_FIELDS, 'unit_concept_id', 'value_source_value']
                from omop_core.models import Measurement
                Measurement.objects.bulk_update(changed, fields)
                total_normalized += len(changed)

    if total_normalized:
        logger.info(
            'Normalized %d measurement row(s) to %s (%s).',
            total_normalized, target_unit, prop,
        )
    return total_normalized
