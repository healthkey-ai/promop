"""#2290: text longer than its column must not fail the whole refresh."""
from datetime import date

import pytest

from omop_core.models import FieldConceptMapping, PatientRecord
from omop_core.services.patient_record_service import DERIVATION_VERSION, refresh_patient_record
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _refresh_with_mapped_result(field, code, value):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code=code)
    FieldConceptMapping.objects.create(
        field_name=field, concept=concept, omop_table='measurement',
        source_value=code, value_kind='string', status='approved',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2026, 2, 3), value_as_string=value,
    )
    refresh_patient_record(person)
    return PatientRecord.objects.get(person=person)


@pytest.mark.parametrize('value,expected', [
    ('Passed Stone\r\nMAYO MEDICAL LABORATORIES, 3050 SUPERIOR DRIVE', 'Passed Stone'),
    ('Core Bx: T10 Spine\r\n   EVALUATION EPISODES: 1 (Adequacy only', 'Core Bx: T10 Spine'),
    # One line with no answer to pick out: cut to the column, not interpreted.
    ('CORRECTED ON 06/15 AT 0322: PREVIOUSLY REPORTED AS Blood',
     'CORRECTED ON 06/15 AT 0322: PREVIOUSLY REPORTED AS'),
    ('Primary biopsy', 'Primary biopsy'),
])
def test_specimen_type_is_the_answer_line(value, expected):
    record = _refresh_with_mapped_result('test_specimen_type', '31208-2', value)

    assert record.test_specimen_type == expected
    assert record.derivation_version == DERIVATION_VERSION


def test_an_overlong_mapped_value_is_skipped_and_the_record_still_saves(caplog):
    long_text = 'Indeterminate, see the comment below for the details'

    record = _refresh_with_mapped_result('report_interpretation', '69548-6', long_text)

    # The built-in extractor's value stands; the mapping's is not written over it.
    assert record.report_interpretation == long_text[:50]
    assert record.derivation_version == DERIVATION_VERSION
    assert 'field=report_interpretation' in caplog.text
    assert long_text not in caplog.text
