"""``/api/v1/phr/`` — the signed-in patient's own record, one section per endpoint.

Only a patient reads here, and only their own record: the caller is resolved
through an existing PatientUser link and nothing is provisioned as a side
effect, so a clinician or service identity gets 404 rather than an empty record.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.models import (
    Concept,
    ConceptAncestor,
    ConditionOccurrence,
    DrugExposure,
    ImageOccurrence,
    Measurement,
    Observation,
    PatientDocument,
    PatientRecord,
    ProcedureOccurrence,
)
from patient_portal.models import PatientStatement, PatientUser
from omop_core.services.condition_context import condition_contexts
from omop_core.services.cancer_details import cancer_details, cancer_family, SLUG_FAMILY
from omop_core.services.patient_record_service import NOT_CANCER_NAME

from .labs import blood_type, lab_history, labs
from .records import genetics, medication_detail, medications, procedures
from .feed import imaging, whats_new
from .outcomes import has_outcomes, outcomes
from .therapy import therapy
from .sources import PATIENT, PATIENT_SELF_REPORT_TYPE_CONCEPT_ID, RECORD, row_sources, source

GENETIC_DOC_TYPES = ('FISH', 'GEP', 'NGS', 'CYTOMETRY', 'CYTOGENETICS', 'MRD', 'BONE_MARROW')


class PhrView(APIView):
    permission_classes = [IsAuthenticated]

    def resolve(self, request):
        """(person, record, None), or (None, None, a 404) when the caller has no record."""
        link = (
            PatientUser.objects.filter(identity=request.user, is_active=True)
            .select_related('person')
            .first()
        )
        if link is None:
            return None, None, Response({'detail': 'No record.'}, status=404)
        return link.person, PatientRecord.objects.filter(person=link.person).first(), None

    def get(self, request):
        person, record, error = self.resolve(request)
        if error:
            return error
        data = self.build(person, record)
        if data is None:
            return Response({'detail': 'Not found.'}, status=404)
        return Response(data)

    def build(self, person, record: PatientRecord | None) -> dict[str, Any] | None:
        raise NotImplementedError


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _edited(record: PatientRecord | None) -> set[str]:
    return set((record.user_edited_fields or []) if record else [])


# ---------------------------------------------------------------- About me

GENDERS = {'F': 'Female', 'M': 'Male', 'O': 'Other', 'U': 'Unknown'}


def about_fields(record: PatientRecord | None, person=None) -> list[dict[str, Any]]:
    """About me's rows; with ``person``, the blood type from their ABO/Rh results."""
    if record is None:
        return []
    edited = _edited(record)

    def field(key, value, *source_fields):
        value = _text(value)
        if value is None:
            return None
        names = source_fields or (key,)
        kind = PATIENT if edited.intersection(names) else RECORD
        return {
            'key': key,
            'value': value,
            'source': source(kind, record.facility_name if kind == RECORD else None),
        }

    gender = _text(record.gender)
    address = ', '.join(
        part for part in (_text(record.city), _text(record.postal_code), _text(record.country)) if part
    )
    fields = [
        field('date_of_birth', record.date_of_birth),
        field('gender', GENDERS.get(gender.upper(), gender) if gender else None),
        field('phone', record.phone_number, 'phone_number'),
        field('email', record.email),
        field('address', address, 'city', 'postal_code', 'country'),
        field('race', record.race),
        field('ethnicity', record.ethnicity),
    ]
    typed = blood_type(person) if person is not None else None
    if typed:
        value, row = typed
        fields.append({
            'key': 'blood_type',
            'value': value,
            'source': row_sources(Measurement, [row], type_attr='measurement_type_concept',
                                  date_attr='measurement_date')[row.pk],
        })
    return [f for f in fields if f]


def patient_name(person) -> str:
    return ' '.join(p for p in (person.given_name, person.family_name) if p and p.strip()).strip()


class AboutView(PhrView):
    def build(self, person, record):
        data = {'name': patient_name(person) or None, 'fields': about_fields(record, person)}
        return {k: v for k, v in data.items() if v is not None}


# ---------------------------------------------------------------- Diagnoses

