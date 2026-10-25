"""The six LOINC axes, on the source code and on every LOINC candidate.

A LOINC code is defined by its fully specified name, built from six parts:
component, property (kind of quantity), time aspect, system (specimen), scale
and method. Two tests get different codes when they differ on any one of them,
so a source code can only map to a LOINC whose axes agree with its own.

Suggest uses that in three places:

* **Retrieval.** When the source's component is known, the LOINC table itself
  is searched on the axes the source has settled (:func:`axis_candidates`).
  Trigram and vector retrieval search names, and a name can only approximate
  an axis.
* **Filtering.** A candidate that disagrees with a settled source axis is not
  a different suggestion, it is a different test, and is never offered
  (:func:`apply`). Excluded candidates are reported with the axis they broke,
  so a curator can see why the obvious answer is missing.
* **Ranking.** Eligible candidates are ordered by how many axes they match.

Where the source axes come from, strongest first:

1. A curator's entry on the mapping (``SourceCodeConceptMapping.loinc_axes``).
2. The source code itself, when it is a loaded LOINC code.
3. A LOINC coding the sending system attached in ``code.coding``.
4. Inference from the FHIR metadata, limited to what the metadata says
   unambiguously: a UCUM unit names a property, a ``valueQuantity`` means a
   quantitative scale, and a specimen display names a system.

A settled axis is usually one value. A unit that fits several properties
settles the property to that *set*: "%" is a mass, number, volume or other
fraction and never a mass concentration, so a candidate is eligible when its
value is any of the set and conflicts otherwise (``AMBIGUOUS_UNIT_PROPERTIES``).

Free-text ``Observation.method`` and an ``effectivePeriod`` are shown as hints
and never become constraints: LOINC's method and time names rarely appear in
free text, and a wrong constraint hides the right answer.
"""
import functools
import re

from omop_core.services.canonical_units import UNIT_GROUPS, unit_code

AXES = ('component', 'property', 'time_aspect', 'system', 'scale_type', 'method_type')
AXIS_LABELS = {
    'component': 'Component',
    'property': 'Property',
    'time_aspect': 'Time',
    'system': 'System',
    'scale_type': 'Scale',
    'method_type': 'Method',
}

#: How a source axis was settled. ``curator`` beats everything; the rest are
#: inference, in decreasing strength.
BASIS_CURATOR = 'curator'
BASIS_SOURCE_CODE = 'source_code'
BASIS_FHIR_CODING = 'fhir_coding'
BASIS_UNIT = 'unit'
BASIS_VALUE_TYPE = 'value_type'
BASIS_SPECIMEN = 'specimen'

MATCH, COMPATIBLE, CONFLICT, UNKNOWN = 'match', 'compatible', 'conflict', 'unknown'

LOINC_VOCABULARIES = {'LOINC', 'http://loinc.org', '2.16.840.1.113883.6.1',
                      'urn:oid:2.16.840.1.113883.6.1'}

