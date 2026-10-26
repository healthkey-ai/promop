"""How much LOINC axis evidence the loaded data holds, counted by the walk (#2051).

The axis variant walk groups a code's units into property/scale variants.
Whether the other four axes ever carry evidence on real data decides whether
the multi-axis capability is worth extending, so a rebuild (#2043) counts, over
the codes it walks, how many have each axis settled and from what, how many
only have a hint for it, and how many carry two or more values of an axis
across their patients. The summary is stored on the completed request and
shown on the Concept to Code tab.

Per code the sources are the ones ``loinc_axes.infer`` reads: a curator's
entry, the source code being LOINC, a LOINC coding in a patient's metadata,
the record units, a quantitative value, and the specimen. A code is counted
once per axis whatever the number of its patients.
"""
from collections import Counter, defaultdict

from omop_core.mapping import loinc_axes

AXES = loinc_axes.AXES
#: Where a settled axis came from, in the order the summary reports them.
BASES = (
    loinc_axes.BASIS_CURATOR, loinc_axes.BASIS_SOURCE_CODE, loinc_axes.BASIS_FHIR_CODING,
    loinc_axes.BASIS_UNIT, loinc_axes.BASIS_VALUE_TYPE, loinc_axes.BASIS_SPECIMEN,
)
#: Per-patient metadata rows read for one code, as ``axis_variants`` reads
#: them for the specimen note.
ROWS_PER_CODE = 2000


def code_evidence(mapping, units, resources, variant_count=0):
    """``{axis: {'bases': set, 'hint': bool, 'varies': bool}}`` for one code.

    ``units`` are the record units the rebuild found, ``resources`` the FHIR
    resources (as stored) of the code's per-patient rows, ``variant_count``
    how many property/scale variants the units make. The mapping's own
    metadata sample, or the first patient's, is what inference reads; the
    rest of the patients' rows are scanned for what they add: a LOINC
    coding, a quantitative value, a specimen, a method, a period. ``varies``
    on an axis means the patients disagree on it: units implying different
    properties, LOINC codings naming different tests, or specimens naming
    different systems. At most one query, for the codings' axes.
    """
    evidence = {axis: {'bases': set(), 'hint': False, 'varies': False} for axis in AXES}
    sample = mapping.source_metadata or (resources[0] if resources else {})
    axes, hints = loinc_axes.infer(mapping.source_vocabulary_id, mapping.source_code, sample,
                                   observed_units=units)
    for axis, settled in axes.items():
        evidence[axis]['bases'].add(settled['basis'])
    for axis in hints:
        evidence[axis]['hint'] = True

    codings, systems, methods = set(), set(), set()
    quantitative = False
    for metadata in resources:
        resource = loinc_axes._resource(metadata)
        codings.update(c.get('code') for c in loinc_axes._codings(resource)
                       if loinc_axes.is_loinc(c.get('system')) and c.get('code'))
        specimen = loinc_axes._text_of(resource.get('specimen'))
        system = loinc_axes.SPECIMEN_SYSTEMS.get(loinc_axes._norm(specimen)) if specimen else None
        if system:
            systems.add(system)
        elif specimen:
            evidence['system']['hint'] = True
        method = loinc_axes._text_of(resource.get('method'))
        if method:
            methods.add(loinc_axes._norm(method))
            evidence['method_type']['hint'] = True
        if isinstance(resource.get('effectivePeriod'), dict):
            evidence['time_aspect']['hint'] = True
        quantitative = quantitative or bool(loinc_axes._quantitative(metadata, resource))
    if codings:
        for found in loinc_axes.axes_for_codes(sorted(codings)).values():
            for axis in found:
                evidence[axis]['bases'].add(loinc_axes.BASIS_FHIR_CODING)
    if quantitative:
        evidence['scale_type']['bases'].add(loinc_axes.BASIS_VALUE_TYPE)
    if systems:
        evidence['system']['bases'].add(loinc_axes.BASIS_SPECIMEN)
    for axis, value in (mapping.loinc_axes or {}).items():
        if axis in evidence and value is not None and (value != '' or axis == 'method_type'):
            evidence[axis]['bases'].add(loinc_axes.BASIS_CURATOR)
    evidence['property']['varies'] = variant_count >= 2
    evidence['component']['varies'] = len(codings) >= 2
    evidence['system']['varies'] = len(systems) >= 2
    evidence['method_type']['varies'] = len(methods) >= 2
    return evidence


class Summary:
    """Counts of codes per axis, accumulated across a walk's batches.

    Serialised as ``{'codes': n, 'axes': {axis: {'settled': n, 'hint': n,
    'varies': n, 'bases': {basis: n}}}}``; ``settled`` counts a code once
    however many bases settle the axis. A resumed walk continues from the
    stored dict, so the counts cover every code walked by every attempt.
    """

    def __init__(self, data=None):
        data = data or {}
        self.codes = int(data.get('codes') or 0)
        self.axes = {}
        for axis in AXES:
            stored = (data.get('axes') or {}).get(axis) or {}
            self.axes[axis] = {
                'settled': int(stored.get('settled') or 0),
                'hint': int(stored.get('hint') or 0),
                'varies': int(stored.get('varies') or 0),
                'bases': Counter({b: int(n) for b, n in (stored.get('bases') or {}).items() if b in BASES}),
            }

    def add(self, evidence):
        self.codes += 1
        for axis, found in evidence.items():
            counts = self.axes[axis]
            if found['bases']:
                counts['settled'] += 1
                for basis in found['bases']:
                    counts['bases'][basis] += 1
            if found['hint']:
                counts['hint'] += 1
            if found['varies']:
                counts['varies'] += 1

    def as_dict(self):
        return {
            'codes': self.codes,
            'axes': {axis: {
                'settled': counts['settled'], 'hint': counts['hint'], 'varies': counts['varies'],
                'bases': {basis: counts['bases'][basis] for basis in BASES if counts['bases'][basis]},
            } for axis, counts in self.axes.items()},
        }


def resources_by_code(mappings):
    """The stored FHIR resources of each mapping's per-patient rows, by mapping pk.

    One query for the batch; at most :data:`ROWS_PER_CODE` rows per code.
    """
    from django.db.models import Q

    from omop_core.models import PatientSourceCode

    by_key = defaultdict(list)
    for mapping in mappings:
        by_key[(mapping.source_vocabulary_id or '', mapping.source_code)].append(mapping.pk)
    if not by_key:
        return {}
    match = Q()
    for vocab, code in by_key:
        match |= Q(source_vocabulary_id=vocab, source_value=code)
    rows = PatientSourceCode.objects.filter(match).exclude(source_metadata={}).values_list(
        'source_vocabulary_id', 'source_value', 'source_metadata')
    found = defaultdict(list)
    for vocab, code, metadata in rows.iterator(chunk_size=2000):
        key = (vocab or '', code)
        if len(found[key]) < ROWS_PER_CODE:
            found[key].append(metadata)
    return {pk: found.get(key, []) for key, pks in by_key.items() for pk in pks}