# ICD-10 neoplasms: C00–C96 malignant, and of the uncertain-behaviour block only
# D45–D47, the blood-cancer precursors and MPN/MDS (D47.2 MGUS, D46 MDS). The
# rest of it, e.g. D48.5 of skin, is not a cancer diagnosis (#119).
_ICD10_CANCER = re.compile(r'^(C\d|D4[5-7])', re.IGNORECASE)
_CANCER_WORDS = re.compile(
    r'neoplas|carcinoma|lymphoma|leuka?emia|myeloma|sarcoma|melanoma|malignan|cancer|'
    r'gammopathy|myelodysplas|myeloproliferative|tumou?r',
    re.IGNORECASE,
)


def is_cancer_condition(row: ConditionOccurrence) -> bool:
    concept = row.condition_concept
    if any(NOT_CANCER_NAME.search(text or '') for text in (_condition_name(row), row.condition_source_value)):
        return False
    codes = [row.condition_source_value or '']
    if concept is not None and (concept.vocabulary_id or '').upper().startswith('ICD10'):
        codes.append(concept.concept_code or '')
    if any(_ICD10_CANCER.match(code.strip()) for code in codes if code):
        return True
    name = concept.concept_name if concept is not None else ''
    return bool(_CANCER_WORDS.search(name or ''))


# Per-disease presentation. Stage systems differ by cancer, so the label goes
# with the disease; fields that don't apply are simply absent.
SOLID_TUMOURS = {'breast-cancer', 'lung-cancer', 'colon-cancer'}
HEMATOLOGIC_CANCERS = {
    'myeloma', 'multiple-myeloma', 'follicular-lymphoma',
    'chronic-lymphocytic-leukemia', 'mantle-cell-lymphoma',
}
STAGE_FIELDS = {
    'myeloma': ('stage', 'Stage & risk group'),
    'multiple-myeloma': ('stage', 'Stage & risk group'),
    'chronic-lymphocytic-leukemia': ('binet_stage', 'Binet stage'),
    'follicular-lymphoma': ('stage', 'Ann Arbor stage'),
}
RISK_FIELDS = {
    'follicular-lymphoma': ('flipi_risk_category', 'FLIPI risk'),
}
BIOMARKER_FIELDS = (
    ('estrogen_receptor_status', 'Estrogen receptor (ER)'),
    ('progesterone_receptor_status', 'Progesterone receptor (PR)'),
    ('her2_status', 'HER2'),
    ('hr_status', 'Hormone receptor (HR)'),
    ('hrd_status', 'HRD'),
    ('androgen_receptor_status', 'Androgen receptor'),
    ('cytogenetic_markers', 'Cytogenetic markers'),
    ('mrd_status', 'MRD'),
)


def _labelled(record, field, label):
    value = _text(getattr(record, field, None))
    return {'label': label, 'value': value} if value else None


def _spread(record) -> str | None:
    slug = (record.disease_slug or '').lower()
    if slug in HEMATOLOGIC_CANCERS:
        # This flat custom field belongs to the primary diagnosis. Do not infer
        # blood-cancer involvement from solid-tumour metastasis flags (D05).
        return _text((record.custom_fields or {}).get('hematologic_spread'))
    if slug not in SOLID_TUMOURS:
        return None
    if _text(record.metastasis_status):
        return _text(record.metastasis_status)
    if record.bone_only_metastasis_status:
        return 'Bone only'
    if record.metastatic_status is True:
        return 'Metastatic'
    if record.metastatic_status is False:
        return 'Not metastatic'
    return None


def primary_cancer(record: PatientRecord, transitions: list[dict]) -> dict[str, Any] | None:
    name = _text(record.disease)
    if name is None:
        return None
    slug = (record.disease_slug or '').lower()
    edited = _edited(record)
    stage_field, stage_label = STAGE_FIELDS.get(slug, ('stage', 'Stage'))
    risk = RISK_FIELDS.get(slug)
    status = _text(record.condition_clinical_status)
    kind = PATIENT if 'disease' in edited else RECORD
    item = {
        'id': 'primary',
        'name': name,
        'subtype': _text(record.histologic_type),
        'stage': _labelled(record, stage_field, stage_label),
        'risk': _labelled(record, *risk) if risk else None,
        'spread': _spread(record),
        'biomarkers': [b for b in (_labelled(record, f, label) for f, label in BIOMARKER_FIELDS) if b],
        'date': record.diagnosis_date.isoformat() if record.diagnosis_date else None,
        'status': status.capitalize() if status else None,
        'transitions': transitions,
        'source': source(kind, record.facility_name if kind == RECORD else None, record.diagnosis_date),
    }
    return {k: v for k, v in item.items() if v not in (None, [], '')}