#: UCUM units that name exactly one LOINC property. Units that fit more than
#: one are in AMBIGUOUS_UNITS instead and never settle a property.
_EXTRA_UNIT_PROPERTIES = {
    'mEq/L': 'SCnc', 'meq/L': 'SCnc',
    'mm[Hg]': 'Pres', 'mmHg': 'Pres',
    '/min': 'NRat', '{beats}/min': 'NRat', '{breaths}/min': 'NRat',
    'mM': 'SCnc', 'uM': 'SCnc', 'nM': 'SCnc',
    # Lower-case spellings labs send; listed rather than case-folded (above).
    'mg/dl': 'MCnc', 'g/dl': 'MCnc', 'g/l': 'MCnc', 'mg/l': 'MCnc', 'ug/ml': 'MCnc',
    'ng/ml': 'MCnc', 'pg/ml': 'MCnc', 'ug/l': 'MCnc',
    'mmol/l': 'SCnc', 'umol/l': 'SCnc', 'nmol/l': 'SCnc', 'meq/l': 'SCnc',
    # One property on the loaded LOINC table (see AMBIGUOUS_UNIT_PROPERTIES).
    'mIU/L': 'ACnc', 'mIU/mL': 'ACnc', 'g/g': 'MRto',
}
#: Real units that fit more than one LOINC property, and which. "%" is a mass,
#: number, volume or other fraction, or a relative quantity; "U/L" is catalytic
#: (an enzyme) or arbitrary (CA-125, 10334-1, is ACnc) concentration. Such a
#: unit settles the property to this set: a candidate with any of them is
#: eligible, one with a property outside it -- a mass concentration for a "%"
#: -- is a different test.
#:
#: The sets are the properties of the LOINC codes that list the unit among
#: their example units. On an instance with a loaded LOINC table they are read
#: from it (:func:`loaded_unit_properties`), so a release that adds a property
#: is followed; this table is the fallback, from the October 2026 release, a
#: property with a single code treated as noise. Sets are sorted: the order
#: never means anything, and two units with the same set are the same test.
AMBIGUOUS_UNIT_PROPERTIES = {
    unit: tuple(sorted(options)) for unit, options in {
        '%': ('NFr', 'MFr', 'AFr', 'VFr', 'SFr', 'CFr', 'RelACnc', 'LenFr', 'Prctl', 'Ratio',
              'RelRto', 'NRto', 'RelTime', 'RelMCnc'),
        'U/L': ('CCnc', 'ACnc'), 'U/mL': ('ACnc', 'CCnc'),
        'IU/L': ('ACnc', 'CCnc'), '[IU]/L': ('ACnc', 'CCnc'),
        'IU/mL': ('ACnc', 'CCnc'), '[IU]/mL': ('ACnc', 'CCnc'),
        'kU/L': ('ACnc', 'CCnc'),
        '{ratio}': ('Ratio', 'MRto', 'RelRto', 'SRto'),
    }.items()
}
#: Lower-case lab spellings, and the spelling LOINC lists them under.
_CANONICAL_SPELLING = {'u/l': 'U/L', 'u/mL': 'U/mL'}
AMBIGUOUS_UNIT_PROPERTIES.update({alias: AMBIGUOUS_UNIT_PROPERTIES[unit] for alias, unit in _CANONICAL_SPELLING.items()})
#: Units that settle no single property. UNIT_GROUPS lists convertible units,
#: not unambiguous ones, so these are taken back out of it. "1" (dimensionless)
#: says nothing about the property at all.
AMBIGUOUS_UNITS = frozenset({*AMBIGUOUS_UNIT_PROPERTIES, '1'})
_FOLDED_AMBIGUOUS_UNITS = frozenset(u.casefold() for u in AMBIGUOUS_UNITS)
_FOLDED_AMBIGUOUS_PROPERTIES = {unit.casefold(): options for unit, options in AMBIGUOUS_UNIT_PROPERTIES.items()}
#: A code whose example units name a property fewer times than this is noise
#: (LOINC lists "IU/L" on one MCnc code).
_EXAMPLE_UNIT_MIN_CODES = 2


@functools.lru_cache(maxsize=1)
def loaded_unit_properties():
    """``{unit: sorted properties}`` for the ambiguous units, from the loaded
    LOINC table's example units; ``{}`` when no table with example units is
    loaded, and then the static sets apply. Cached per process; a LOINC load
    calls :func:`reset_loaded_unit_properties`."""
    from collections import Counter, defaultdict

    from omop_core.models import LoincCodeClass

    counts = defaultdict(Counter)
    rows = LoincCodeClass.objects.exclude(example_units='').exclude(property='') \
        .values_list('example_units', 'property')
    for example, prop in rows.iterator(chunk_size=5000):
        for token in re.split(r'[;,]\s*', example):
            token = token.strip()
            if token in AMBIGUOUS_UNIT_PROPERTIES:
                counts[token][prop] += 1
    found = {unit: tuple(sorted(p for p, n in c.items() if n >= _EXAMPLE_UNIT_MIN_CODES))
             for unit, c in counts.items()}
    return {unit: options for unit, options in found.items() if options}


