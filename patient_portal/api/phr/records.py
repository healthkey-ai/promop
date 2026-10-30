"""Medications, procedures and genetic testing for the patient's own record.

Read-only views over OMOP rows. Every item carries its source; entered-in-error
rows are left out. Patient input (confirming medications, adding procedures)
is a later phase and is stored beside these rows, never over them.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import date, timedelta
from typing import Any

from django.utils import timezone
from django.utils.text import slugify

from omop_core.models import DrugExposure, Measurement, PatientDocument, ProcedureOccurrence, Provider
from omop_core.services.medication_context import drug_event_concepts, phr_medication_contexts

from patient_portal.models import PatientStatement

from .names import drug_name, procedure_name
from .sources import row_sources, source

# ---------------------------------------------------------------- medications


def _drug_name(row: DrugExposure) -> str | None:
    return drug_name(row) or None


def _group_key(row: DrugExposure) -> str:
    if row.drug_concept_id:
        return str(row.drug_concept_id)
    # Unmapped drugs are grouped by their source text, slugged for use in a URL.
    return f"src-{slugify(row.drug_source_value or '')}"


def _dose(row: DrugExposure) -> str | None:
    if (row.sig or '').strip():
        return row.sig.strip()
    if row.quantity is not None:
        quantity = row.quantity.normalize()
        unit = (row.dose_unit_source_value or '').strip()
        return f"{quantity:f} {unit}".strip()
    return None


def _is_current(row: DrugExposure, today: date) -> bool:
    end = row.drug_exposure_end_date
    return end is None or end >= today


def _exposures(person):
    return list(
        DrugExposure.objects.filter(person=person, is_erroneous=False)
        .select_related('drug_concept', 'drug_source_concept', 'visit_occurrence')
        .order_by('-drug_exposure_start_date', '-drug_exposure_id')
    )


def _in_lines_of_therapy(person, rows: list[DrugExposure]) -> set[int]:
    """Exposures that belong to one of the patient's lines of therapy (Episode → EpisodeEvent)."""
    from omop_oncology.models import Episode, EpisodeEvent

    episodes = Episode.objects.filter(person=person).values_list('episode_id', flat=True)
    return set(
        EpisodeEvent.objects.filter(episode_id__in=list(episodes), event_id__in=[r.pk for r in rows],
                                   episode_event_field_concept_id__in=drug_event_concepts().values('pk'))
        .values_list('event_id', flat=True)
    )


def _medication_groups(person) -> tuple[OrderedDict, dict]:
    rows = _exposures(person)
    sources = row_sources(DrugExposure, rows, type_attr='drug_type_concept', date_attr='drug_exposure_start_date')
    groups: OrderedDict[str, list[DrugExposure]] = OrderedDict()
    memo: dict = {}
    for row in rows:
        if drug_name(row, memo):
            groups.setdefault(_group_key(row), []).append(row)
    # Medicines given as part of a line of therapy are shown with that line;
    # here they are not asked about as prescriptions.
    in_line = _in_lines_of_therapy(person, rows)
    contexts = phr_medication_contexts(person.pk, [r.pk for r in rows])
    for row in rows:
        row.medication_context = contexts.get(row.pk, {})
        row.in_line_of_therapy = row.pk in in_line
    return groups, sources


def _statements(person, subject: str) -> dict[str, PatientStatement]:
    return {s.subject_key: s for s in PatientStatement.objects.filter(person=person, subject=subject)}


# Prescriptions from the record wait for the patient to say whether they took
# them only when given in the last year; older ones count as taken (#123).
ASK_WITHIN = timedelta(days=365)


def _summary(key: str, rows: list[DrugExposure], sources: dict, today: date,
             statement: PatientStatement | None = None, with_statements: bool = True,
             note: PatientStatement | None = None) -> dict[str, Any]:
    latest = rows[0]
    current = any(_is_current(r, today) for r in rows)
    ends = [r.drug_exposure_end_date for r in rows if r.drug_exposure_end_date]
    ended = None if current or not ends else max(ends)
    in_line = all(getattr(r, 'in_line_of_therapy', False) for r in rows)
    source = sources[latest.pk]
    confirmation = None
    if with_statements and statement is not None:
        confirmation = {k: v for k, v in {
            'status': statement.status,
            'note': statement.note or None,
            'stopped_on': statement.stopped_on.isoformat() if statement.stopped_on else None,
        }.items() if v is not None}
        if statement.status == PatientStatement.STOPPED and current:
            # Stopped by the patient: no longer current, history intact.
            current, ended = False, statement.stopped_on
    unanswered = with_statements and source['kind'] == 'record' and statement is None and not in_line
    if unanswered and latest.drug_exposure_start_date < today - ASK_WITHIN:
        # Taken unless the patient says otherwise, which they can as usual.
        confirmation = {'status': PatientStatement.TAKING if current else PatientStatement.TOOK, 'assumed': True}
        unanswered = False
    item = {
        'id': key,
        'drug_exposure_id': latest.pk,
        **getattr(latest, 'medication_context', {}),
        'name': _drug_name(latest),
        'dose': _dose(latest),
        'status': 'current' if current else 'past',
        'started': min(r.drug_exposure_start_date for r in rows).isoformat(),
        'ended': ended.isoformat() if ended else None,
        'source': source,
        'confirmation': confirmation,
        'my_note': (note.note or None) if with_statements and note is not None else None,
        'pending': True if unanswered else None,
    }
    return {k: v for k, v in item.items() if v is not None}


