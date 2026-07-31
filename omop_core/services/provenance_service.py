"""
provenance_service.py — Per-(row, field) OMOP provenance lookup.

Given a Person and a PatientRecord field name, returns the OMOP source rows
that produced the current projected value.  The lookup is computed at query
time by re-executing the same query the section extractor used.
"""

from __future__ import annotations

import logging
from typing import Any

from omop_core.models import (
    ConditionOccurrence,
    DrugExposure,
    Measurement,
    Observation,
    PatientRecord,
    Person,
    ProcedureOccurrence,
)
from omop_core.services.concept_cache import concept_by_loinc as _cc_by_loinc
from omop_core.services.provenance_registry import FieldProvenance, get_registry

logger = logging.getLogger(__name__)


def get_field_provenance(person: Person, field_name: str) -> dict[str, Any] | None:
    """Return the OMOP source rows that produced a PatientRecord field value.

    Returns None if the field is not in the provenance registry.
    """
    registry = get_registry()
    entry = registry.get(field_name)
    if entry is None:
        return None

    # Get the current projected value.
    try:
        record = PatientRecord.objects.get(person=person)
        current_value = getattr(record, field_name, None)
        derivation_version = record.derivation_version
        derived_at = record.derived_at.isoformat() if record.derived_at else None
    except PatientRecord.DoesNotExist:
        current_value = None
        derivation_version = None
        derived_at = None

    # For composite fields, recurse into constituent fields.
    if entry.selection_rule == "composite" and entry.constituent_fields:
        source_rows = _lookup_composite(person, entry)
    else:
        source_rows = _lookup_simple(person, entry)

    return {
        "field": field_name,
        "current_value": _serialize_value(current_value),
        "derivation_version": derivation_version,
        "derived_at": derived_at,
        "selection_rule": entry.selection_rule,
        "description": entry.description,
        "source_rows": source_rows,
    }


def get_fields_provenance(
    person: Person, field_names: list[str]
) -> dict[str, dict[str, Any] | None]:
    """Bulk provenance lookup for multiple fields."""
    return {name: get_field_provenance(person, name) for name in field_names}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _lookup_simple(person: Person, entry: FieldProvenance) -> list[dict]:
    """Query the appropriate OMOP table and return candidate rows."""
    table = entry.omop_table.split(",")[0].strip()

    if table == "Measurement":
        return _lookup_measurement(person, entry)
    elif table == "Observation":
        return _lookup_observation(person, entry)
    elif table == "ConditionOccurrence":
        return _lookup_condition(person, entry)
    elif table == "DrugExposure":
        return _lookup_drug_exposure(person, entry)
    elif table == "ProcedureOccurrence":
        return _lookup_procedure(person, entry)
    elif table == "Death":
        return _lookup_death(person)
    else:
        return []


def _lookup_measurement(person: Person, entry: FieldProvenance) -> list[dict]:
    """Return Measurement rows matching LOINC codes or source values."""
    from django.db.models import Q

    qs = (
        Measurement.objects.filter(person=person)
        .select_related("measurement_concept")
        .order_by("-measurement_date")
    )

    filters = Q()
    if entry.concept_codes:
        # Look up concept_ids for the LOINC codes.
        concept_ids = []
        for code in entry.concept_codes:
            concept = _cc_by_loinc(code)
            if concept:
                concept_ids.append(concept.concept_id)
        if concept_ids:
            filters |= Q(measurement_concept_id__in=concept_ids)
        # Also match by concept_code directly.
        filters |= Q(measurement_concept__concept_code__in=entry.concept_codes)

    if entry.source_values:
        filters |= Q(measurement_source_value__in=entry.source_values)

    if not filters:
        return []

    rows = qs.filter(filters)[:20]  # Cap to avoid huge responses.
    result = []
    for i, m in enumerate(rows):
        concept_name = (
            m.measurement_concept.concept_name if m.measurement_concept else None
        )
        result.append({
            "table": "Measurement",
            "id": m.measurement_id,
            "concept_id": m.measurement_concept_id,
            "concept_name": concept_name,
            "value": m.value_as_number,
            "unit": m.unit_source_value,
            "date": str(m.measurement_date) if m.measurement_date else None,
            "selected": i == 0,  # Latest is selected.
        })
    return result