def reset_loaded_unit_properties():
    loaded_unit_properties.cache_clear()
UNIT_PROPERTIES = {
    unit: prop
    for unit, prop in {
        **{unit: prop for prop, units in UNIT_GROUPS.items() for unit in units},
        **_EXTRA_UNIT_PROPERTIES,
    }.items()
    if unit not in AMBIGUOUS_UNITS
}


def _fold_unambiguous():
    """Case-folded spellings that still name one property. UCUM is
    case-sensitive -- ``mM`` (millimolar) is not ``mm`` (millimetre) -- so a
    folded spelling two units share is dropped rather than guessed."""
    folded = {}
    for unit, prop in UNIT_PROPERTIES.items():
        folded.setdefault(unit.casefold(), set()).add(prop)
    clashing = {unit.casefold() for unit in UNIT_PROPERTIES} & {
        a.casefold() for a in UNIT_PROPERTIES for b in UNIT_PROPERTIES
        if a != b and a.casefold() == b.casefold() and UNIT_PROPERTIES[a] != UNIT_PROPERTIES[b]}
    return {unit: props.pop() for unit, props in folded.items()
            if len(props) == 1 and unit not in clashing}


_FOLDED_UNIT_PROPERTIES = _fold_unambiguous()

def unit_property(unit):
    """The LOINC property a source unit implies, or ``''``.

    Case-sensitive first, as UCUM is; a case-folded spelling is accepted only
    when no two units collide on it (``mM``/``mm`` do, ``MG/DL`` does not).
    """
    normalized = unit_code(unit)
    if not normalized or normalized in AMBIGUOUS_UNITS:
        return ''
    if normalized in UNIT_PROPERTIES:
        return UNIT_PROPERTIES[normalized]
    if normalized.casefold() in _FOLDED_AMBIGUOUS_UNITS:
        return ''
    # A short unit is all case: S (siemens) is not s (seconds).
    if len(normalized) <= 2:
        return ''
    return _FOLDED_UNIT_PROPERTIES.get(normalized.casefold(), '')


def unit_property_options(unit):
    """The LOINC properties a source unit can imply: one, several, or ``()``.

    One for a unit :func:`unit_property` settles; the set for a unit that fits
    several (``%``, ``U/L``); empty for a unit inference does not know, or one
    that says nothing about the property (``1``).
    """
    normalized = unit_code(unit)
    if not normalized:
        return ()
    prop = unit_property(normalized)
    if prop:
        return (prop,)
    key = normalized if normalized in AMBIGUOUS_UNIT_PROPERTIES else next(
        (u for u in AMBIGUOUS_UNIT_PROPERTIES if u.casefold() == normalized.casefold()), None)
    if key is None:
        return ()
    key = _CANONICAL_SPELLING.get(key, key)
    return tuple(loaded_unit_properties().get(key) or AMBIGUOUS_UNIT_PROPERTIES[key])


def _property_value(options):
    """An axis value from :func:`unit_property_options`: the one property, or
    the set as a list; ``[]`` for none."""
    return options[0] if len(options) == 1 else list(options)


def unit_is_recognised(unit):
    """Whether a unit is one LOINC inference knows: one property, or known
    to fit several. A misspelling or unlisted unit is neither."""
    normalized = unit_code(unit)
    return bool(normalized) and (bool(unit_property(normalized)) or normalized in AMBIGUOUS_UNITS
                                 or normalized.casefold() in _FOLDED_AMBIGUOUS_UNITS)


