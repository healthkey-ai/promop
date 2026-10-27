"""Cancer-specific projections of existing OMOP facts, linked by CDM event fields.

No values are copied between diagnoses. Unlinked historical facts remain a
legacy primary-record fallback; a typed condition link is authoritative.
"""
import re
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, time

from django.db.models import Q
from django.utils import timezone

from omop_core.models import Concept, FieldConceptMapping, Measurement, Observation

CONDITION_FIELD = 'condition_occurrence.condition_occurrence_id'
EVENT_FIELDS = {Measurement: ('measurement_event_id', 'meas_event_field_concept_id'),
                Observation: ('observation_event_id', 'obs_event_field_concept_id')}
CANCER_FAMILIES = {
    'myeloma': ('C90', 'D47.2'), 'prostate-cancer': ('C61',),
    'breast-cancer': ('C50', 'D05'), 'lung-cancer': ('C34',),
    'colon-cancer': ('C18', 'C19', 'C20'), 'follicular-lymphoma': ('C82',),
    'chronic-lymphocytic-leukemia': ('C91.1',), 'mantle-cell-lymphoma': ('C83.1',),
}
SLUG_FAMILY = {'multiple-myeloma': 'myeloma'}
# Existing field mappings remain authoritative. These are existing extractor
# identities, not newly invented concepts or diagnoses inferred from a date.
CODES = {
    'stage': [('LOINC', '21908-9'), ('LOINC', '21908-9-riss')],
    'histologic_type': [('LOINC', '59847-4')],
    'binet_stage': [('SNOMED', '1149099005')],
    'flipi_risk_category': [],
    'estrogen_receptor_status': [('LOINC', '16112-5')],
    'progesterone_receptor_status': [('LOINC', '16113-3')],
    'her2_status': [('LOINC', '48676-1')],
    'androgen_receptor_status': [('LOINC', '49457-5')],
    'hr_status': [('SNOMED', '310871000000100')],
    'hrd_status': [('LOINC', '107286-7')],
    'cytogenetic_markers': [('SNOMED', '107675007')],
    'mrd_status': [('LOINC', '98847-0')],
}
SOURCES = {'FHIR-condition-stage': 'stage', 'sample-patient-stage': 'stage',
           'mm-cytogenetic-markers': 'cytogenetic_markers'}


def cancer_family(row):
    concept = row.condition_concept
    codes = [(row.condition_source_value or '').strip().upper()]
    if concept and (concept.vocabulary_id or '').upper().startswith('ICD10'):
        codes.append((concept.concept_code or '').upper())
    for family, prefixes in CANCER_FAMILIES.items():
        if any(code.startswith(prefix) for code in codes for prefix in prefixes):
            return family
    name = concept.concept_name if row.condition_concept_id else row.condition_source_value
    from omop_core.services.disease_episodes import disease_slug
    slug = disease_slug(name)
    slug = SLUG_FAMILY.get(slug, slug)
    from omop_core.services.patient_record_service import _CANONICAL_DISEASES, _canonicalize_disease
    if slug in CANCER_FAMILIES or _canonicalize_disease(name or '').lower() in _CANONICAL_DISEASES:
        return slug
    code = next((c for c in codes if re.match(r'^(C\d|D3[7-9]|D4[0-8])', c)), None)
    if code:
        return code[:3]
    return slug or (name or '').strip().lower()


def condition_field_concepts():
    return Concept.objects.filter(vocabulary_id='CDM', domain_id='Metadata', invalid_reason__isnull=True).filter(
        Q(concept_code=CONDITION_FIELD) | Q(concept_name=CONDITION_FIELD)).order_by('pk')


MEASUREMENT_FIELD = 'measurement.measurement_id'
_UNLINKED = object()
_INVALID = object()


def event_namespaces():
    rows = Concept.objects.filter(vocabulary_id='CDM', domain_id='Metadata', invalid_reason__isnull=True).filter(
        Q(concept_code__in=(CONDITION_FIELD, MEASUREMENT_FIELD)) | Q(concept_name__in=(CONDITION_FIELD, MEASUREMENT_FIELD)))
    return ({r.pk for r in rows if r.concept_code == CONDITION_FIELD or r.concept_name == CONDITION_FIELD},
            {r.pk for r in rows if r.concept_code == MEASUREMENT_FIELD or r.concept_name == MEASUREMENT_FIELD})


