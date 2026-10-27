"""Tests for date_of_birth_precision on PatientRecord.

Covers:
- Derivation: precision inferred from Person's month/day nullity
- Sync: precision honoured when writing dob back to Person
- Validation: future dates still rejected
- Age: computed correctly from year-only dob
"""
import pytest
from datetime import date

from omop_core.models import PatientRecord, Person
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.omop_write_service import _sync_demographics
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Derivation — precision derived from Person fields
# ---------------------------------------------------------------------------

class TestPrecisionDerived:
    """Refresh should set date_of_birth_precision based on Person month/day."""

    def test_year_only(self):
        person = PersonFactory(year_of_birth=1985, month_of_birth=None, day_of_birth=None)
        PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'year'
        assert pr.date_of_birth == date(1985, 1, 1)

    def test_month_year(self):
        person = PersonFactory(year_of_birth=1985, month_of_birth=6, day_of_birth=None)
        PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'month'
        assert pr.date_of_birth == date(1985, 6, 1)

    def test_full_date(self):
        person = PersonFactory(year_of_birth=1985, month_of_birth=6, day_of_birth=15)
        PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'day'
        assert pr.date_of_birth == date(1985, 6, 15)

    def test_invalid_partial_falls_back_to_year(self):
        """An invalid month/day combination (e.g. Feb 31) falls back to year precision."""
        person = PersonFactory(year_of_birth=1985, month_of_birth=2, day_of_birth=31)
        PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'year'
        assert pr.date_of_birth == date(1985, 1, 1)


# ---------------------------------------------------------------------------
# Sync — precision honoured when writing back to Person
# ---------------------------------------------------------------------------

class TestPrecisionSync:
    """_sync_demographics should null out Person month/day per precision."""

    def test_year_only_syncs(self):
        person = PersonFactory(year_of_birth=1970, month_of_birth=3, day_of_birth=15)
        pr = PatientRecordFactory(person=person)
        pr.date_of_birth = date(1985, 1, 1)
        pr.date_of_birth_precision = 'year'
        pr.save()
        _sync_demographics(person, pr, {'date_of_birth': date(1985, 1, 1), 'date_of_birth_precision': 'year'})
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth is None
        assert person.day_of_birth is None

    def test_month_year_syncs(self):
        person = PersonFactory(year_of_birth=1970, month_of_birth=None, day_of_birth=None)
        pr = PatientRecordFactory(person=person)
        pr.date_of_birth = date(1985, 6, 1)
        pr.date_of_birth_precision = 'month'
        pr.save()
        _sync_demographics(person, pr, {'date_of_birth': date(1985, 6, 1), 'date_of_birth_precision': 'month'})
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth is None

    def test_full_date_syncs(self):
        person = PersonFactory(year_of_birth=1970, month_of_birth=None, day_of_birth=None)
        pr = PatientRecordFactory(person=person)
        pr.date_of_birth = date(1985, 6, 15)
        pr.date_of_birth_precision = 'day'
        pr.save()
        _sync_demographics(person, pr, {'date_of_birth': date(1985, 6, 15), 'date_of_birth_precision': 'day'})
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth == 15

    def test_no_precision_defaults_to_full(self):
        """When precision is absent/None, all Person date fields are written."""
        person = PersonFactory(year_of_birth=1970, month_of_birth=None, day_of_birth=None)
        pr = PatientRecordFactory(person=person)
        pr.date_of_birth = date(1985, 6, 15)
        pr.date_of_birth_precision = None
        pr.save()
        _sync_demographics(person, pr, {'date_of_birth': date(1985, 6, 15)})
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth == 15


# ---------------------------------------------------------------------------
# Age computation
# ---------------------------------------------------------------------------

class TestAgeComputation:
    """patient_age should still be computed correctly regardless of precision."""

    def test_year_only_age(self):
        person = PersonFactory(year_of_birth=1985, month_of_birth=None, day_of_birth=None)
        pr = PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr.refresh_from_db()
        today = date.today()
        expected_age = today.year - 1985 - ((today.month, today.day) < (1, 1))
        assert pr.patient_age == expected_age

    def test_full_date_age(self):
        person = PersonFactory(year_of_birth=1985, month_of_birth=6, day_of_birth=15)
        pr = PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr.refresh_from_db()
        today = date.today()
        expected_age = today.year - 1985 - ((today.month, today.day) < (6, 15))
        assert pr.patient_age == expected_age


# ---------------------------------------------------------------------------
# Serializer validation
# ---------------------------------------------------------------------------

class TestPrecisionValidation:
    """Serializer should validate date_of_birth_precision choices."""

    def test_future_date_still_rejected(self):
        from rest_framework.test import APIRequestFactory
        from patient_portal.api.serializers import PatientRecordSerializer
        future = date(2099, 1, 1)
        serializer = PatientRecordSerializer(data={
            'date_of_birth': str(future),
            'date_of_birth_precision': 'year',
        }, partial=True)
        assert not serializer.is_valid()
        assert 'date_of_birth' in serializer.errors

    def test_invalid_precision_rejected(self):
        from patient_portal.api.serializers import PatientRecordSerializer
        serializer = PatientRecordSerializer(data={
            'date_of_birth_precision': 'century',
        }, partial=True)
        assert not serializer.is_valid()
        assert 'date_of_birth_precision' in serializer.errors

    def test_valid_precisions_accepted(self):
        from patient_portal.api.serializers import PatientRecordSerializer
        for prec in ('day', 'month', 'year'):
            serializer = PatientRecordSerializer(data={
                'date_of_birth_precision': prec,
            }, partial=True)
            assert serializer.is_valid(), f"Precision {prec!r} should be valid: {serializer.errors}"
