from datetime import date

import pytest

from omop_core.models import Measurement
from omop_core.services.mappings import LAB_FIELD_TO_LOINC
from omop_core.services.omop_write_service import sync_to_omop
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
    ConceptFactory(
        concept_id=1_968_007,
        concept_name='Bilirubin.direct [Mass/volume] in Serum or Plasma',
        concept_code='1968-7',
        vocabulary=loinc,
        domain=domain,
        concept_class=concept_class,
    )
    person = PersonFactory()
    record = PatientRecordFactory(person=person, serum_bilirubin_level_direct=0.4)

    sync_to_omop(record, {'serum_bilirubin_level_direct'}, today=date(2026, 8, 14))

    measurement = Measurement.objects.get(person=person, measurement_source_value='1968-7')
    assert float(measurement.value_as_number) == 0.4
    assert measurement.unit_source_value == 'mg/dL'

    refreshed = refresh_patient_record(person)
    assert float(refreshed.serum_bilirubin_level_direct) == 0.4
    assert 'serum_bilirubin_level_direct' not in refreshed.user_edited_fields


def test_anc_uses_the_standard_absolute_neutrophil_count_loinc():
    assert LAB_FIELD_TO_LOINC['anc_thousand_per_ul'] == (
        '751-8', '10*3/uL', 'Neutrophils [#/volume] in Blood',
    )
