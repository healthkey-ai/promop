"""Where a value came from, for every item the PHR shows.

The PHR states, next to each value, whether it came from the patient's records
or from the patient, the facility that reported it and the date it refers to.
OMOP already carries all three; this module reads them in batches so a section
costs a fixed number of queries however many rows it has.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from django.contrib.contenttypes.models import ContentType

from omop_core.models import CareSite, ProvenanceRecord

# OMOP type concept "Patient self-report" (Type Concept vocabulary).
PATIENT_SELF_REPORT_TYPE_CONCEPT_ID = 32865

RECORD = 'record'
PATIENT = 'patient'


def source(kind: str, facility: str | None = None, on: date | None = None) -> dict[str, Any]:
    return {
        'kind': kind,
        'facility': facility or None,
        'date': on.isoformat() if on else None,
    }


def patient_authored_ids(model, pks: Iterable[int]) -> set[int]:
    """Primary keys among ``pks`` that a patient wrote themselves."""
    pks = list(pks)
    if not pks:
        return set()
    return set(
        ProvenanceRecord.objects.filter(
            content_type=ContentType.objects.get_for_model(model),
            object_id__in=pks,
            source='PATIENT_SELF',
        ).values_list('object_id', flat=True)
    )


def care_site_names(rows: Sequence[Any]) -> dict[int, str]:
    """care_site_id → name for the visits ``rows`` point at (visit preloaded)."""
    ids = {
        row.visit_occurrence.care_site_id
        for row in rows
        if row.visit_occurrence_id and row.visit_occurrence.care_site_id
    }
    if not ids:
        return {}
    return dict(
        CareSite.objects.filter(care_site_id__in=ids)
        .exclude(care_site_name__isnull=True)
        .exclude(care_site_name='')
        .values_list('care_site_id', 'care_site_name')
    )


def row_sources(
    model,
    rows: Sequence[Any],
    *,
    type_attr: str,
    date_attr: str,
) -> dict[int, dict[str, Any]]:
    """pk → source for OMOP clinical rows (visit_occurrence preloaded)."""
    patient_ids = patient_authored_ids(model, (row.pk for row in rows))
    sites = care_site_names(rows)
    result = {}
    for row in rows:
        is_patient = (
            row.pk in patient_ids
            or getattr(row, f'{type_attr}_id') == PATIENT_SELF_REPORT_TYPE_CONCEPT_ID
        )
        site = (
            sites.get(row.visit_occurrence.care_site_id)
            if row.visit_occurrence_id
            else None
        )
        result[row.pk] = source(
            PATIENT if is_patient else RECORD,
            None if is_patient else site,
            getattr(row, date_attr),
        )
    return result