def medication_summary_by_key(person, key: str, with_statements: bool = True) -> dict[str, Any] | None:
    """One medication's summary — judged on the record alone when
    ``with_statements`` is False, which is how answers are validated."""
    groups, sources = _medication_groups(person)
    rows = groups.get(key)
    if not rows:
        return None
    statement = note = None
    if with_statements:
        statement = PatientStatement.objects.filter(
            person=person, subject=PatientStatement.SUBJECT_MEDICATION, subject_key=key).first()
        note = PatientStatement.objects.filter(
            person=person, subject=PatientStatement.SUBJECT_MEDICATION_NOTE, subject_key=key).first()
    return _summary(key, rows, sources, timezone.localdate(), statement, with_statements, note)


def medications(person, record) -> dict[str, Any]:
    groups, sources = _medication_groups(person)
    today = timezone.localdate()
    statements = _statements(person, PatientStatement.SUBJECT_MEDICATION)
    notes = _statements(person, PatientStatement.SUBJECT_MEDICATION_NOTE)
    items = [_summary(key, rows, sources, today, statements.get(key), note=notes.get(key))
             for key, rows in groups.items()]
    # Current first, each group by its latest prescription, newest first: the
    # date its card shows (#125). Sorts are stable.
    latest = {key: rows[0].drug_exposure_start_date for key, rows in groups.items()}
    items.sort(key=lambda m: latest[m['id']], reverse=True)
    items.sort(key=lambda m: m['status'] != 'current')
    return {'medications': items}


def medication_detail(person, key: str) -> dict[str, Any] | None:
    groups, sources = _medication_groups(person)
    rows = groups.get(key)
    if not rows:
        return None
    history = []
    for row in rows:
        entry = {
            'drug_exposure_id': row.pk,
            **getattr(row, 'medication_context', {}),
            'date': row.drug_exposure_start_date.isoformat(),
            'end_date': row.drug_exposure_end_date.isoformat() if row.drug_exposure_end_date else None,
            'dose': _dose(row),
            'source': sources[row.pk],
        }
        history.append({k: v for k, v in entry.items() if v is not None})
    return {**medication_summary_by_key(person, key), 'history': history}


# ---------------------------------------------------------------- procedures


def procedures(person, record) -> dict[str, Any]:
    rows = list(
        # Imaging studies are listed under Imaging (promop#1731).
        ProcedureOccurrence.objects.filter(person=person, is_erroneous=False, image_occurrences__isnull=True)
        .select_related('procedure_concept', 'procedure_source_concept', 'visit_occurrence')
        .order_by('-procedure_date', '-procedure_occurrence_id')
    )
    sources = row_sources(ProcedureOccurrence, rows, type_attr='procedure_type_concept', date_attr='procedure_date')
    provider_ids = {r.provider_id for r in rows if r.provider_id}
    providers = dict(
        Provider.objects.filter(provider_id__in=provider_ids)
        .exclude(provider_name__isnull=True)
        .values_list('provider_id', 'provider_name')
    ) if provider_ids else {}
    entries = _statements(person, PatientStatement.SUBJECT_PROCEDURE)
    items = []
    memo: dict = {}
    for row in rows:
        name = procedure_name(row, memo)
        if not name:
            continue
        entry = entries.get(str(row.pk))
        details = entry.details if entry is not None else {}
        dated = not details.get('date_unknown')
        item = {
            'id': row.pk,
            'name': name,
            'date': row.procedure_date.isoformat() if dated else None,
            'where': details.get('where'),
            'note': (entry.note or None) if entry is not None else None,
            'end_date': row.procedure_end_date.isoformat()
            if row.procedure_end_date and row.procedure_end_date != row.procedure_date
            else None,
            'performed_by': (providers.get(row.provider_id) or '').strip() or None,
            'source': sources[row.pk] if dated else {**sources[row.pk], 'date': None},
        }
        items.append({k: v for k, v in item.items() if v is not None})
    return {'procedures': items}


