"""
FHIR R4 Bundle export service.

Converts a patient's OMOP CDM data into a FHIR R4 Bundle (type "searchset").
All OMOP tables are queried in bulk up front to avoid N+1 queries.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from omop_core.models import (
    ConditionOccurrence,
    DrugExposure,
    Measurement,
    Observation,
    Person,
    ProcedureOccurrence,
    VisitOccurrence,
)

# ---------------------------------------------------------------------------
# Vocabulary → FHIR system mapping
# ---------------------------------------------------------------------------

_VOCAB_TO_FHIR_SYSTEM: dict[str, str] = {
    "LOINC": "http://loinc.org",
    "SNOMED": "http://snomed.info/sct",
    "RxNorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "CPT4": "http://www.ama-assn.org/go/cpt",
    "ICD10CM": "http://hl7.org/fhir/sid/icd-10-cm",
    "HemOnc": "https://hemonc.org",
}

# Gender concept_id → FHIR AdministrativeGender code
_GENDER_MAP: dict[int, str] = {
    8532: "female",
    8507: "male",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fhir_system(vocabulary_id: str) -> str:
    """Map an OMOP vocabulary_id to a FHIR coding system URI."""
    return _VOCAB_TO_FHIR_SYSTEM.get(vocabulary_id, f"urn:oid:unknown:{vocabulary_id}")


def _make_coding(concept) -> dict[str, str] | None:
    """Build a FHIR Coding dict from an OMOP Concept, or None if concept is missing/zero."""
    if concept is None or concept.concept_id == 0:
        return None
    return {
        "system": _fhir_system(concept.vocabulary_id),
        "code": concept.concept_code,
        "display": concept.concept_name,
    }


def _date_str(d: date | datetime | None) -> str | None:
    """Format a date or datetime as an ISO string, or None."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.isoformat()
    return d.isoformat()


def _decimal_to_float(val: Decimal | None) -> float | None:
    """Convert a Decimal to float for JSON serialization, or None."""
    if val is None:
        return None
    return float(val)


def _make_entry(resource: dict[str, Any]) -> dict[str, Any]:
    """Wrap a FHIR resource in a Bundle entry."""
    resource_id = resource.get("id", str(uuid.uuid4()))
    return {
        "fullUrl": f"urn:uuid:{resource_id}",
        "resource": resource,
    }


# ---------------------------------------------------------------------------
# Resource builders
# ---------------------------------------------------------------------------

def _build_patient(person: Person) -> dict[str, Any]:
    """Convert an OMOP Person to a FHIR Patient resource."""
    resource_id = str(uuid.uuid4())
    patient: dict[str, Any] = {
        "resourceType": "Patient",
        "id": resource_id,
    }

    # Name
    name_parts: dict[str, Any] = {}
    if person.family_name:
        name_parts["family"] = person.family_name
    if person.given_name:
        name_parts["given"] = [person.given_name]
    if name_parts:
        patient["name"] = [name_parts]

    # Birth date
    if person.year_of_birth:
        month = person.month_of_birth or 1
        day = person.day_of_birth or 1
        try:
            birth_date = date(person.year_of_birth, month, day)
            patient["birthDate"] = birth_date.isoformat()
        except ValueError:
            # Invalid date components — use year only
            patient["birthDate"] = str(person.year_of_birth)

    # Gender
    gender_concept_id = person.gender_concept_id
    patient["gender"] = _GENDER_MAP.get(gender_concept_id, "unknown")

    return patient


def _build_condition(condition: ConditionOccurrence, patient_ref: str) -> dict[str, Any]:
    """Convert an OMOP ConditionOccurrence to a FHIR Condition resource."""
    resource_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "resourceType": "Condition",
        "id": resource_id,
        "subject": {"reference": patient_ref},
    }

    # Code
    coding = _make_coding(condition.condition_concept)
    code_obj: dict[str, Any] = {}
    if coding:
        code_obj["coding"] = [coding]
    if condition.condition_source_value:
        code_obj["text"] = condition.condition_source_value
    if code_obj:
        result["code"] = code_obj

    # Onset
    onset = _date_str(condition.condition_start_datetime) or _date_str(condition.condition_start_date)
    if onset:
        result["onsetDateTime"] = onset

    # Abatement
    abatement = _date_str(condition.condition_end_datetime) or _date_str(condition.condition_end_date)
    if abatement:
        result["abatementDateTime"] = abatement

    return result


