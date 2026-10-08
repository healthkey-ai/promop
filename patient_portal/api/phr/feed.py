"""What's new and Imaging.

What's new lists the last 30 days of the record, grouped by date, then section,
then facility. OMOP rows carry no import timestamp, so clinical rows are placed
by the date they refer to; documents use their upload time, which is when they
actually arrived.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from django.utils import timezone

from omop_core.models import ConditionOccurrence, DrugExposure, Measurement, PatientDocument, ProcedureOccurrence

from .labs import VITALS, effective_concept
from .records import GENETIC_DOC_LABELS
from .sources import care_site_names, row_sources, source

WINDOW_DAYS = 30
SECTION_ORDER = ['labs', 'medications', 'diagnoses', 'procedures', 'genetics', 'imaging']


def _site(row, sites):
    return sites.get(row.visit_occurrence.care_site_id) if row.visit_occurrence_id else None


def whats_new(person, record) -> dict[str, Any]:
    since = timezone.localdate() - timedelta(days=WINDOW_DAYS)
    counts: Counter = Counter()

    def tally(model, date_field, section, extra=None, **filters):
        rows = list(
            model.objects.filter(person=person, is_erroneous=False, **{f'{date_field}__gte': since}, **filters)
            .select_related('visit_occurrence', *(extra or []))
        )
        sites = care_site_names(rows)
        for row in rows:
            if section == 'labs':
                concept = effective_concept(row)
                if concept is None or concept.concept_code in VITALS or not (
                        row.value_as_number is not None or row.value_as_string):
                    continue
            counts[(getattr(row, date_field), section, _site(row, sites))] += 1

    tally(Measurement, 'measurement_date', 'labs', ['measurement_concept', 'measurement_source_concept'])
    tally(DrugExposure, 'drug_exposure_start_date', 'medications')
    tally(ConditionOccurrence, 'condition_start_date', 'diagnoses')
    # An imaging study is a procedure row with an image_occurrence (#1731).
    tally(ProcedureOccurrence, 'procedure_date', 'procedures', image_occurrences__isnull=True)
    tally(ProcedureOccurrence, 'procedure_date', 'imaging', image_occurrences__isnull=False)
    # A study's own report file is counted with the study.
    for doc in PatientDocument.objects.filter(person=person, uploaded_at__date__gte=since,
                                              procedure_occurrence_id__isnull=True):
        section = 'imaging' if doc.doc_type == 'IMAGING' else 'genetics' if doc.doc_type in GENETIC_DOC_LABELS else None
        if section:
            counts[(timezone.localtime(doc.uploaded_at).date(), section, None)] += 1

    by_date: dict = defaultdict(list)
    for (day, section, facility), count in counts.items():
        item = {'section': section, 'count': count}
        if facility:
            item['facility'] = facility
        by_date[day].append(item)
    groups = [
        {'date': day.isoformat(),
         'items': sorted(items, key=lambda i: (SECTION_ORDER.index(i['section']), i.get('facility') or ''))}
        for day, items in sorted(by_date.items(), reverse=True)
    ]
    return {'days': WINDOW_DAYS, 'groups': groups}


def _document(doc: PatientDocument, fallback: str) -> dict[str, Any]:
    document = {'title': doc.title or fallback}
    if (doc.file_url or '').startswith('https://'):
        document['url'] = doc.file_url
    return document


def imaging(person, record) -> dict[str, Any]:
    """Imaging studies (promop#1731), newest first, then any imaging report
    filed without a study (an uploaded file) as it was filed."""
    from omop_core.services.imaging import imaging_studies

    structured = imaging_studies(person)
    sources = row_sources(
        ProcedureOccurrence, [s['procedure'] for s in structured],
        type_attr='procedure_type_concept', date_attr='procedure_date',
    )
    studies = []
    for study in structured:
        pid = study['procedure_occurrence_id']
        item = {
            'id': f'study-{pid}',
            'type': study['type'],
            'name': study['name'],
            'date': study['date'].isoformat(),
            'body_part': study['body_part'],
            'contrast': study['contrast'],
            'impression': study['impression'],
            'findings': study['findings'],
            'notes': study['notes'],
            'has_image': study['has_image'],
            'image_url': study['image_url'],
            'document': _document(study['document'], 'Imaging report') if study['document'] else None,
            'source': sources[pid],
        }
        studies.append({k: v for k, v in item.items() if v is not None or k in ('image_url', 'impression')})

    filed = PatientDocument.objects.filter(
        person=person, doc_type='IMAGING', procedure_occurrence_id__isnull=True,
    ).order_by('-effective_date', '-uploaded_at')
    for doc in filed:
        on = doc.effective_date or timezone.localtime(doc.uploaded_at).date()
        studies.append({
            'id': f'doc-{doc.pk}',
            'name': doc.title or 'Imaging report',
            'date': on.isoformat(),
            'document': _document(doc, 'Imaging report'),
            'source': source('record', None, on),
        })
    studies.sort(key=lambda s: s['date'], reverse=True)
    return {'studies': studies}