def fact_owners(rows, groups, condition_concepts, measurement_concepts):
    """Resolve typed report ancestry in memory; missing parents/cycles are invalid.

    Ownership is never followed into another person's rows: callers load only
    this person's facts. An unlinked legacy report remains primary-only fallback.
    """
    measurements = {row.pk: row for row in rows if isinstance(row, Measurement)}
    owners, pending = {}, {}
    for pk, row in measurements.items():
        if row.meas_event_field_concept_id in condition_concepts:
            owners[pk] = groups.get(row.measurement_event_id, _INVALID)
        elif row.meas_event_field_concept_id in measurement_concepts:
            pending[pk] = row.measurement_event_id
        else:
            owners[pk] = _UNLINKED
    while pending:
        resolved = {pk: owners[parent] for pk, parent in pending.items() if parent in owners}
        if not resolved:
            break
        owners.update(resolved)
        pending = {pk: parent for pk, parent in pending.items() if pk not in resolved}
    owners.update({pk: _INVALID for pk in pending})
    result = {}
    for row in rows:
        event, kind = EVENT_FIELDS[type(row)]
        if isinstance(row, Measurement):
            owner = owners[row.pk]
        elif getattr(row, kind) in condition_concepts:
            owner = groups.get(getattr(row, event), _INVALID)
        elif getattr(row, kind) in measurement_concepts:
            owner = owners.get(getattr(row, event), _INVALID)
        else:
            owner = _UNLINKED
        result[type(row), row.pk] = owner
    return result


class DetailFields:
    def __init__(self):
        self.codes = {pair: field for field, pairs in CODES.items() for pair in pairs}
        self.ids, self.identities, self.sources = defaultdict(set), {}, dict(SOURCES)
        for mapping in FieldConceptMapping.objects.filter(
                status='approved', field_name__in=CODES).select_related('concept'):
            if mapping.concept_id:
                self.ids[mapping.concept_id].add(mapping.field_name)
                self.identities[mapping.concept_id, mapping.source_value or mapping.concept_code] = mapping.field_name
                self.codes[mapping.vocabulary_id, mapping.concept_code] = mapping.field_name
            source = mapping.source_value or mapping.concept_code
            if source:
                self.sources[source] = mapping.field_name

    def field(self, row):
        table = row._meta.db_table
        concept = getattr(row, f'{table}_concept')
        source = getattr(row, f'{table}_source_value') or ''
        if concept.pk:
            exact = self.identities.get((concept.pk, source))
            if exact:
                return exact
            candidates = self.ids.get(concept.pk, set())
            if len(candidates) > 1:
                return None  # Shared concepts require the curator's distinct source key.
            field = next(iter(candidates), None) or self.codes.get((concept.vocabulary_id, concept.concept_code))
            if field:
                return field
            # MRD assay results differ by method and specimen. Preserve the
            # actual source concept; never recode every assay as one panel.
            if concept.vocabulary_id in ('LOINC', 'SNOMED') and any(
                    term in concept.concept_name.lower() for term in ('minimal residual disease', 'measurable residual disease')):
                return 'mrd_status'
        source = getattr(row, f'{table}_source_value') or ''
        if source in SOURCES or source.startswith('cytogenetic:'):
            return self.sources.get(source, 'cytogenetic_markers')
        if not concept.pk:
            return self.sources.get(source) or next((f for (v, c), f in self.codes.items() if c == source), None)
        return None


def value(row):
    from omop_core.services.omop_projection import CLEAR_VALUE
    if row.value_source_value == CLEAR_VALUE:
        return ''
    if row.value_as_concept_id:
        return row.value_as_concept.concept_name
    if row.value_as_string is not None:
        text = row.value_as_string.strip()
        return text or row.value_source_value or None
    if row.value_as_number is not None:
        return format(row.value_as_number, 'f').rstrip('0').rstrip('.') if row.value_as_number % 1 else str(int(row.value_as_number))
    return None


