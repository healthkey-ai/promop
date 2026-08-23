"""Entered-in-error rows must not reach the derived read model.

PHR-S FM keeps an erroneous row rather than deleting it, so the only thing that
stops it counting is the flag. The API read layer honoured it; derivation did not
— 15 of its 21 Measurement/Observation queries omitted the filter — so a result a
clinician had marked as entered in error still drove PatientRecord, which is what
trial matching and eligibility screening read.

Found by running the app: correcting a lab left the corrected value in place.
"""
import re
from decimal import Decimal
from pathlib import Path

import pytest

from omop_core.models import Measurement, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory, MeasurementFactory, OrganizationFactory,
    PatientRecordFactory, PersonFactory,
)

pytestmark = pytest.mark.django_db

SERVICE = Path(__file__).resolve().parents[1] / 'omop_core/services/patient_record_service.py'


def _hgb(person, value, date, erroneous=False):
    return MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(
            concept_code='718-7', concept_name='Hemoglobin [Mass/volume] in Blood',
        ),
        measurement_source_value='718-7',
        measurement_date=date,
        value_as_number=value,
        is_erroneous=erroneous,
    )


@pytest.fixture
def patient():
    person = PersonFactory()
    PatientRecordFactory(person=person, organization=OrganizationFactory())
    return person


class TestLabProjection:
    def test_an_erroneous_result_does_not_project(self, patient):
        _hgb(patient, 13.4, '2026-08-21', erroneous=True)
        _hgb(patient, 11.8, '2026-08-21')

        refresh_patient_record(patient)

        assert PatientRecord.objects.get(person=patient).hemoglobin_g_dl == Decimal('11.8')

    def test_a_newer_erroneous_result_does_not_win(self, patient):
        """The failure seen in the app: the corrected value stayed visible."""
        _hgb(patient, 11.8, '2026-08-21')
        _hgb(patient, 12.5, '2026-08-22', erroneous=True)

        refresh_patient_record(patient)

        assert PatientRecord.objects.get(person=patient).hemoglobin_g_dl == Decimal('11.8')

    def test_all_results_erroneous_clears_the_field(self, patient):
        """No usable fact means no value — never a stale one left behind."""
        _hgb(patient, 13.4, '2026-08-21', erroneous=True)

        refresh_patient_record(patient)

        assert PatientRecord.objects.get(person=patient).hemoglobin_g_dl is None

    def test_flagging_the_latest_falls_back_to_the_previous(self, patient):
        _hgb(patient, 11.8, '2026-08-21')
        latest = _hgb(patient, 12.5, '2026-08-22')
        refresh_patient_record(patient)
        assert PatientRecord.objects.get(person=patient).hemoglobin_g_dl == Decimal('12.5')

        latest.is_erroneous = True
        latest.save(update_fields=['is_erroneous'])
        refresh_patient_record(patient)

        assert PatientRecord.objects.get(person=patient).hemoglobin_g_dl == Decimal('11.8')

    def test_the_row_is_retained_not_deleted(self, patient):
        """PHR-S FM keeps the record; only its visibility changes."""
        _hgb(patient, 13.4, '2026-08-21', erroneous=True)
        _hgb(patient, 11.8, '2026-08-21')

        refresh_patient_record(patient)

        assert Measurement.objects.filter(person=patient).count() == 2


def test_every_derivation_query_filters_entered_in_error():
    """Structural guard: a new extractor must not reintroduce the omission.

    Fifteen queries were missing it, across labs, biomarkers, staging, genetics
    and wearables. Nothing failed loudly, which is why it survived.
    """
    lines = SERVICE.read_text().splitlines()
    unfiltered = [
        (i + 1, line.strip()[:70])
        for i, line in enumerate(lines)
        if re.search(r'(Measurement|Observation)\.objects\.filter\(', line)
        and 'is_erroneous' not in '\n'.join(lines[i:i + 5])
    ]
    assert not unfiltered, f'derivation queries missing is_erroneous: {unfiltered}'