# ---------------------------------------------------------------- genetics

UNLABELLED = 'Genetic test'
GENETIC_DOC_LABELS = {
    'FISH': 'FISH',
    'CYTOGENETICS': 'Cytogenetics',
    'NGS': 'NGS',
    'GEP': 'GEP',
    'CYTOMETRY': 'Flow cytometry',
    'MRD': 'MRD',
    'BONE_MARROW': 'Bone marrow',
}


def _finding(variant: dict, sources: dict) -> dict[str, Any]:
    gene = (variant.get('gene') or '').strip()
    feature = (variant.get('genomic_feature') or '').strip()
    item = {
        'id': variant.get('id'),
        # Gene symbols in capitals (TP53); a named result like "GEP70 risk score" as written.
        'name': feature or (gene if ' ' in gene else gene.upper()),
        'variant': (variant.get('variant') or '').strip() or None,
        'interpretation': variant.get('interpretation'),
        'origin': variant.get('origin'),
        'percent': variant.get('allelic_frequency'),
        'description': variant.get('description'),
        'source': sources.get(variant.get('id')),
    }
    return {k: v for k, v in item.items() if v not in (None, '')}


def genetics(person, record) -> dict[str, Any]:
    from omop_core.services.genomics import list_variants

    variants = list_variants(person)
    rows = list(
        Measurement.objects.filter(pk__in=[v['id'] for v in variants if v.get('id')])
        .select_related('visit_occurrence')
    )
    sources = row_sources(Measurement, rows, type_attr='measurement_type_concept', date_attr='measurement_date')

    # One test per (method, date): the findings a single report produced.
    tests: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
    for variant in sorted(variants, key=lambda v: v.get('test_date') or '', reverse=True):
        # Imported findings carry assay_method; the Genomics editor writes the
        # variant analysis method component (LOINC 81304-8).
        method = ((variant.get('assay_method') or '').strip()
                  or (variant.get('variant_analysis_method_type') or '').strip() or UNLABELLED)
        when = variant.get('test_date')
        test = tests.setdefault((method.lower(), when), {
            'id': f"test-{variant.get('id')}",
            'type': method,
            'date': when,
            'findings': [],
        })
        test['findings'].append(_finding(variant, sources))
        # Specimen and pathologist notes describe the report, so every finding
        # from it repeats them (D22, #1865). Show each distinct value once.
        for key, field in (('specimen', 'specimen_type'), ('notes', 'pathologist_notes')):
            value = (variant.get(field) or '').strip()
            if value and value not in test.setdefault(key, []):
                test[key].append(value)

    documents = list(PatientDocument.objects.filter(
        person=person, doc_type__in=GENETIC_DOC_LABELS,
    ).order_by('-effective_date', '-uploaded_at'))
    same_day_docs = Counter(doc.effective_date or doc.uploaded_at.date() for doc in documents)
    for doc in documents:
        on = doc.effective_date or doc.uploaded_at.date()
        label = GENETIC_DOC_LABELS[doc.doc_type]
        document = {'title': doc.title or label}
        # Only a link the browser can open directly; stored files need a
        # signed-download endpoint, which is a later change.
        if (doc.file_url or '').startswith('https://'):
            document['url'] = doc.file_url
        match = tests.get((label.lower(), on.isoformat()))
        if match is None and same_day_docs[on] == 1:
            # Findings recorded without a method (e.g. FISH translocations with
            # no gene-specific code) belong to the only report from that day.
            match = tests.get((UNLABELLED.lower(), on.isoformat()))
            if match is not None:
                match['type'] = label
        if match is not None:
            match.setdefault('document', document)
            continue
        tests[(label.lower(), on.isoformat(), doc.pk)] = {
            'id': f'doc-{doc.pk}',
            'type': label,
            'date': on.isoformat(),
            'findings': [],
            'document': document,
            'source': source('record', None, on),
        }

    items = []
    for test in tests.values():
        if 'source' not in test:
            first = next((f['source'] for f in test['findings'] if f.get('source')), None)
            test['source'] = first or source('record', None, None)
        if 'specimen' in test:
            test['specimen'] = '; '.join(test['specimen'])
        if 'notes' in test:
            test['notes'] = '\n\n'.join(test['notes'])
        items.append({k: v for k, v in test.items() if v is not None})
    items.sort(key=lambda t: t.get('date') or '', reverse=True)
    return {'tests': items}
