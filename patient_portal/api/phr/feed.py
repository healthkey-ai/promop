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

    def tally(model, date_field, section, extra=None):
        rows = list(
            model.objects.filter(person=person, is_erroneous=False, **{f'{date_field}__gte': since})
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
    tally(ProcedureOccurrence, 'procedure_date', 'procedures')
    for doc in PatientDocument.objects.filter(person=person, uploaded_at__date__gte=since):
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


def imaging(person, record) -> dict[str, Any]:
    """Imaging reports. Structured studies (modality, body part, impression)
    arrive with promop#1731; until then each report is listed as filed."""
    studies = []
    for doc in PatientDocument.objects.filter(person=person, doc_type='IMAGING').order_by('-effective_date', '-uploaded_at'):
        on = doc.effective_date or timezone.localtime(doc.uploaded_at).date()
        document = {'title': doc.title or 'Imaging report'}
        if (doc.file_url or '').startswith('https://'):
            document['url'] = doc.file_url
        studies.append({
            'id': f'doc-{doc.pk}',
            'name': doc.title or 'Imaging report',
            'date': on.isoformat(),
            'document': document,
            'source': source('record', None, on),
        })
    return {'studies': studies}