def _condition_rows(person):
    rows = list(
        ConditionOccurrence.objects.filter(person=person, is_erroneous=False)
        .select_related('condition_concept', 'condition_source_concept', 'visit_occurrence')
        .order_by('condition_start_date', 'condition_occurrence_id')
    )
    _attach_code_names(rows)
    return rows


logger = logging.getLogger(__name__)

# A source value that is a code rather than a name: ICD-10 (Z94.81, or E039
# without its decimal), a SNOMED or other numeric id, or junk like "0" (#131).
_BARE_CODE = re.compile(r'[A-Z]\d{2}(?:\.?[0-9A-Z]{1,4})?|\d+', re.IGNORECASE)
# Where a bare code is looked up, most specific first.
_NAMING_VOCABULARIES = ('ICD10CM', 'ICD10', 'SNOMED')


def _code_candidates(code: str) -> set[str]:
    code = code.strip().upper()
    candidates = {code}
    if '.' not in code and len(code) > 3 and code[0].isalpha():
        candidates.add(f'{code[:3]}.{code[3:]}')  # E039 -> E03.9
    return candidates


def _code_names(codes) -> dict[str, str | None]:
    """The terminology's name for each bare condition code, in one query."""
    wanted = {code: _code_candidates(code) for code in codes}
    candidates = set().union(*wanted.values()) if wanted else set()
    if not candidates:
        return {}
    found: dict[str, dict[str, str]] = {}
    for vocabulary, code, name in Concept.objects.filter(
        vocabulary_id__in=_NAMING_VOCABULARIES, concept_code__in=candidates,
    ).values_list('vocabulary_id', 'concept_code', 'concept_name'):
        found.setdefault(code, {})[vocabulary] = name
    result = {}
    for code, options in wanted.items():
        # Most specific vocabulary first; within one, the code as sent before the dotted form.
        ordered = sorted(options, key=lambda c: c != code.strip().upper())
        result[code] = next(
            (_text(found[c][v]) for v in _NAMING_VOCABULARIES for c in ordered
             if c in found and _text(found[c].get(v))),
            None,
        )
    return result


def _own_name(row) -> str | None:
    """The name the row carries itself: its concept's, then its source concept's."""
    concept = row.condition_concept if row.condition_concept_id else None
    if name := _text(concept.concept_name if concept else None):
        return name
    source = row.condition_source_concept if row.condition_source_concept_id else None
    return _text(source.concept_name if source else None)


def _is_bare_code(row) -> bool:
    """A row the terminology has to name: nothing of its own, and a source text that is a code.
    A patient's own entry is their words, whatever they look like (#2553)."""
    if row.condition_type_concept_id == PATIENT_SELF_REPORT_TYPE_CONCEPT_ID or _own_name(row):
        return False
    text = _text(row.condition_source_value)
    return bool(text and _BARE_CODE.fullmatch(text))


def _attach_code_names(rows) -> None:
    """Name every bare-code row from the terminology with one query, so a section
    costs the same however many unmapped codes the patient has."""
    bare = [row for row in rows if _is_bare_code(row)]
    names = _code_names({_text(row.condition_source_value) for row in bare})
    for row in bare:
        row._phr_code_name = names.get(_text(row.condition_source_value))


def _condition_name(row) -> str | None:
    """A condition's name. Concept 0 is "No matching concept": then it is the
    record's own coding, named from the terminology when it is a bare code,
    and None when nothing names it (the card is left out, #131)."""
    if name := _own_name(row):
        return name
    text = _text(row.condition_source_value)
    if not _is_bare_code(row):
        return text
    if not hasattr(row, '_phr_code_name'):  # a row not loaded by _condition_rows
        _attach_code_names([row])
    return row._phr_code_name