def recency(row):
    table = row._meta.db_table
    on = getattr(row, f'{table}_date')
    at = getattr(row, f'{table}_datetime') or timezone.make_aware(datetime.combine(on, time.min))
    return on, at, table, row.pk


def select_details(rows, groups, event_concepts, fields, measurement_concepts=()):
    """group -> field -> (value, source row); query-free and patient-scoped by caller."""
    selected = defaultdict(dict)
    marker_rows = defaultdict(list)
    owners = fact_owners(rows, groups, event_concepts, measurement_concepts)
    for row in rows:
        group = owners[type(row), row.pk]
        if group is _UNLINKED or group is _INVALID:
            continue
        field = fields.field(row)
        text = value(row)
        if not field or text is None:
            continue
        if field == 'cytogenetic_markers':
            marker_rows[group].append(row)
            continue
        previous = selected[group].get(field)
        if previous is None or recency(row) > recency(previous[1]):
            selected[group][field] = text, row
    from omop_core.services.cytogenetics import SOURCE_PREFIX, VALUES, selections, read_cytogenetic_summary
    from omop_core.services.omop_projection import CLEAR_VALUE
    from omop_core.models import Note
    from omop_core.services.clinical_text import note_id
    marker_facts = [row for facts in marker_rows.values() for row in facts]
    note_ids = {note_id(row.value_as_string) for row in marker_facts} - {None}
    notes = {note.pk: note for note in Note.objects.filter(pk__in=note_ids,
        person_id__in={row.person_id for row in marker_facts})} if note_ids else {}
    for row in marker_facts:
        note = notes.get(note_id(row.value_as_string))
        row._cytogenetic_summary_text = (note.note_text if note and note.person_id == row.person_id
            and note.note_source_value == f'cytogenetics:{row._meta.db_table}:{row.pk}' else row.value_as_string)
    for group, facts in marker_rows.items():
        ordered = sorted(facts, key=recency, reverse=True)
        current, origins = {}, {}
        for row in ordered:
            source = getattr(row, f'{row._meta.db_table}_source_value') or ''
            if source.startswith(SOURCE_PREFIX):
                marker = source[len(SOURCE_PREFIX):]
                current.setdefault(marker, row.value_source_value != CLEAR_VALUE and bool(value(row)))
                origins.setdefault(marker, row)
            else:
                summary = [] if row.value_source_value == CLEAR_VALUE else selections(read_cytogenetic_summary(row) or value(row))
                for marker in VALUES:
                    current.setdefault(marker, marker in summary)
                    origins.setdefault(marker, row)
                for marker in summary:
                    current.setdefault(marker, True)
                    origins.setdefault(marker, row)
                break
        markers = [m for m in VALUES if current.get(m)] + sorted(m for m, present in current.items() if present and m not in VALUES)
        contributors = {(type(row), row.pk): row for row in [ordered[0]] + [origins[m] for m in markers]}
        ordered[0]._cancer_detail_contributors = list(contributors.values())
        selected[group]['cytogenetic_markers'] = ', '.join(markers), ordered[0]
    return dict(selected)


def cancer_details(person_id, groups):
    """Read all requested cancers in batches, with source metadata per value."""
    if not groups:
        return {}
    event_concepts, measurement_concepts = event_namespaces()
    rows = []
    for model, (event, kind) in EVENT_FIELDS.items():
        table = model._meta.db_table
        rows.extend(model.objects.filter(person_id=person_id, is_erroneous=False,
            **{f'{kind}__in': event_concepts | measurement_concepts}).select_related(
                f'{table}_concept', 'value_as_concept', 'visit_occurrence'))
    return select_details(rows, groups, event_concepts, DetailFields(), measurement_concepts)


