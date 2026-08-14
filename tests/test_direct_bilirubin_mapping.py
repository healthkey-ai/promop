from datetime import date

import pytest

from omop_core.models import Measurement
from omop_core.services.mappings import LAB_FIELD_TO_LOINC
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptClassFactory,
    ConceptFactory,
    DomainFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)


pytestmark = pytest.mark.django_db


def test_direct_bilirubin_round_trips_through_measurement():
    loinc = VocabularyFactory(vocabulary_id='LOINC')
    domain = DomainFactory(domain_id='Measurement')
    concept_class = ConceptClassFactory(concept_class_id='Lab Test')
    ConceptFactory(
        concept_id=3_000_963,
        concept_name='Laboratory test result',
        concept_code='generic-lab',
        vocabulary=loinc,
        domain=domain,
        concept_class=concept_class,
    )
    direct_bilirubin = ConceptFactory(
        concept_id=1_968_007,
        concept_name='Bilirubin.direct [Mass/volume] in Serum or Plasma',
        concept_code='1968-7',
        vocabulary=loinc,
        domain=domain,
        concept_class=concept_class,
    )
    person = PersonFactory()
    PatientRecordFactory(person=person)
    measurement = Measurement.objects.create(
        measurement_id=1_968_007,
        person=person,
        measurement_concept=direct_bilirubin,
        measurement_date=date(2026, 8, 14),
        measurement_type_concept=ConceptFactory(
            concept_id=3_000_964,
            concept_name='Laboratory result',
            concept_code='lab-result-type',
            vocabulary=loinc,
            domain=domain,
            concept_class=concept_class,
        ),
        value_as_number=0.4,
        unit_source_value='mg/dL',
        measurement_source_value='1968-7',
    )
    assert float(measurement.value_as_number) == 0.4

    refreshed = refresh_patient_record(person)
    assert float(refreshed.serum_bilirubin_level_direct) == 0.4


def test_anc_uses_the_standard_absolute_neutrophil_count_loinc():
    assert LAB_FIELD_TO_LOINC['anc_thousand_per_ul'] == (
        '751-8', '10*3/uL', 'Neutrophils [#/volume] in Blood',
    )


def test_rederivation_does_not_preserve_legacy_projection_edits():
    """PatientRecord clinical values have no authority without an OMOP fact."""
    record = PatientRecordFactory(stage='II', user_edited_fields=['stage'])

    refreshed = refresh_patient_record(record.person)

    assert refreshed.stage is None