def _unnamed(row, sources) -> None:
    """Log a condition left out for having no name, with where it came from, to
    see how often codes fail to resolve and whether a source clusters (#131).
    The row id and facility only: the code itself is a diagnosis, and the row
    id leads to it (#2545)."""
    if row.condition_source_value:
        origin = sources.get(row.pk) or {}
        logger.warning(
            'phr: unresolved condition code (condition %s, facility %r)', row.pk, origin.get('facility'),
        )


# Cancer families by ICD-10 prefix: conditions in one family are one disease
# over time (MGUS → smoldering → myeloma); different families are different
# cancers and get their own entries.


def _transitions(rows, name_of=None) -> list[dict]:
    """Dated steps when a family holds more than one diagnosis name."""
    name_of = name_of or _condition_name
    steps, seen = [], set()
    for row in rows:
        name = name_of(row)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            steps.append({'name': name, 'date': row.condition_start_date.isoformat()})
    return steps if len(steps) > 1 else []


def _same_disease(families: dict[str, list]) -> tuple[dict[str, list], dict[int, str]]:
    """One entry per disease, however specifically it was coded (#119).

    A cancer whose concept descends from another of the patient's cancers
    (squamous cell carcinoma of foot under squamous cell carcinoma) joins that
    family, and is named for the more general diagnosis. Returns the merged
    families and the general names by condition id.
    """
    concepts = {r.condition_concept_id: r.condition_concept for rows in families.values() for r in rows
                if r.condition_concept_id}
    pairs = ConceptAncestor.objects.filter(
        ancestor_concept_id__in=list(concepts), descendant_concept_id__in=list(concepts),
    ).values_list('ancestor_concept_id', 'descendant_concept_id') if len(concepts) > 1 else []
    above = {}
    for ancestor, descendant in pairs:
        if ancestor != descendant:
            above.setdefault(descendant, set()).add(ancestor)
    # The most general of the patient's own concepts above each one.
    general = {d: min(a for a in ancestors if a not in above) for d, ancestors in above.items()
               if any(a not in above for a in ancestors)}
    if not general:
        return families, {}
    home = {r.condition_concept_id: key for key, rows in families.items() for r in rows}
    merged: dict[str, list] = {}
    names = {}
    for key, rows in families.items():
        for row in rows:
            top = general.get(row.condition_concept_id)
            merged.setdefault(home[top] if top else key, []).append(row)
            if top:
                names[row.pk] = _text(concepts[top].concept_name)
    for rows in merged.values():
        rows.sort(key=lambda r: (r.condition_start_date, r.condition_occurrence_id))
    return merged, names


def _apply_cancer_details(entry, details, sources, family):
    """Overlay only facts explicitly attached to this cancer, including clears."""
    if not details:
        return entry
    detail_sources = {}
    def take(field):
        text, row = details[field]
        origin = sources[type(row), row.pk]
        contributors = getattr(row, '_cancer_detail_contributors', [])
        if len(contributors) > 1:
            origin = {**origin, 'contributors': [sources[type(r), r.pk] for r in contributors]}
        return text, origin
    if 'histologic_type' in details:
        text, origin = take('histologic_type')
        entry.pop('subtype', None)
        if text:
            entry['subtype'] = text
        detail_sources['subtype'] = origin
    stage_field, stage_label = STAGE_FIELDS.get(family, ('stage', 'Stage'))
    if stage_field not in details and 'stage' in details:
        stage_field = 'stage'
    if stage_field in details:
        text, origin = take(stage_field)
        entry.pop('stage', None)
        if text:
            entry['stage'] = {'label': stage_label, 'value': text, 'source': origin}
        detail_sources['stage'] = origin
    if family in RISK_FIELDS and RISK_FIELDS[family][0] in details:
        field, label = RISK_FIELDS[family]
        text, origin = take(field)
        entry.pop('risk', None)
        if text:
            entry['risk'] = {'label': label, 'value': text, 'source': origin}
        detail_sources['risk'] = origin
    biomarkers = list(entry.get('biomarkers', []))
    for field, label in BIOMARKER_FIELDS:
        if field not in details:
            continue
        text, origin = take(field)
        biomarkers = [b for b in biomarkers if b['label'] != label]
        if text:
            biomarkers.append({'label': label, 'value': text, 'source': origin})
        detail_sources[field] = origin
    entry.pop('biomarkers', None)
    if biomarkers:
        entry['biomarkers'] = biomarkers
    if detail_sources:
        entry['detail_sources'] = detail_sources
    return entry