#: FHIR specimen display -> LOINC system. Exact phrases only: "blood" inside
#: "Blood urea nitrogen" is a serum test, so names are never searched for it.
SPECIMEN_SYSTEMS = {
    'serum': 'Ser',
    'plasma': 'Plas',
    'serum or plasma': 'Ser/Plas',
    'serum/plasma': 'Ser/Plas',
    # Not plain "blood": venous blood is drawn for serum tests, so a "Blood"
    # specimen would exclude every Ser/Plas code. It stays a hint.
    'whole blood': 'Bld',
    'arterial blood': 'BldA',
    'venous blood': 'BldV',
    'capillary blood': 'BldC',
    'urine': 'Urine',
    'cerebrospinal fluid': 'CSF',
    'csf': 'CSF',
    'saliva': 'Saliva',
    'stool': 'Stool',
    'feces': 'Stool',
}

#: The only axis that is legitimately empty: a methodless LOINC. A curator
#: sets it with this value; a cleared axis is ``None``.
METHODLESS = ''


def _norm(value):
    return ' '.join(str(value).split()).casefold()


def is_loinc(vocabulary_id):
    return (vocabulary_id or '').strip() in LOINC_VOCABULARIES


# --- the LOINC side -----------------------------------------------------------

def axes_for_codes(codes):
    """``{loinc_num: {axis: value}}`` from ``LoincCodeClass``, in one query."""
    from omop_core.models import LoincCodeClass

    codes = {code for code in codes if code}
    if not codes:
        return {}
    return {
        row['loinc_num']: {axis: row[axis] for axis in AXES}
        for row in LoincCodeClass.objects.filter(loinc_num__in=codes)
                                         .exclude(component='')
                                         .values('loinc_num', *AXES)
    }


# --- the source side ----------------------------------------------------------

def _resource(metadata):
    if not isinstance(metadata, dict):
        return {}
    inner = metadata.get('fhir')
    return inner if isinstance(inner, dict) else metadata


def _codings(resource):
    code = resource.get('code')
    codings = code.get('coding') if isinstance(code, dict) else None
    return [c for c in codings or [] if isinstance(c, dict)]


