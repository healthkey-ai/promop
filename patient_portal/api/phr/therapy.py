"""Lines of therapy, one track per diagnosis.

Built on PRomop's own derivation (``PatientRecordSerializer.get_lines_of_therapy``
— regimen, dates, outcome, intent, stop reason, Episode) and adds what the
record shows per line: each medicine with its own dates, the treatment
procedures inside the line, a plain-language outcome, and the source.

Every line is drawn on the primary cancer's track: PRomop numbers lines per
person, not per cancer (see ht-phr-one TODO.md, "Lines of therapy for a second
cancer").
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from django.utils import timezone

from omop_core.models import DrugExposure, ProcedureOccurrence
from omop_oncology.models import EpisodeEvent

from .sources import row_sources, source

# Response categories, explained without disease-specific thresholds: the
# numbers behind them differ between cancers (IMWG for myeloma, RECIST for
# solid tumours, Lugano for lymphoma).
OUTCOMES = {
    'sCR': ('Stringent complete response', 'No sign of the cancer, confirmed by more sensitive tests than for a complete response.'),
    'CR': ('Complete response', 'No sign of the cancer was found after this treatment.'),
    'VGPR': ('Very good partial response', 'The cancer got much smaller, but some could still be found.'),
    'PR': ('Partial response', 'The cancer got smaller but did not go away.'),
    'MR': ('Minimal response', 'The cancer got a little smaller.'),
    'SD': ('Stable disease', 'The cancer stayed about the same.'),
    'PD': ('Progressive disease', 'The cancer grew or spread during this treatment.'),
}
STOP_REASONS = {
    'completion': 'Treatment completed as planned',
    'progression': 'Stopped because the cancer progressed',
    'toxicity': 'Stopped because of side effects',
    'patient_choice': 'Stopped by your choice',
}
# Procedures that are part of a treatment plan rather than a test.
_TREATMENT_PROCEDURE = re.compile(
    r'transplant|car[ -]?t\b|cell therapy|infusion|radiation|radiotherapy|ectomy|resection|surgery|ablation',
    re.IGNORECASE,
)


def _d(value) -> date | None:
    if value in (None, ''):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _outcome(code: str | None) -> dict[str, str] | None:
    if not code:
        return None
    key = next((k for k in OUTCOMES if k.lower() == code.strip().lower()), None)
    if key is None:
        return {'code': code, 'label': code}
    label, explanation = OUTCOMES[key]
    return {'code': key, 'label': label, 'explanation': explanation}


def _medications(person, episode_id: int | None) -> list[dict[str, Any]]:
    if episode_id is None:
        return []
    ids = EpisodeEvent.objects.filter(episode_id=episode_id).values_list('event_id', flat=True)
    rows = (
        DrugExposure.objects.filter(person=person, drug_exposure_id__in=list(ids), is_erroneous=False)
        .select_related('drug_concept')
        .order_by('drug_exposure_start_date', 'drug_exposure_id')
    )
    meds: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = (
            row.drug_concept.concept_name if row.drug_concept_id and row.drug_concept else row.drug_source_value
        ) or ''
        name = name.strip()
        if not name:
            continue
        med = meds.setdefault(name.lower(), {'name': name, 'start': row.drug_exposure_start_date, 'end': None, 'open': False})
        med['start'] = min(med['start'], row.drug_exposure_start_date)
        if row.drug_exposure_end_date is None:
            med['open'] = True
        else:
            med['end'] = max(filter(None, [med['end'], row.drug_exposure_end_date]))
    return [
        {k: v for k, v in {
            'name': m['name'],
            'start': m['start'].isoformat(),
            'end': None if m['open'] or m['end'] is None else m['end'].isoformat(),
        }.items() if v is not None}
        for m in meds.values()
    ]


def therapy_lines(person, record) -> list[dict[str, Any]]:
    """The record's lines, oldest first, shaped for display."""
    if record is None:
        return []
    from patient_portal.api.serializers import PatientRecordSerializer

    derived = PatientRecordSerializer().get_lines_of_therapy(record) or []
    today = timezone.localdate()
    procedures = list(
        ProcedureOccurrence.objects.filter(person=person, is_erroneous=False)
        .select_related('procedure_concept', 'visit_occurrence')
        .order_by('procedure_date')
    )
    proc_sources = row_sources(ProcedureOccurrence, procedures, type_attr='procedure_type_concept', date_attr='procedure_date')

    from patient_portal.models import PatientStatement

    reasons = {
        s.subject_key: s for s in PatientStatement.objects.filter(
            person=person, subject=PatientStatement.SUBJECT_THERAPY_LINE)
    }
    lines = []
    for entry in sorted(derived, key=lambda e: e.get('line') or 0):
        start, end = _d(entry.get('start_date')), _d(entry.get('end_date'))
        if start is None:
            continue
        window_end = end or today
        procs = []
        for row in procedures:
            name = (
                row.procedure_concept.concept_name if row.procedure_concept_id and row.procedure_concept
                else row.procedure_source_value
            ) or ''
            if start <= row.procedure_date <= window_end and _TREATMENT_PROCEDURE.search(name):
                procs.append({'name': name.strip(), 'date': row.procedure_date.isoformat(),
                              'source': proc_sources[row.pk]})
        reason = (entry.get('discontinuation_reason') or '').strip()
        line = {
            'id': str(entry.get('episode_id') or f"line-{entry['line']}"),
            'number': entry['line'],
            'regimen': (entry.get('regimen') or '').strip() or None,
            'start': start.isoformat(),
            'end': end.isoformat() if end else None,
            'current': end is None or end >= today,
            'medications': _medications(person, entry.get('episode_id')),
            'procedures': procs,
            'outcome': _outcome(entry.get('outcome')),
            'intent': (entry.get('intent') or '').strip() or None,
            'stopped_because': STOP_REASONS.get(reason.lower().replace(' ', '_'), reason) or None,
            'source': source('record', record.facility_name, start),
        }
        told = reasons.get(line['id'])
        if told is not None:
            from .statements import END_REASONS

            line['patient_reason'] = {k: v for k, v in {
                'reason': told.reason, 'label': END_REASONS.get(told.reason, told.reason), 'note': told.note or None,
            }.items() if v is not None}
        lines.append({k: v for k, v in line.items() if v is not None and v != []})
    return lines


def therapy(person, record) -> dict[str, Any]:
    lines = therapy_lines(person, record)
    if not lines:
        return {'tracks': []}
    name = (record.disease or '').strip() or 'Your cancer'
    return {
        'tracks': [{
            'diagnosis': {'name': name, 'slug': (record.disease_slug or '').lower() or None},
            'lines': lines,
        }],
    }