def diagnoses(person, record: PatientRecord | None, *, log_unresolved: bool = False) -> dict[str, Any]:
    """The Diagnoses section. Only the page itself logs the codes it left out
    (``log_unresolved``); the exports and shares that build it again do not, so
    the log counts codes and not page loads (#2545)."""
    rows = _condition_rows(person)
    contexts = condition_contexts(person.pk, [row.pk for row in rows])
    sources = row_sources(
        ConditionOccurrence, rows, type_attr='condition_type_concept', date_attr='condition_start_date'
    )
    families: dict[str, list] = {}
    other_rows = []
    for row in rows:
        if is_cancer_condition(row):
            families.setdefault(cancer_family(row), []).append(row)
        else:
            other_rows.append(row)
    families, general_names = _same_disease(families)

    def name_of(row):
        return general_names.get(row.pk) or _condition_name(row)

    details = cancer_details(person.pk, {row.pk: family for family, items in families.items() for row in items})
    detail_sources = {}
    for model in (Measurement, Observation):
        selected = {r.pk: r for fields in details.values() for text, row in fields.values()
                    for r in [row] + getattr(row, '_cancer_detail_contributors', []) if type(r) is model}
        table = model._meta.db_table
        detail_sources.update({(model, pk): origin for pk, origin in row_sources(model, list(selected.values()),
            type_attr=f'{table}_type_concept', date_attr=f'{table}_date').items()})
    cancer = []
    slug = (record.disease_slug or '').lower() if record else ''
    primary_family = SLUG_FAMILY.get(slug, slug)
    primary_rows = families.pop(primary_family, [])
    primary = primary_cancer(record, _transitions(primary_rows, name_of)) if record else None
    if primary:
        _apply_cancer_details(primary, details.get(primary_family, {}), detail_sources, primary_family)
        if primary_rows:
            primary['condition_id'] = primary_rows[-1].pk
            primary.update(contexts.get(primary_rows[-1].pk, {}))
        if 'status' not in primary and primary_rows and all(r.condition_end_date for r in primary_rows):
            primary['status'] = 'Resolved'
        cancer.append(primary)
    for family, family_rows in families.items():
        latest = family_rows[-1]
        name = name_of(latest)
        if not name:
            if log_unresolved:
                _unnamed(latest, sources)
            continue
        resolved = all(r.condition_end_date for r in family_rows)
        # Diagnosed when the current diagnosis began (myeloma, not the MGUS
        # before it); the earlier steps are in the transitions.
        current_start = next(r for r in family_rows if name_of(r) == name).condition_start_date
        entry = {
            'id': f'condition-{latest.pk}',
            **contexts.get(latest.pk, {}),
            'name': name,
            'date': current_start.isoformat(),
            'status': 'Resolved' if resolved else _text(latest.condition_status_source_value),
            'transitions': _transitions(family_rows, name_of),
            'source': sources[latest.pk],
        }
        _apply_cancer_details(entry, details.get(family, {}), detail_sources, family)
        cancer.append({k: v for k, v in entry.items() if v not in (None, [])})
    # The primary cancer (the one the record follows) first, then the others newest first.
    cancer.sort(key=lambda c: c.get('date') or '', reverse=True)
    cancer.sort(key=lambda c: c['id'] != 'primary')

    # Patient-added conditions may have no known date (stored as the day added).
    undated = set(
        PatientStatement.objects.filter(
            person=person, subject=PatientStatement.SUBJECT_CONDITION, details__date_unknown=True,
        ).values_list('subject_key', flat=True)
    )

    # Other conditions: one entry per condition, dated from its first record.
    other, seen = [], set()
    for row in other_rows:
        name = _condition_name(row)
        if not name:
            if log_unresolved:
                _unnamed(row, sources)
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        dated = str(row.pk) not in undated
        entry = {
            'id': f'condition-{row.pk}',
            **contexts.get(row.pk, {}),
            'name': name,
            'date': row.condition_start_date.isoformat() if dated else None,
            'status': 'Resolved' if row.condition_end_date else _text(row.condition_status_source_value),
            'source': sources[row.pk] if dated else {**sources[row.pk], 'date': None},
        }
        other.append({k: v for k, v in entry.items() if v is not None})
    other.sort(key=lambda e: e.get('date') or '9999', reverse=True)

    return {'cancer': cancer, 'other': other}