def _text_of(value):
    """The display text of a CodeableConcept, Reference or string."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ''
    if value.get('text') or value.get('display'):
        return value.get('text') or value.get('display')
    for coding in value.get('coding') or []:
        if isinstance(coding, dict) and coding.get('display'):
            return coding['display']
    return ''


def _unit(metadata, resource):
    quantity = resource.get('valueQuantity')
    if not isinstance(quantity, dict):
        quantity = metadata.get('fhir_valueQuantity') if isinstance(metadata, dict) else None
    if isinstance(quantity, dict):
        for key in ('code', 'unit'):
            if quantity.get(key):
                return quantity[key], f'valueQuantity.{key}', True
    ranged = metadata.get('reference_range') if isinstance(metadata, dict) else None
    if isinstance(ranged, dict) and ranged.get('unit'):
        return ranged['unit'], 'reference_range.unit', False
    return None, None, False


def _quantitative(metadata, resource):
    if isinstance(resource.get('valueQuantity'), dict):
        return 'valueQuantity present'
    value_types = metadata.get('value_types') if isinstance(metadata, dict) else None
    if isinstance(value_types, dict):
        share = value_types.get('quantity')
        if isinstance(share, (int, float)) and share >= 95:
            return f'{share:g}% of records carry valueQuantity'
    return None


def infer(source_vocabulary_id, source_code, metadata, *, observed_units=None):
    """``(axes, hints)`` the source code settles before any curator input.

    ``axes`` is ``{axis: {'value', 'basis', 'evidence'}}`` for the axes that
    are settled. *observed_units* are the units the code's records arrived in
    across patients, and they come first: they settle the property when every
    one implies the same property, and the scale (quantitative) whenever
    records carry units at all, whatever one sample in the metadata says. A
    code seen in mg/dL and mmol/L is two tests (``axis_variants``), not one
    with a property -- and then the sample unit does not settle it either
    (#2014). ``hints`` is ``{axis: text}`` for what the metadata says about an
    axis without settling it.
    """
    axes, hints = {}, {}
    resource = _resource(metadata)

    def settle(values, basis, evidence):
        for axis in AXES:
            if axis not in axes and axis in values:
                axes[axis] = {'value': values[axis], 'basis': basis, 'evidence': evidence}

    if is_loinc(source_vocabulary_id):
        found = axes_for_codes([source_code]).get(source_code)
        if found:
            settle(found, BASIS_SOURCE_CODE, f'Source code is LOINC {source_code}')

    loinc_codings = [c.get('code') for c in _codings(resource)
                     if is_loinc(c.get('system')) and c.get('code')]
    if loinc_codings and len(axes) < len(AXES):
        known = axes_for_codes(loinc_codings)
        for code in loinc_codings:
            if code in known:
                settle(known[code], BASIS_FHIR_CODING, f'code.coding carries LOINC {code}')
                break

    unit, where, observed = _unit(metadata, resource)
    # Only units inference recognises: a misspelling is not a second property.
    units = sorted({u for u in observed_units or [] if u and unit_is_recognised(u)})
    # Each unit's properties: one, or the set an ambiguous unit fits. Units
    # whose sets differ are different tests (``axis_variants``).
    options = {unit_property_options(u) for u in units}
    mixed = len(options) > 1
    shown = ', '.join(units[:4]) + ('…' if len(units) > 4 else '')
    if mixed:
        hints['property'] = (f'Records arrive in units implying different properties ({shown}): '
                             'one LOINC per axis variant')
    if units:
        only = next(iter(options)) if len(options) == 1 else ()
        if len(only) == 1:
            settle({'property': only[0]}, BASIS_UNIT, f'every record unit implies it ({shown})')
        elif only:
            settle({'property': list(only)}, BASIS_UNIT,
                   f'every record unit is {shown}, which fits only these properties')
        settle({'scale_type': 'Qn'}, BASIS_VALUE_TYPE, f'records carry units ({shown})')
    if unit:
        sample = unit_property_options(unit)
        # The sample settles the property only where the records are silent
        # or agree: records in g/dL and % are two tests, and a g/dL sample
        # must not pick one for them.
        if sample and not mixed and (not units or sample in options):
            settle({'property': _property_value(sample)}, BASIS_UNIT, f'{where} = {unit}')
        if observed:
            settle({'scale_type': 'Qn'}, BASIS_VALUE_TYPE, f'{where} = {unit}')
    quantitative = _quantitative(metadata, resource)
    if quantitative:
        settle({'scale_type': 'Qn'}, BASIS_VALUE_TYPE, quantitative)

    specimen = _text_of(resource.get('specimen'))
    system = SPECIMEN_SYSTEMS.get(_norm(specimen)) if specimen else None
    if system:
        settle({'system': system}, BASIS_SPECIMEN, f'specimen = {specimen}')

    method = _text_of(resource.get('method'))
    if method:
        hints['method_type'] = f'Observation.method = {method}'
    if isinstance(resource.get('effectivePeriod'), dict):
        hints['time_aspect'] = 'effectivePeriod present: an interval, not a point in time'
    if specimen and not system:
        hints['system'] = f'specimen = {specimen}'
    return axes, hints


def resolve(source_vocabulary_id, source_code, metadata, overrides=None, *, observed_units=None):
    """The source's axes after curator overrides: ``(axes, hints)``.

    An override of ``None`` clears an inferred axis, which is how a curator
    lifts a constraint inference got wrong. ``METHODLESS`` on ``method_type``
    says the test has no method, which is itself a constraint.
    """
    axes, hints = infer(source_vocabulary_id, source_code, metadata, observed_units=observed_units)
    for axis, value in (overrides or {}).items():
        if axis not in AXES:
            continue
        if isinstance(value, (list, tuple)):
            # An axis variant's values: the set an ambiguous unit fits.
            value = _property_value(tuple(sorted({str(v).strip() for v in value if str(v).strip()})))
        if value is None or (value == '' and axis != 'method_type') or value == []:
            axes.pop(axis, None)
        else:
            axes[axis] = {'value': value if isinstance(value, list) else str(value).strip(),
                          'basis': BASIS_CURATOR, 'evidence': 'Set by a curator'}
    return axes, hints


def clean_overrides(raw):
    """Validate a curator's axis entries; raises ``ValueError``."""
    if raw in (None, ''):
        return {}
    if not isinstance(raw, dict):
        raise ValueError('loinc_axes must be an object keyed by axis.')
    unknown = set(raw) - set(AXES)
    if unknown:
        raise ValueError(f'Unknown LOINC axes: {", ".join(sorted(unknown))}.')
    cleaned = {}
    for axis, value in raw.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f'{axis} must be text or null.')
        if isinstance(value, str) and len(value) > 255:
            raise ValueError(f'{axis} is longer than 255 characters.')
        cleaned[axis] = value.strip() if isinstance(value, str) else None
    return cleaned


