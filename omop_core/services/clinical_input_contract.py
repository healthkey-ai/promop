"""Versioned, source-fact-owned CB #5440 inputs on the integration schema.

No modern standalone schema, therapy derivation or curator dependency is imported.
These local questions have explicit reviewed source keys; ULN ratios must not be
mislabelled with INR/seconds LOINCs. Writes retain all earlier/imported facts.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction


CLINICAL_INPUT_CONTRACT_VERSION = 1
CLEAR_VALUE = 'PatientRecord:cleared'
RECIPES = {
    'prothrombin_time_uln': ('cb:pt-uln', 'number', '1'),
    'activated_partial_thromboplastin_time_uln': ('cb:aptt-uln', 'number', '1'),
    'able_to_swallow_tablets_capsules': ('cb:swallow-tablets', 'boolean', None),
    # Same source as existing FHIR SCT date; date remains an input, not a verdict.
    'sct_date': ('mm-sct-date', 'date', None),
}
CLINICAL_INPUT_FIELDS = frozenset(RECIPES)


def descriptors():
    from omop_core.models import Concept
    ready = Concept.objects.filter(pk=0).exists() and Concept.objects.filter(
        pk=32865, vocabulary_id='Type Concept',
    ).exists()
    return {
        field: {
            'kind': 'editable', 'writable': ready, 'target': 'observation',
            'concept_id': 0, 'type_concept_id': 32865, 'source_value': source,
            'value_kind': kind, 'unit': unit, 'unit_concept_id': None,
            'reason': 'Explicit local source-input recipe; requires OMOP concept 0 and Patient self-report type vocabulary.',
        }
        for field, (source, kind, unit) in RECIPES.items()
    }


def _validated_values(changes):
    from omop_core.models import PatientRecord
    if set(changes) - CLINICAL_INPUT_FIELDS:
        raise ValueError('Unsupported clinical input field')
    cleaned = {}
    for field, value in changes.items():
        kind = RECIPES[field][1]
        if kind == 'number' and value is not None:
            if isinstance(value, bool):
                raise ValueError('A ratio is not a boolean')
            try:
                value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError('Expected a finite ratio') from None
            if not value.is_finite():
                raise ValueError('Expected a finite ratio')
        if kind == 'boolean' and value is not None and type(value) is not bool:
            raise ValueError('Swallowing ability must be an explicit boolean or null')
        if kind == 'date' and value is not None and not isinstance(value, (date, str)):
            raise ValueError('Expected a date')
        cleaned[field] = PatientRecord._meta.get_field(field).clean(value, None)
    return cleaned


def _append_inputs(person, values):
    from omop_core.models import Concept, Observation
    from omop_core.services.field_write_service import _patient_reported_type
    from omop_core.services.pk import next_pk
    if not Concept.objects.filter(pk=0).exists():
        raise ValueError('OMOP concept-0 sentinel is not loaded')
    type_id = _patient_reported_type().pk
    for field, value in values.items():
        source, kind, unit = RECIPES[field]
        number, string = None, None
        if value is not None:
            if kind == 'number':
                number = value
            elif kind == 'boolean':
                string = 'true' if value else 'false'
            else:
                string = value.isoformat()
        row = Observation(
            observation_id=next_pk(Observation, 'observation_id'), person=person,
            observation_concept_id=0, observation_type_concept_id=type_id,
            observation_date=date.today(), observation_source_value=source,
            value_as_number=number, value_as_string=string,
            value_source_value=CLEAR_VALUE if value is None else None,
            unit_source_value=unit,
        )
        row._skip_patient_record_refresh = True
        row.save()


def write_clinical_inputs(person, changes):
    """Atomic public host seam; validate ALL inputs before writing ANY fact.

    Caller supplies an authorized Person. Equal-value retries still write source
    facts. History is append-only and clear tombstones mask, never delete, imports.
    """
    from omop_core.models import Person, PatientRecord
    from omop_core.services.patient_record_service import refresh_patient_record
    values = _validated_values(changes)
    with transaction.atomic():
        # Same lock order as the existing field applier: Person then projection.
        Person.objects.select_for_update().get(pk=person.pk)
        list(PatientRecord.objects.select_for_update().filter(person=person))
        if values:
            _append_inputs(person, values)
        record = refresh_patient_record(person)
        # A newer imported fact (e.g. future-dated) must not turn an accepted edit
        # into a different projected answer. Refuse atomically rather than ACK it.
        if any(getattr(record, field) != value for field, value in values.items()):
            raise ValidationError('Clinical input did not round-trip through its OMOP source')
        return record


def values_from_snapshot(snapshot):
    """Latest date/PK for each explicit source; null/invalid is never default yes.

    Invalid latest facts and tombstones suppress older values. SCT dates also
    accept existing imported facts on the same source key, regardless of type.
    """
    result = {}
    for field, (source, kind, _unit) in RECIPES.items():
        rows = snapshot.obs_by_source.get(source, [])
        if not rows:
            continue
        fact = rows[0]
        value = None
        if fact.value_source_value != CLEAR_VALUE:
            try:
                if kind == 'number' and fact.value_as_number is not None:
                    value = _validated_values({field: fact.value_as_number})[field]
                elif kind == 'boolean':
                    raw = (fact.value_as_string or '').strip().lower()
                    if raw in ('true', 'false'):
                        value = raw == 'true'
                elif kind == 'date' and fact.value_as_string:
                    value = date.fromisoformat(fact.value_as_string[:10])
            except (ValueError, ValidationError, InvalidOperation):
                value = None
        result[field] = value
    return result
