"""Medications, procedures and genetic testing for the patient's own record.

Read-only views over OMOP rows. Every item carries its source; entered-in-error
rows are left out. Patient input (confirming medications, adding procedures)
is a later phase and is stored beside these rows, never over them.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import date
from typing import Any

from django.utils import timezone
from django.utils.text import slugify

from omop_core.models import DrugExposure, Measurement, PatientDocument, ProcedureOccurrence, Provider

from .sources import row_sources, source

# ---------------------------------------------------------------- medications


def _drug_name(row: DrugExposure) -> str | None:
    concept = row.drug_concept
    if concept is not None and row.drug_concept_id != 0 and (concept.concept_name or '').strip():
        return concept.concept_name.strip()
    return (row.drug_source_value or '').strip() or None


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
        .select_related('drug_concept', 'visit_occurrence')
        .order_by('-drug_exposure_start_date', '-drug_exposure_id')
    )


def _medication_groups(person) -> tuple[OrderedDict, dict]:
    rows = _exposures(person)
    sources = row_sources(DrugExposure, rows, type_attr='drug_type_concept', date_attr='drug_exposure_start_date')
    groups: OrderedDict[str, list[DrugExposure]] = OrderedDict()
    for row in rows:
        if _drug_name(row):
            groups.setdefault(_group_key(row), []).append(row)
    return groups, sources


def _summary(key: str, rows: list[DrugExposure], sources: dict, today: date) -> dict[str, Any]:
    latest = rows[0]
    current = any(_is_current(r, today) for r in rows)
    ends = [r.drug_exposure_end_date for r in rows if r.drug_exposure_end_date]
    item = {
        'id': key,
        'name': _drug_name(latest),
        'dose': _dose(latest),
        'status': 'current' if current else 'past',
        'started': min(r.drug_exposure_start_date for r in rows).isoformat(),
        'ended': None if current or not ends else max(ends).isoformat(),
        'source': sources[latest.pk],
    }
    return {k: v for k, v in item.items() if v is not None}


def medications(person, record) -> dict[str, Any]:
    groups, sources = _medication_groups(person)
    today = timezone.localdate()
    items = [_summary(key, rows, sources, today) for key, rows in groups.items()]
    # Current first, each group most recently started first (sorts are stable).
    items.sort(key=lambda m: m['started'], reverse=True)
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
            'date': row.drug_exposure_start_date.isoformat(),
            'end_date': row.drug_exposure_end_date.isoformat() if row.drug_exposure_end_date else None,
            'dose': _dose(row),
            'source': sources[row.pk],
        }
        history.append({k: v for k, v in entry.items() if v is not None})
    return {**_summary(key, rows, sources, timezone.localdate()), 'history': history}


# ---------------------------------------------------------------- procedures


def procedures(person, record) -> dict[str, Any]:
    rows = list(
        ProcedureOccurrence.objects.filter(person=person, is_erroneous=False)
        .select_related('procedure_concept', 'visit_occurrence')
        .order_by('-procedure_date', '-procedure_occurrence_id')
    )
    sources = row_sources(ProcedureOccurrence, rows, type_attr='procedure_type_concept', date_attr='procedure_date')
    provider_ids = {r.provider_id for r in rows if r.provider_id}
    providers = dict(
        Provider.objects.filter(provider_id__in=provider_ids)
        .exclude(provider_name__isnull=True)
        .values_list('provider_id', 'provider_name')
    ) if provider_ids else {}
    items = []
    for row in rows:
        concept = row.procedure_concept
        name = (
            concept.concept_name.strip()
            if concept is not None and row.procedure_concept_id != 0 and (concept.concept_name or '').strip()
            else (row.procedure_source_value or '').strip()
        )
        if not name:
            continue
        item = {
            'id': row.pk,
            'name': name,
            'date': row.procedure_date.isoformat(),
            'end_date': row.procedure_end_date.isoformat()
            if row.procedure_end_date and row.procedure_end_date != row.procedure_date
            else None,
            'performed_by': (providers.get(row.provider_id) or '').strip() or None,
            'source': sources[row.pk],
        }
        items.append({k: v for k, v in item.items() if v is not None})
    return {'procedures': items}


# ---------------------------------------------------------------- genetics

UNLABELLED = 'Genetic test'
GENETIC_DOC_LABELS = {
    'FISH': 'FISH',
    'CYTOGENETICS': 'Cytogenetics',
    'NGS': 'NGS',
    'GEP': 'Gene expression profiling',
    'CYTOMETRY': 'Flow cytometry',
    'MRD': 'MRD',
    'BONE_MARROW': 'Bone marrow',
}


def _finding(variant: dict, sources: dict) -> dict[str, Any]:
    gene = (variant.get('gene') or '').strip()
    feature = (variant.get('genomic_feature') or '').strip()
    item = {
        'id': variant.get('id'),
        'name': feature or gene.upper(),
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
        method = (variant.get('assay_method') or '').strip() or UNLABELLED
        when = variant.get('test_date')
        test = tests.setdefault((method.lower(), when), {
            'id': f"test-{variant.get('id')}",
            'type': method,
            'date': when,
            'findings': [],
        })
        test['findings'].append(_finding(variant, sources))

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
        items.append({k: v for k, v in test.items() if v is not None})
    items.sort(key=lambda t: t.get('date') or '', reverse=True)
    return {'tests': items}
