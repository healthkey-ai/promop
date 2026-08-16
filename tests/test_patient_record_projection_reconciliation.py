from datetime import date

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import Measurement
from tests.factories import (
    ConceptClassFactory,
    ConceptFactory,
    DomainFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)


pytestmark = pytest.mark.django_db


def _measurement_concepts():
    vocabulary = VocabularyFactory(vocabulary_id="LOINC")
    domain = DomainFactory(domain_id="Measurement")
    concept_class = ConceptClassFactory(concept_class_id="Lab Test")
    lab_type = ConceptFactory(
        concept_id=32856,
        concept_name="Lab result",
        concept_code="Lab result",
        vocabulary=vocabulary,
        domain=domain,
        concept_class=concept_class,
    )
    hemoglobin = ConceptFactory(
        concept_name="Hemoglobin [Mass/volume] in Blood",
        concept_code="718-7",
        vocabulary=vocabulary,
        domain=domain,
        concept_class=concept_class,
    )
    return lab_type, hemoglobin


def test_inventory_does_not_write_or_invent_event_date_and_excludes_unmapped_fields(capsys):
    person = PersonFactory()
    PatientRecordFactory(person=person, hemoglobin_g_dl=12.4, stage="II")

    call_command("reconcile_patient_record_projection")

    output = capsys.readouterr().out
    assert f"RECONCILABLE person_id={person.person_id} field=hemoglobin_g_dl" in output
    assert "field=stage" not in output
    assert Measurement.objects.filter(person=person).count() == 0


def test_apply_requires_explicit_attested_event_date():
    PatientRecordFactory(hemoglobin_g_dl=12.4)

    with pytest.raises(CommandError, match="requires --event-date"):
        call_command("reconcile_patient_record_projection", "--apply")


def test_apply_requires_one_explicit_person_and_field():
    _lab_type, _hemoglobin = _measurement_concepts()
    person = PersonFactory()
    PatientRecordFactory(person=person, hemoglobin_g_dl=12.4)

    with pytest.raises(CommandError, match="requires --person-id"):
        call_command(
            "reconcile_patient_record_projection",
            "--apply", "--event-date", "2024-05-06", "--field", "hemoglobin_g_dl",
        )
    with pytest.raises(CommandError, match="requires --field"):
        call_command(
            "reconcile_patient_record_projection",
            "--apply", "--event-date", "2024-05-06", "--person-id", str(person.person_id),
        )


def test_apply_requires_the_exact_projection_only_candidate():
    _lab_type, hemoglobin = _measurement_concepts()
    person = PersonFactory()
    PatientRecordFactory(person=person, hemoglobin_g_dl=12.4)
    Measurement.objects.create(
        measurement_id=987_654,
        person=person,
        measurement_concept=hemoglobin,
        measurement_date=date(2024, 5, 1),
        measurement_type_concept=ConceptFactory(),
        value_as_number=12.4,
    )

    with pytest.raises(CommandError, match="exactly one projection-only"):
        call_command(
            "reconcile_patient_record_projection",
            "--apply", "--event-date", "2024-05-06", "--person-id", str(person.person_id),
            "--field", "hemoglobin_g_dl",
        )


def test_apply_creates_mapped_measurement_with_explicit_event_date():
    _lab_type, hemoglobin = _measurement_concepts()
    person = PersonFactory()
    PatientRecordFactory(person=person, hemoglobin_g_dl=12.4)

    call_command(
        "reconcile_patient_record_projection",
        "--person-id", str(person.person_id), "--field", "hemoglobin_g_dl",
        "--apply", "--event-date", "2024-05-06",
    )

    measurement = Measurement.objects.get(person=person, measurement_concept=hemoglobin)
    assert measurement.measurement_date == date(2024, 5, 6)
    assert float(measurement.value_as_number) == 12.4
    assert measurement.measurement_source_value == "718-7"