class DiagnosesView(PhrView):
    def build(self, person, record):
        return diagnoses(person, record, log_unresolved=True)


# ---------------------------------------------------------------- Status

SECTIONS = (
    'whats_new', 'about', 'diagnoses', 'therapy', 'outcomes',
    'labs', 'medications', 'procedures', 'genetics', 'imaging',
)


def _has_lines(person) -> bool:
    from omop_core.services.disease_episodes import regimen_episodes

    return regimen_episodes(person).filter(episode_number__isnull=False).exists()


def section_status(person, record: PatientRecord | None) -> dict[str, str]:
    """``ready`` or ``empty`` per section.

    ``processing`` is part of the contract, for a section whose records are
    still being imported, but PRomop has no per-patient import state yet: the
    importers need to report it before this can tell "still processing" from
    "nothing found".
    """
    def has(qs):
        return qs.filter(person=person).exists()

    docs = PatientDocument.objects.filter(person=person)
    present = {
        'about': bool(about_fields(record, person)),
        'diagnoses': bool(record and _text(record.disease))
        or has(ConditionOccurrence.objects.filter(is_erroneous=False)),
        # Any cancer's lines (#1739), not only the primary one's.
        'therapy': bool(record and (_text(record.first_line_therapy) or record.later_therapies))
        or _has_lines(person),
        'outcomes': has_outcomes(),
        'labs': has(Measurement.objects.filter(is_erroneous=False)),
        'medications': has(DrugExposure.objects.filter(is_erroneous=False)),
        'procedures': has(ProcedureOccurrence.objects.filter(is_erroneous=False, image_occurrences__isnull=True)),
        'genetics': docs.filter(doc_type__in=GENETIC_DOC_TYPES).exists()
        or bool(record and (_text(record.cytogenetic_markers) or record.genetic_mutations)),
        'imaging': docs.filter(doc_type='IMAGING').exists()
        or has(ImageOccurrence.objects.filter(procedure_occurrence__is_erroneous=False)),
    }
    present['whats_new'] = any(present.values())
    return {name: 'ready' if present[name] else 'empty' for name in SECTIONS}


class StatusView(PhrView):
    def build(self, person, record):
        return {'sections': section_status(person, record)}


# ---------------------------------------------------------------- Labs


class LabsView(PhrView):
    def build(self, person, record):
        return labs(person, record)


class LabHistoryView(PhrView):
    def get(self, request, test_id: int):
        self.test_id = test_id
        return super().get(request)

    def build(self, person, record):
        return lab_history(person, self.test_id, record)


# ---------------------------------------------------------------- Medications, procedures, genetics


class MedicationsView(PhrView):
    def build(self, person, record):
        return medications(person, record)


class MedicationDetailView(PhrView):
    def get(self, request, medication_id: str):
        self.medication_id = medication_id
        return super().get(request)

    def build(self, person, record):
        return medication_detail(person, self.medication_id)


class ProceduresView(PhrView):
    def build(self, person, record):
        return procedures(person, record)


class GeneticsView(PhrView):
    def build(self, person, record):
        return genetics(person, record)


class TherapyView(PhrView):
    def build(self, person, record):
        return therapy(person, record)


class WhatsNewView(PhrView):
    def build(self, person, record):
        return whats_new(person, record)


class ImagingView(PhrView):
    def build(self, person, record):
        return imaging(person, record)


class OutcomesView(PhrView):
    def build(self, person, record):
        return outcomes(person, record)