def _lookup_observation(person: Person, entry: FieldProvenance) -> list[dict]:
    """Return Observation rows matching the extractor's query pattern."""
    qs = (
        Observation.objects.filter(person=person)
        .select_related("observation_concept")
        .order_by("-observation_date")
    )

    if entry.concept_codes:
        from django.db.models import Q

        filters = Q(observation_concept__concept_code__in=entry.concept_codes)
        filters |= Q(observation_source_value__in=entry.concept_codes)
        qs = qs.filter(filters)

    rows = qs[:20]
    result = []
    for i, o in enumerate(rows):
        concept_name = (
            o.observation_concept.concept_name if o.observation_concept else None
        )
        value = o.value_as_number if o.value_as_number is not None else o.value_as_string
        result.append({
            "table": "Observation",
            "id": o.observation_id,
            "concept_id": o.observation_concept_id,
            "concept_name": concept_name,
            "value": value,
            "date": str(o.observation_date) if o.observation_date else None,
            "selected": i == 0 if entry.selection_rule == "latest" else True,
        })
    return result


def _lookup_condition(person: Person, entry: FieldProvenance) -> list[dict]:
    """Return ConditionOccurrence rows."""
    qs = (
        ConditionOccurrence.objects.filter(person=person)
        .select_related("condition_concept")
        .order_by("-condition_start_date")
    )
    rows = qs[:20]
    result = []
    for i, c in enumerate(rows):
        concept_name = (
            c.condition_concept.concept_name if c.condition_concept else None
        )
        result.append({
            "table": "ConditionOccurrence",
            "id": c.condition_occurrence_id,
            "concept_id": c.condition_concept_id,
            "concept_name": concept_name,
            "value": concept_name,
            "date": str(c.condition_start_date) if c.condition_start_date else None,
            "selected": i == 0 if entry.selection_rule == "latest" else (
                i == len(rows) - 1 if entry.selection_rule == "earliest" else True
            ),
        })
    return result


def _lookup_drug_exposure(person: Person, entry: FieldProvenance) -> list[dict]:
    """Return DrugExposure rows."""
    qs = (
        DrugExposure.objects.filter(person=person)
        .select_related("drug_concept")
        .order_by("drug_exposure_start_date")
    )
    rows = qs[:20]
    result = []
    for i, d in enumerate(rows):
        concept_name = d.drug_concept.concept_name if d.drug_concept else None
        result.append({
            "table": "DrugExposure",
            "id": d.drug_exposure_id,
            "concept_id": d.drug_concept_id,
            "concept_name": concept_name,
            "value": concept_name,
            "start_date": str(d.drug_exposure_start_date) if d.drug_exposure_start_date else None,
            "end_date": str(d.drug_exposure_end_date) if d.drug_exposure_end_date else None,
            "selected": True,
        })
    return result


def _lookup_procedure(person: Person, entry: FieldProvenance) -> list[dict]:
    """Return ProcedureOccurrence rows."""
    qs = (
        ProcedureOccurrence.objects.filter(person=person)
        .select_related("procedure_concept")
        .order_by("-procedure_date")
    )
    rows = qs[:20]
    result = []
    for p in rows:
        concept_name = (
            p.procedure_concept.concept_name if p.procedure_concept else None
        )
        result.append({
            "table": "ProcedureOccurrence",
            "id": p.procedure_occurrence_id,
            "concept_id": p.procedure_concept_id,
            "concept_name": concept_name,
            "value": concept_name,
            "date": str(p.procedure_date) if p.procedure_date else None,
            "selected": True,
        })
    return result


def _lookup_death(person: Person) -> list[dict]:
    """Return Death row if present."""
    from omop_core.models import Death

    try:
        d = Death.objects.get(person=person)
        return [{
            "table": "Death",
            "id": d.pk,
            "concept_id": None,
            "concept_name": None,
            "value": str(d.death_date) if d.death_date else None,
            "date": str(d.death_date) if d.death_date else None,
            "selected": True,
        }]
    except Death.DoesNotExist:
        return []


def _lookup_composite(person: Person, entry: FieldProvenance) -> list[dict]:
    """For composite fields, collect source rows from all constituent fields."""
    registry = get_registry()
    all_rows: list[dict] = []
    for sub_field in entry.constituent_fields:
        sub_entry = registry.get(sub_field)
        if sub_entry is None:
            continue
        rows = _lookup_simple(person, sub_entry)
        # Tag each row with the constituent field it belongs to.
        for row in rows:
            row["constituent_field"] = sub_field
        all_rows.extend(rows)
    return all_rows


def _serialize_value(value: Any) -> Any:
    """Ensure the value is JSON-serializable."""
    import decimal

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return value
    # Fallback for dates etc.
    return str(value)