def primary_snapshot(person, snapshot):
    """Keep a secondary cancer's linked disease facts out of the flat primary.

    CBCs and other shared clinical facts remain patient-wide. Only recognized
    diagnosis detail fields are scoped. Unknown/foreign/dangling condition links
    cannot become a primary-cancer fallback.
    """
    raw = snapshot.cancer_detail_rows or (snapshot.measurements + snapshot.observations)
    if not any(getattr(r, EVENT_FIELDS[type(r)][0]) is not None for r in raw):
        return snapshot, {}
    event_concepts, measurement_concepts = event_namespaces()
    linked = [r for r in raw if getattr(r, EVENT_FIELDS[type(r)][1]) in event_concepts | measurement_concepts]
    if not linked:
        return snapshot, {}
    if not any(getattr(r, EVENT_FIELDS[type(r)][1]) in event_concepts for r in linked):
        legacy_owners = fact_owners(raw, {}, event_concepts, measurement_concepts)
        if all(owner is _UNLINKED for owner in legacy_owners.values()):
            return snapshot, {}
    from omop_core.services.patient_record_service import _is_oncologic, _primary_cancer_condition
    cancers = [c for c in snapshot.conditions if not c.is_erroneous and _is_oncologic(c)]
    primary = _primary_cancer_condition(person, snapshot, cancers) if cancers else None
    own = {c.pk for c in cancers if primary and cancer_family(c) == cancer_family(primary)}
    fields = DetailFields()
    groups = {pk: 'primary' for pk in own}
    owners = fact_owners(raw, groups, event_concepts, measurement_concepts)
    selected = select_details(raw, groups, event_concepts, fields, measurement_concepts).get('primary', {})

    def keep(row):
        owner = owners[type(row), row.pk]
        return owner is _UNLINKED or owner == 'primary' or not fields.field(row)

    measurements = [r for r in snapshot.measurements if keep(r)]
    observations = [r for r in snapshot.observations if keep(r)]
    indexes = {}
    for table, rows in (('meas', measurements), ('obs', observations)):
        model = 'measurement' if table == 'meas' else 'observation'
        by_code, by_source = defaultdict(list), defaultdict(list)
        for row in rows:
            by_code[getattr(row, f'{model}_concept').concept_code].append(row)
            by_source[getattr(row, f'{model}_source_value')].append(row)
        indexes[f'{table}_by_code'], indexes[f'{table}_by_source'] = dict(by_code), dict(by_source)
    scoped = replace(snapshot, measurements=measurements, observations=observations,
        cytogenetic_observations=[r for r in snapshot.cytogenetic_observations if keep(r)],
        breast_measurements=[r for r in snapshot.breast_measurements if keep(r)] if snapshot.breast_measurements is not None else None,
        genomics_cache={}, **indexes)
    return scoped, {field: text or None for field, (text, row) in selected.items()}


def projection_scope(person, field, model):
    """A flat PatientRecord edit belongs to its current primary diagnosis.

    No source-only or secondary-cancer fact may be overwritten by the existing
    editor. Without an identifiable cancer the old unlinked write is retained.
    """
    if field not in CODES or model not in EVENT_FIELDS:
        return {}
    from omop_core.models import ConditionOccurrence, PatientRecord
    from omop_core.services.disease_episodes import disease_slug
    record = PatientRecord.objects.filter(person=person).only('disease_slug', 'disease').first()
    slug = (record.disease_slug or disease_slug(record.disease)) if record else ''
    key = SLUG_FAMILY.get(slug, slug)
    conditions = ConditionOccurrence.objects.filter(person=person, is_erroneous=False).select_related(
        'condition_concept').order_by('-condition_start_date', '-condition_occurrence_id')
    parent = None
    for row in conditions:
        name = row.condition_concept.concept_name if row.condition_concept_id else row.condition_source_value
        canonical = disease_slug(name)
        if key and (cancer_family(row) == key or SLUG_FAMILY.get(canonical, canonical) == key):
            parent = row
            break
    event, kind = EVENT_FIELDS[model]
    if parent is None:
        return {event: None, kind: None}
    field_concept = condition_field_concepts().first()
    if field_concept is None:
        raise ValueError('CDM condition field vocabulary must be loaded before projecting cancer details.')
    return {event: parent.pk, kind: field_concept.pk}
