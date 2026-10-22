"""Safe recovery of legacy clinical values stranded in ``PatientRecord``.

``PatientRecord`` is a derived read model.  Before #484, however, selected
numeric fields could be written to it directly and reverse-synced into OMOP
using the current date.  That date is not the clinical event time and must
never be recreated.  This module therefore inventories candidates by default
and permits a write only when an operator explicitly attests an event date.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from omop_core.models import Concept, Measurement, PatientRecord
from omop_core.services.mappings import CONCEPT_LAB_TYPE, LAB_FIELD_TO_LOINC
from omop_core.services.pk import next_pk


# Only fields which had a numeric LOINC reverse mapping and are still actual
# PatientRecord fields can be reconstructed as Measurements.  This is a
# mapping-inventory catalog, not a declaration that every PatientRecord field
# is derived: unmapped projection-owned fields remain writable and are outside
# this command entirely.
RECONCILABLE_FIELDS = {
    field: mapping
    for field, mapping in LAB_FIELD_TO_LOINC.items()
    if field in {model_field.name for model_field in PatientRecord._meta.fields}
}


@dataclass(frozen=True)
class Candidate:
    person_id: int
    field: str
    value: Decimal
    loinc_code: str
    unit: str


def projection_only_candidates(records=None):
    """Yield numeric projection values with no OMOP Measurement for that LOINC.

    The comparison is intentionally about the presence of a fact, not equality
    with the current projected value: the projection may be stale, whereas an
    existing OMOP fact remains authoritative.
    """
    if records is None:
        records = PatientRecord.objects.all()

    records = records.select_related("person")
    codes = {mapping[0] for mapping in RECONCILABLE_FIELDS.values()}
    for record in records.iterator():
        existing_codes = set(
            Measurement.objects.filter(person_id=record.person_id)
            .filter(measurement_concept__concept_code__in=codes)
            .values_list("measurement_concept__concept_code", flat=True)
        )
        for field, (loinc_code, unit, _display) in RECONCILABLE_FIELDS.items():
            value = getattr(record, field)
            if value is None or loinc_code in existing_codes:
                continue
            yield Candidate(
                person_id=record.person_id,
                field=field,
                value=Decimal(str(value)),
                loinc_code=loinc_code,
                unit=unit,
            )


def migrate_candidates(candidates, *, event_date: date) -> tuple[int, list[Candidate]]:
    """Create Measurements for candidates using an operator-attested date.

    Missing LOINC or Lab-type concepts are returned rather than replaced with a
    generic concept.  That keeps the future derivation semantically exact.
    """
    type_concept = Concept.objects.filter(concept_id=CONCEPT_LAB_TYPE).first()
    if type_concept is None:
        return 0, list(candidates)

    concepts = {
        concept.concept_code: concept
        for concept in Concept.objects.filter(
            vocabulary_id="LOINC",
            concept_code__in={candidate.loinc_code for candidate in candidates},
        )
    }
    migrated = 0
    skipped = []
    for candidate in candidates:
        concept = concepts.get(candidate.loinc_code)
        if concept is None:
            skipped.append(candidate)
            continue
        # A concurrent or repeated run must not create a duplicate fact.
        if Measurement.objects.filter(
            person_id=candidate.person_id,
            measurement_concept=concept,
        ).exists():
            continue
        Measurement.objects.create(
            measurement_id=next_pk(Measurement, "measurement_id"),
            person_id=candidate.person_id,
            measurement_concept=concept,
            measurement_date=event_date,
            measurement_type_concept=type_concept,
            value_as_number=candidate.value,
            measurement_source_value=candidate.loinc_code,
            unit_source_value=candidate.unit,
        )
        migrated += 1
    return migrated, skipped