def _build_measurement_observation(meas: Measurement, patient_ref: str) -> dict[str, Any]:
    """Convert an OMOP Measurement to a FHIR Observation resource."""
    resource_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "resourceType": "Observation",
        "id": resource_id,
        "status": "final",
        "subject": {"reference": patient_ref},
    }

    # Code
    coding = _make_coding(meas.measurement_concept)
    code_obj: dict[str, Any] = {}
    if coding:
        code_obj["coding"] = [coding]
    if meas.measurement_source_value:
        code_obj["text"] = meas.measurement_source_value
    if code_obj:
        result["code"] = code_obj

    # Effective date
    effective = _date_str(meas.measurement_datetime) or _date_str(meas.measurement_date)
    if effective:
        result["effectiveDateTime"] = effective

    # Value
    if meas.value_as_number is not None:
        quantity: dict[str, Any] = {"value": _decimal_to_float(meas.value_as_number)}
        if meas.unit_source_value:
            quantity["unit"] = meas.unit_source_value
        result["valueQuantity"] = quantity
    elif meas.value_as_concept_id and meas.value_as_concept_id != 0:
        val_coding = _make_coding(meas.value_as_concept)
        if val_coding:
            result["valueCodeableConcept"] = {"coding": [val_coding]}
    elif meas.value_as_string:
        result["valueString"] = meas.value_as_string

    # Reference range
    if meas.range_low is not None or meas.range_high is not None:
        ref_range: dict[str, Any] = {}
        if meas.range_low is not None:
            ref_range["low"] = {"value": _decimal_to_float(meas.range_low)}
        if meas.range_high is not None:
            ref_range["high"] = {"value": _decimal_to_float(meas.range_high)}
        result["referenceRange"] = [ref_range]

    return result


def _build_observation(obs: Observation, patient_ref: str) -> dict[str, Any]:
    """Convert an OMOP Observation to a FHIR Observation resource."""
    resource_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "resourceType": "Observation",
        "id": resource_id,
        "status": "final",
        "subject": {"reference": patient_ref},
    }

    # Code
    coding = _make_coding(obs.observation_concept)
    code_obj: dict[str, Any] = {}
    if coding:
        code_obj["coding"] = [coding]
    if obs.observation_source_value:
        code_obj["text"] = obs.observation_source_value
    if code_obj:
        result["code"] = code_obj

    # Effective date
    effective = _date_str(obs.observation_datetime) or _date_str(obs.observation_date)
    if effective:
        result["effectiveDateTime"] = effective

    # Value
    if obs.value_as_number is not None:
        quantity: dict[str, Any] = {"value": _decimal_to_float(obs.value_as_number)}
        if obs.unit_source_value:
            quantity["unit"] = obs.unit_source_value
        result["valueQuantity"] = quantity
    elif obs.value_as_string:
        result["valueString"] = obs.value_as_string
    elif obs.value_as_concept_id and obs.value_as_concept_id != 0:
        val_coding = _make_coding(obs.value_as_concept)
        if val_coding:
            result["valueCodeableConcept"] = {"coding": [val_coding]}

    return result


def _build_medication_statement(drug: DrugExposure, patient_ref: str) -> dict[str, Any]:
    """Convert an OMOP DrugExposure to a FHIR MedicationStatement resource."""
    resource_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": resource_id,
        "status": "completed",
        "subject": {"reference": patient_ref},
    }

    # Medication coding
    coding = _make_coding(drug.drug_concept)
    med_obj: dict[str, Any] = {}
    if coding:
        med_obj["coding"] = [coding]
    if drug.drug_source_value:
        med_obj["text"] = drug.drug_source_value
    if med_obj:
        result["medicationCodeableConcept"] = med_obj

    # Effective period
    period: dict[str, str] = {}
    start = _date_str(drug.drug_exposure_start_datetime) or _date_str(drug.drug_exposure_start_date)
    if start:
        period["start"] = start
    end = _date_str(drug.drug_exposure_end_datetime) or _date_str(drug.drug_exposure_end_date)
    if end:
        period["end"] = end
    if period:
        result["effectivePeriod"] = period

    return result


