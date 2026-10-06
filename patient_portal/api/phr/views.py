"""``/api/v1/phr/`` — the signed-in patient's own record, one section per endpoint.

Only a patient reads here, and only their own record: the caller is resolved
through an existing PatientUser link and nothing is provisioned as a side
effect, so a clinician or service identity gets 404 rather than an empty record.
"""
from __future__ import annotations

import re
from typing import Any

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.models import (
    ConditionOccurrence,
    DrugExposure,
    Measurement,
    PatientDocument,
    PatientRecord,
    ProcedureOccurrence,
)
from patient_portal.models import PatientUser

from .labs import lab_history, labs
from .sources import PATIENT, RECORD, row_sources, source

GENETIC_DOC_TYPES = ('FISH', 'GEP', 'NGS', 'CYTOMETRY', 'CYTOGENETICS', 'MRD', 'BONE_MARROW')


class PhrView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        link = (
            PatientUser.objects.filter(identity=request.user, is_active=True)
            .select_related('person')
            .first()
        )
        if link is None:
            return Response({'detail': 'No record.'}, status=404)
        person = link.person
        record = PatientRecord.objects.filter(person=person).first()
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


def about_fields(record: PatientRecord | None) -> list[dict[str, Any]]:
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
    return [f for f in fields if f]


class AboutView(PhrView):
    def build(self, person, record):
        return {'fields': about_fields(record)}


# ---------------------------------------------------------------- Diagnoses

# ICD-10 neoplasms: C00–C96 malignant, D37–D48 uncertain behaviour (which holds
# the blood-cancer precursors and MPN/MDS, e.g. D47.2 MGUS, D46 MDS).
_ICD10_CANCER = re.compile(r'^(C\d|D3[7-9]|D4[0-8])', re.IGNORECASE)
_CANCER_WORDS = re.compile(
    r'neoplas|carcinoma|lymphoma|leuka?emia|myeloma|sarcoma|melanoma|malignan|cancer|'
    r'gammopathy|myelodysplas|myeloproliferative|tumou?r',
    re.IGNORECASE,
)


def is_cancer_condition(row: ConditionOccurrence) -> bool:
    concept = row.condition_concept
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
STAGE_FIELDS = {
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
        'spread': _spread(record) if slug in SOLID_TUMOURS else None,
        'biomarkers': [b for b in (_labelled(record, f, label) for f, label in BIOMARKER_FIELDS) if b],
        'date': record.diagnosis_date.isoformat() if record.diagnosis_date else None,
        'status': status.capitalize() if status else None,
        'transitions': transitions,
        'source': source(kind, record.facility_name if kind == RECORD else None, record.diagnosis_date),
    }
    return {k: v for k, v in item.items() if v not in (None, [], '')}


def _condition_rows(person):
    return list(
        ConditionOccurrence.objects.filter(person=person, is_erroneous=False)
        .select_related('condition_concept', 'visit_occurrence')
        .order_by('condition_start_date', 'condition_occurrence_id')
    )


def _condition_name(row) -> str | None:
    concept = row.condition_concept
    return _text(concept.concept_name if concept else None) or _text(row.condition_source_value)


def diagnoses(person, record: PatientRecord | None) -> dict[str, Any]:
    rows = _condition_rows(person)
    sources = row_sources(
        ConditionOccurrence, rows, type_attr='condition_type_concept', date_attr='condition_start_date'
    )
    cancer_rows = [r for r in rows if is_cancer_condition(r)]
    other_rows = [r for r in rows if not is_cancer_condition(r)]

    # Dated disease transitions, e.g. MGUS → smoldering myeloma → myeloma.
    transitions, seen = [], set()
    for row in cancer_rows:
        name = _condition_name(row)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            transitions.append({'name': name, 'date': row.condition_start_date.isoformat()})
    if len(transitions) < 2:
        transitions = []

    cancer = []
    primary = primary_cancer(record, transitions) if record else None
    if primary:
        cancer.append(primary)
    else:
        # No derived primary yet: show each cancer condition as recorded.
        for row in reversed(cancer_rows):
            name = _condition_name(row)
            if name and not any(c['name'].lower() == name.lower() for c in cancer):
                cancer.append({
                    'id': f'condition-{row.pk}',
                    'name': name,
                    'date': row.condition_start_date.isoformat(),
                    'source': sources[row.pk],
                })

    # Other conditions: one entry per condition, dated from its first record.
    other, by_name = [], {}
    for row in other_rows:
        name = _condition_name(row)
        if not name or name.lower() in by_name:
            continue
        entry = {
            'id': f'condition-{row.pk}',
            'name': name,
            'date': row.condition_start_date.isoformat(),
            'status': 'Resolved' if row.condition_end_date else _text(row.condition_status_source_value),
            'source': sources[row.pk],
        }
        by_name[name.lower()] = entry
        other.append({k: v for k, v in entry.items() if v is not None})
    other.sort(key=lambda e: e['date'], reverse=True)

    return {'cancer': cancer, 'other': other}


class DiagnosesView(PhrView):
    def build(self, person, record):
        return diagnoses(person, record)


# ---------------------------------------------------------------- Status

SECTIONS = (
    'whats_new', 'about', 'diagnoses', 'therapy', 'outcomes',
    'labs', 'medications', 'procedures', 'genetics', 'imaging',
)


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
        'about': bool(about_fields(record)),
        'diagnoses': bool(record and _text(record.disease))
        or has(ConditionOccurrence.objects.filter(is_erroneous=False)),
        'therapy': bool(record and (_text(record.first_line_therapy) or record.later_therapies)),
        'outcomes': False,
        'labs': has(Measurement.objects.filter(is_erroneous=False)),
        'medications': has(DrugExposure.objects.filter(is_erroneous=False)),
        'procedures': has(ProcedureOccurrence.objects.filter(is_erroneous=False)),
        'genetics': docs.filter(doc_type__in=GENETIC_DOC_TYPES).exists()
        or bool(record and (_text(record.cytogenetic_markers) or record.genetic_mutations)),
        'imaging': docs.filter(doc_type='IMAGING').exists(),
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
        return lab_history(person, self.test_id)
