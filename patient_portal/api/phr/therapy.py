"""Lines of therapy, one track per diagnosis.

Built on PRomop's own derivation (``PatientRecordSerializer.get_lines_of_therapy``
— regimen, dates, outcome, intent, stop reason, Episode) and adds what the
record shows per line: each medicine with its own dates, the treatment
procedures inside the line, a plain-language outcome, and the source.

One track per cancer (#1739): each line hangs off the Disease Episode of the
cancer it treats. The primary cancer's lines come from PRomop's derivation, the
others straight from their Episodes (``disease_episodes.lines_by_disease``).
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


class _Context:
    """What every line needs, read once per request."""

    def __init__(self, person, record):
        from patient_portal.models import PatientStatement

        self.person, self.record = person, record
        self.today = timezone.localdate()
        self.procedures = list(
            ProcedureOccurrence.objects.filter(person=person, is_erroneous=False)
            .select_related('procedure_concept', 'visit_occurrence')
            .order_by('procedure_date')
        )
        self.proc_sources = row_sources(
            ProcedureOccurrence, self.procedures, type_attr='procedure_type_concept', date_attr='procedure_date',
        )
        self.reasons = {
            s.subject_key: s for s in PatientStatement.objects.filter(
                person=person, subject=PatientStatement.SUBJECT_THERAPY_LINE)
        }
        self.facility = record.facility_name if record else None


def _shape(ctx: _Context, entry: dict) -> dict[str, Any] | None:
    """One line for display, from PRomop's line fields (derived or per-cancer)."""
    start, end = _d(entry.get('start_date')), _d(entry.get('end_date'))
    if start is None:
        return None
    window_end = end or ctx.today
    procs = []
    for row in ctx.procedures:
        name = (
            row.procedure_concept.concept_name if row.procedure_concept_id and row.procedure_concept
            else row.procedure_source_value
        ) or ''
        if start <= row.procedure_date <= window_end and _TREATMENT_PROCEDURE.search(name):
            procs.append({'name': name.strip(), 'date': row.procedure_date.isoformat(),
                          'source': ctx.proc_sources[row.pk]})
    reason = (entry.get('discontinuation_reason') or '').strip()
    line = {
        'id': str(entry.get('episode_id') or f"line-{entry['line']}"),
        'number': entry['line'],
        'regimen': (entry.get('regimen') or '').strip() or None,
        'start': start.isoformat(),
        'end': end.isoformat() if end else None,
        'current': end is None or end >= ctx.today,
        'medications': _medications(ctx.person, entry.get('episode_id')),
        'procedures': procs,
        'outcome': _outcome(entry.get('outcome')),
        'intent': (entry.get('intent') or '').strip() or None,
        'stopped_because': STOP_REASONS.get(reason.lower().replace(' ', '_'), reason) or None,
        'source': source('record', ctx.facility, start),
    }
    told = ctx.reasons.get(line['id'])
    if told is not None:
        from .statements import END_REASONS

        line['patient_reason'] = {k: v for k, v in {
            'reason': told.reason, 'label': END_REASONS.get(told.reason, told.reason), 'note': told.note or None,
        }.items() if v is not None}
    return {k: v for k, v in line.items() if v is not None and v != []}


def _primary(ctx: _Context) -> list[dict[str, Any]]:
    """The primary cancer's lines, from PRomop's own derivation (richest regimen naming).

    Until the record is next derived (a line was just written), its Episodes.
    """
    from omop_core.services.disease_episodes import lines_by_disease
    from patient_portal.api.serializers import PatientRecordSerializer

    derived = (PatientRecordSerializer().get_lines_of_therapy(ctx.record) or []) if ctx.record else []
    if not derived:
        derived = next((g['lines'] for g in lines_by_disease(ctx.person) if g['primary']), [])
    shaped = (_shape(ctx, entry) for entry in sorted(derived, key=lambda e: e.get('line') or 0))
    return [line for line in shaped if line]


def therapy_lines(person, record) -> list[dict[str, Any]]:
    """The primary cancer's lines, oldest first, shaped for display."""
    return _primary(_Context(person, record))


def _cancer_names(person, record) -> dict[str, str]:
    """Each cancer diagnosis by the slug its lines are filed under."""
    from omop_core.services.disease_episodes import disease_slug

    from .views import diagnoses

    names = {}
    for cancer in diagnoses(person, record)['cancer']:
        if cancer['id'] == 'primary':
            continue  # the record's primary cancer, which has its own track
        slug = disease_slug(cancer['name'])
        if slug:
            names.setdefault(slug, cancer['name'])
    return names


def therapy(person, record) -> dict[str, Any]:
    """One track per cancer, primary first (#1739).

    A cancer with no lines still has a track, with no lines, so the record can
    say so when filtered to it. No cancers and no lines: no tracks.
    """
    from omop_core.services.disease_episodes import lines_by_disease

    ctx = _Context(person, record)
    names = _cancer_names(person, record)
    primary_slug = ((record.disease_slug if record else '') or '').lower()
    tracks = []
    primary = _primary(ctx)
    if primary or (record and record.disease):
        name = ((record.disease if record else '') or '').strip() or names.get(primary_slug) or 'Your cancer'
        tracks.append({'diagnosis': {'name': name, 'slug': primary_slug or None}, 'lines': primary})
    for group in lines_by_disease(person):
        if group['primary']:
            continue
        lines = [line for line in (_shape(ctx, entry) for entry in group['lines']) if line]
        name = names.get(group['slug']) or group['slug'].replace('-', ' ').capitalize() or 'Another cancer'
        tracks.append({'diagnosis': {'name': name, 'slug': group['slug'] or None}, 'lines': lines})
    # Cancers with no lines. The same cancer can be slugged two ways (the
    # record's stored slug and its name's), so match on the name too.
    seen = {t['diagnosis']['slug'] for t in tracks}
    seen_names = {t['diagnosis']['name'].lower() for t in tracks}
    for slug, name in names.items():
        if slug not in seen and name.lower() not in seen_names:
            tracks.append({'diagnosis': {'name': name, 'slug': slug}, 'lines': []})
    if not any(t['lines'] for t in tracks):
        return {'tracks': []}
    return {'tracks': tracks}


def all_lines(person, record) -> list[dict[str, Any]]:
    """Every cancer's lines."""
    return [line for track in therapy(person, record)['tracks'] for line in track['lines']]


def lines_for_markers(person, record, disease_slugs) -> list[dict[str, Any]]:
    """The lines a lab test's chart is read against: the cancer it marks, else the primary one."""
    slugs = {s.lower() for s in disease_slugs or []}
    if slugs:
        for track in therapy(person, record)['tracks']:
            if (track['diagnosis']['slug'] or '') in slugs and track['lines']:
                return track['lines']
    return therapy_lines(person, record)