def _build_procedure(proc: ProcedureOccurrence, patient_ref: str) -> dict[str, Any]:
    """Convert an OMOP ProcedureOccurrence to a FHIR Procedure resource."""
    resource_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "resourceType": "Procedure",
        "id": resource_id,
        "status": "completed",
        "subject": {"reference": patient_ref},
    }

    # Code
    coding = _make_coding(proc.procedure_concept)
    code_obj: dict[str, Any] = {}
    if coding:
        code_obj["coding"] = [coding]
    if proc.procedure_source_value:
        code_obj["text"] = proc.procedure_source_value
    if code_obj:
        result["code"] = code_obj

    # Performed date
    performed = _date_str(proc.procedure_datetime) or _date_str(proc.procedure_date)
    if performed:
        result["performedDateTime"] = performed

    return result


def _build_encounter(visit: VisitOccurrence, patient_ref: str) -> dict[str, Any]:
    """Convert an OMOP VisitOccurrence to a FHIR Encounter resource."""
    resource_id = str(uuid.uuid4())
    result: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": resource_id,
        "status": "finished",
        "subject": {"reference": patient_ref},
    }

    # Class
    coding = _make_coding(visit.visit_concept)
    if coding:
        result["class"] = coding

    # Period
    period: dict[str, str] = {}
    start = _date_str(visit.visit_start_datetime) or _date_str(visit.visit_start_date)
    if start:
        period["start"] = start
    end = _date_str(visit.visit_end_datetime) or _date_str(visit.visit_end_date)
    if end:
        period["end"] = end
    if period:
        result["period"] = period

    return result


# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

def build_fhir_bundle(person: Person) -> dict[str, Any]:
    """
    Build a FHIR R4 Bundle of type "searchset" from a patient's OMOP data.

    Queries all relevant OMOP tables for the given Person in bulk (one query
    per table, with select_related for concept FKs) and converts each row
    to its corresponding FHIR resource.

    Args:
        person: An OMOP Person model instance.

    Returns:
        A JSON-serializable dict representing a FHIR R4 Bundle.
    """
    entries: list[dict[str, Any]] = []

    # Patient resource
    patient_resource = _build_patient(person)
    patient_ref = f"Patient/{patient_resource['id']}"
    entries.append(_make_entry(patient_resource))

    # --- ConditionOccurrence → Condition ---
    conditions = (
        ConditionOccurrence.objects
        .filter(person=person)
        .select_related("condition_concept", "condition_concept__vocabulary")
    )
    for cond in conditions:
        entries.append(_make_entry(_build_condition(cond, patient_ref)))

    # --- Measurement → Observation ---
    measurements = (
        Measurement.objects
        .filter(person=person)
        .select_related(
            "measurement_concept", "measurement_concept__vocabulary",
            "value_as_concept", "value_as_concept__vocabulary",
        )
    )
    for meas in measurements:
        entries.append(_make_entry(_build_measurement_observation(meas, patient_ref)))

    # --- Observation → Observation ---
    observations = (
        Observation.objects
        .filter(person=person)
        .select_related(
            "observation_concept", "observation_concept__vocabulary",
            "value_as_concept", "value_as_concept__vocabulary",
        )
    )
    for obs in observations:
        entries.append(_make_entry(_build_observation(obs, patient_ref)))

    # --- DrugExposure → MedicationStatement ---
    drug_exposures = (
        DrugExposure.objects
        .filter(person=person)
        .select_related("drug_concept", "drug_concept__vocabulary")
    )
    for drug in drug_exposures:
        entries.append(_make_entry(_build_medication_statement(drug, patient_ref)))

    # --- ProcedureOccurrence → Procedure ---
    procedures = (
        ProcedureOccurrence.objects
        .filter(person=person)
        .select_related("procedure_concept", "procedure_concept__vocabulary")
    )
    for proc in procedures:
        entries.append(_make_entry(_build_procedure(proc, patient_ref)))

    # --- VisitOccurrence → Encounter ---
    visits = (
        VisitOccurrence.objects
        .filter(person=person)
        .select_related("visit_concept", "visit_concept__vocabulary")
    )
    for visit in visits:
        entries.append(_make_entry(_build_encounter(visit, patient_ref)))

    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(entries),
        "entry": entries,
    }