def values(axes):
    """``{axis: value}`` from a resolved axes dict."""
    return {axis: entry['value'] for axis, entry in axes.items()}


# --- comparing ------------------------------------------------------------------

def options_of(value):
    """A settled axis value as the values it accepts: one, or a set's members."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


def compare(axis, source_value, candidate_value):
    """How one candidate axis stands against the source's.

    A source value that is a set (the properties a ``%`` can mean) matches a
    candidate with any member and conflicts with one outside it.
    """
    if source_value is None or candidate_value is None or not options_of(source_value):
        return UNKNOWN
    candidate = _norm(candidate_value)
    verdict = CONFLICT
    for option in options_of(source_value):
        source = _norm(option)
        if source == candidate:
            return MATCH
        if axis == 'system' and source and candidate:
            # "Ser/Plas" is how LOINC names a test valid on either specimen, so
            # serum is compatible with it -- and with each other way round.
            source_parts, candidate_parts = set(source.split('/')), set(candidate.split('/'))
            if source_parts <= candidate_parts or candidate_parts <= source_parts:
                verdict = COMPATIBLE
    return verdict


def assess(source_values, candidate_axes):
    """``{axis: verdict}`` for one candidate."""
    return {
        axis: compare(axis, source_values.get(axis),
                      None if candidate_axes is None else candidate_axes.get(axis))
        for axis in AXES
    }


def apply(candidates, source_axes):
    """Annotate, filter and order candidates by the source's axes.

    Returns ``(eligible, excluded)``. Every candidate gets ``loinc_axes`` (its
    six axes, or ``None`` when it is not a loaded LOINC code) and
    ``axis_match`` (``{axis: verdict}``) plus ``axes_matched`` and
    ``axes_compatible`` counts. A candidate with any ``conflict`` goes to
    ``excluded``. Eligible ones are ordered by matches, then compatible axes,
    then their retrieval order -- the order the ranker and the curator see.
    """
    source_values = values(source_axes)
    codes = [c.get('concept_code') for c in candidates if c.get('vocabulary_id') == 'LOINC']
    known = axes_for_codes(codes)
    eligible, excluded = [], []
    for position, candidate in enumerate(candidates):
        candidate_axes = known.get(candidate.get('concept_code')) \
            if candidate.get('vocabulary_id') == 'LOINC' else None
        verdicts = assess(source_values, candidate_axes)
        annotated = {
            **candidate,
            'loinc_axes': candidate_axes,
            'axis_match': verdicts,
            'axes_matched': sum(v == MATCH for v in verdicts.values()),
            'axes_compatible': sum(v == COMPATIBLE for v in verdicts.values()),
        }
        if CONFLICT in verdicts.values():
            annotated['axis_conflicts'] = [a for a, v in verdicts.items() if v == CONFLICT]
            excluded.append(annotated)
        else:
            eligible.append((position, annotated))
    eligible.sort(key=lambda item: (-item[1]['axes_matched'], -item[1]['axes_compatible'], item[0]))
    return [candidate for _, candidate in eligible], excluded


# --- retrieval ------------------------------------------------------------------

AXIS_RETRIEVAL_LIMIT = 10
#: The ``retrieval`` label on candidates found by their axes.
RETRIEVAL_AXES = 'axes'


def axis_candidates(source_axes, domain_id, limit=AXIS_RETRIEVAL_LIMIT):
    """Standard LOINC concepts whose axes agree with every settled source axis.

    Needs a component: without one the other five axes describe thousands of
    tests. System matches exactly or as a compatible ``A/B`` pair.
    """
    from django.db.models import Q

    from omop_core.models import Concept, LoincCodeClass

    source_values = values(source_axes)
    component = source_values.get('component')
    if not component:
        return []
    rows = LoincCodeClass.objects.filter(component__iexact=component.strip())
    for axis in ('property', 'time_aspect', 'scale_type', 'method_type'):
        if axis in source_values:
            accepted = [str(v).strip() for v in options_of(source_values[axis])]
            if accepted:
                any_of = Q()
                for value in accepted:
                    any_of |= Q(**{f'{axis}__iexact': value})
                rows = rows.filter(any_of)
    codes = list(rows.values_list('loinc_num', 'system')[:500])
    if 'system' in source_values:
        source_system = source_values['system']
        codes = [(code, system) for code, system in codes
                 if compare('system', source_system, system) in (MATCH, COMPATIBLE)]
    if not codes:
        return []
    concepts = Concept.objects.filter(
        vocabulary_id='LOINC', concept_code__in=[code for code, _ in codes],
        standard_concept='S', invalid_reason__isnull=True,
    )
    if domain_id:
        concepts = concepts.filter(Q(domain_id=domain_id))
    return [
        {
            'concept_id': c.concept_id, 'concept_name': c.concept_name,
            'concept_code': c.concept_code, 'vocabulary_id': c.vocabulary_id,
            'concept_class_id': c.concept_class_id, 'domain_id': c.domain_id,
            'standard_concept': c.standard_concept, 'retrieval': RETRIEVAL_AXES,
        }
        for c in concepts.order_by('concept_code')[:limit]
    ]


def _lab_category(metadata, resource):
    """Whether the sender filed the record as a laboratory result."""
    for category in resource.get('category') or []:
        if not isinstance(category, dict):
            continue
        codes = [c.get('code') for c in category.get('coding') or [] if isinstance(c, dict)]
        if 'laboratory' in codes or _norm(category.get('text') or '') == 'laboratory':
            return True
    aggregate = metadata.get('category') if isinstance(metadata, dict) else None
    if isinstance(aggregate, dict):
        return 'laboratory' in _norm(aggregate.get('top') or '')
    return False


def lab_signal(source_vocabulary_id, metadata, axes):
    """Why this source is a lab-style measurement LOINC should name, or None.

    OHDSI's convention is LOINC for lab tests, but SNOMED has standard
    Measurement-domain concepts too ("Glucose measurement, serum"), so without
    a preference one can outrank LOINC 2345-7. Each signal is something the
    source itself says; none is inferred from the candidates.
    """
    if is_loinc(source_vocabulary_id):
        return 'The source code is LOINC'
    resource = _resource(metadata)
    if any(is_loinc(c.get('system')) for c in _codings(resource)):
        return 'The sending system attached a LOINC coding'
    if _lab_category(metadata, resource):
        return 'The sending system filed it as a laboratory result'
    for axis in ('property', 'scale_type'):
        entry = (axes or {}).get(axis)
        if entry and entry['basis'] in (BASIS_UNIT, BASIS_VALUE_TYPE):
            return f"Its {AXIS_LABELS[axis].lower()} is settled ({entry['evidence']})"
    return None


def prefer_loinc(candidates):
    """LOINC candidates first, each group keeping its order (axis rank)."""
    return ([c for c in candidates if c.get('vocabulary_id') == 'LOINC']
            + [c for c in candidates if c.get('vocabulary_id') != 'LOINC'])


def source_summary(axes, hints):
    """The JSON a curator and the ranker see for the source's axes."""
    return {
        'axes': {axis: axes[axis] for axis in AXES if axis in axes},
        'hints': hints,
    }
