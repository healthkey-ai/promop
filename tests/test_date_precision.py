"""Tests for date precision on PatientRecord.

Covers:
- DOB: precision inferred from Person's month/day nullity
- Sync: precision honoured when writing dob back to Person
- Validation: future dates still rejected
- Age: computed correctly from year-only dob
- Clinical dates: diagnosis_date, sct_date, dlbcl_transformation_date precision
- FHIR: partial birthDate parsing (YYYY, YYYY-MM)
"""
import pytest
from datetime import date

from omop_core.models import PatientRecord, Person
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.omop_write_service import _sync_demographics
from tests.factories import (
    ConditionOccurrenceFactory, ConceptFactory, PatientRecordFactory, PersonFactory,
)

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
# Profile projection — the actual PATCH path via _project_profile_fields
# ---------------------------------------------------------------------------

class TestProfileProjection:
    """_project_profile_fields must honor precision when writing to Person."""

    def test_year_precision_nulls_month_day_on_person(self):
        from patient_portal.api.views import _project_profile_fields
        person = PersonFactory(year_of_birth=1970, month_of_birth=3, day_of_birth=15)
        PatientRecordFactory(person=person)
        _project_profile_fields(
            person,
            {'date_of_birth', 'date_of_birth_precision'},
            {'date_of_birth': date(1985, 1, 1), 'date_of_birth_precision': 'year'},
        )
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth is None
        assert person.day_of_birth is None

    def test_month_precision_nulls_day_on_person(self):
        from patient_portal.api.views import _project_profile_fields
        person = PersonFactory(year_of_birth=1970, month_of_birth=3, day_of_birth=15)
        PatientRecordFactory(person=person)
        _project_profile_fields(
            person,
            {'date_of_birth', 'date_of_birth_precision'},
            {'date_of_birth': date(1985, 6, 1), 'date_of_birth_precision': 'month'},
        )
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth is None

    def test_day_precision_writes_all(self):
        from patient_portal.api.views import _project_profile_fields
        person = PersonFactory(year_of_birth=1970, month_of_birth=None, day_of_birth=None)
        PatientRecordFactory(person=person)
        _project_profile_fields(
            person,
            {'date_of_birth', 'date_of_birth_precision'},
            {'date_of_birth': date(1985, 6, 15), 'date_of_birth_precision': 'day'},
        )
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth == 15

    def test_precision_only_patch_uses_existing_dob(self):
        """Changing only precision (no date_of_birth in payload) re-writes Person."""
        from patient_portal.api.views import _project_profile_fields
        person = PersonFactory(year_of_birth=1985, month_of_birth=6, day_of_birth=15)
        PatientRecordFactory(person=person)
        _project_profile_fields(
            person,
            {'date_of_birth_precision'},
            {'date_of_birth_precision': 'year'},
        )
        person.refresh_from_db()
        assert person.year_of_birth == 1985
        assert person.month_of_birth is None
        assert person.day_of_birth is None

    def test_round_trip_year_survives_refresh(self):
        """Year-only through _project_profile_fields → refresh → precision stays 'year'."""
        from patient_portal.api.views import _project_profile_fields
        person = PersonFactory(year_of_birth=1970, month_of_birth=3, day_of_birth=15)
        pr = PatientRecordFactory(person=person)
        _project_profile_fields(
            person,
            {'date_of_birth', 'date_of_birth_precision'},
            {'date_of_birth': date(1985, 1, 1), 'date_of_birth_precision': 'year'},
        )
        # Refresh should derive 'year' from Person's null month/day.
        refresh_patient_record(person)
        pr.refresh_from_db()
        assert pr.date_of_birth_precision == 'year'
        assert pr.date_of_birth == date(1985, 1, 1)


# ---------------------------------------------------------------------------
# Serializer validation
# ---------------------------------------------------------------------------

class TestPrecisionValidation:
    """Serializer should validate date_of_birth_precision choices."""

    def test_future_date_still_rejected(self):
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


# ---------------------------------------------------------------------------
# Clinical date precision — diagnosis, sct, transformation
# ---------------------------------------------------------------------------

class TestDiagnosisDatePrecision:
    """diagnosis_date_precision derived from ConditionOccurrence (always 'day')."""

    def test_precision_day_from_condition(self):
        """When diagnosis_date is derived from a ConditionOccurrence, precision is 'day'."""
        person = PersonFactory()
        PatientRecordFactory(person=person, disease='follicular lymphoma')
        concept = ConceptFactory(concept_name='Follicular lymphoma', standard_concept='S')
        ConditionOccurrenceFactory(
            person=person,
            condition_concept=concept,
            condition_start_date=date(2020, 3, 15),
        )
        refresh_patient_record(person)
        pr = PatientRecord.objects.get(person=person)
        assert pr.diagnosis_date == date(2020, 3, 15)
        assert pr.diagnosis_date_precision == 'day'

    def test_precision_null_when_no_condition(self):
        """Without a ConditionOccurrence, no diagnosis_date or precision."""
        person = PersonFactory()
        PatientRecordFactory(person=person)
        refresh_patient_record(person)
        pr = PatientRecord.objects.get(person=person)
        assert pr.diagnosis_date is None
        assert pr.diagnosis_date_precision is None

    def test_user_edit_year_precision_survives_refresh(self):
        """A user-edited year-only diagnosis_date survives refresh when no OMOP condition exists."""
        person = PersonFactory()
        pr = PatientRecordFactory(person=person)
        pr.diagnosis_date = date(2019, 1, 1)
        pr.diagnosis_date_precision = 'year'
        pr.user_edited_fields = ['diagnosis_date', 'diagnosis_date_precision']
        pr.save()
        refresh_patient_record(person)
        pr.refresh_from_db()
        assert pr.diagnosis_date == date(2019, 1, 1)
        assert pr.diagnosis_date_precision == 'year'


class TestSctDatePrecision:
    """sct_date_precision stored on PatientRecord."""

    def test_precision_stored_on_model(self):
        person = PersonFactory()
        pr = PatientRecordFactory(person=person)
        pr.sct_date = date(2022, 5, 1)
        pr.sct_date_precision = 'month'
        pr.save()
        pr.refresh_from_db()
        assert pr.sct_date == date(2022, 5, 1)
        assert pr.sct_date_precision == 'month'

    def test_invalid_precision_rejected(self):
        from patient_portal.api.serializers import PatientRecordSerializer
        serializer = PatientRecordSerializer(data={
            'sct_date_precision': 'century',
        }, partial=True)
        assert not serializer.is_valid()
        assert 'sct_date_precision' in serializer.errors

    def test_valid_precisions_accepted(self):
        from patient_portal.api.serializers import PatientRecordSerializer
        for prec in ('day', 'month', 'year'):
            serializer = PatientRecordSerializer(data={
                'sct_date_precision': prec,
            }, partial=True)
            assert serializer.is_valid(), f"sct_date_precision {prec!r}: {serializer.errors}"


class TestDlbclTransformationDatePrecision:
    """dlbcl_transformation_date_precision stored on PatientRecord."""

    def test_precision_stored_on_model(self):
        person = PersonFactory()
        pr = PatientRecordFactory(person=person)
        pr.dlbcl_transformation_date = date(2021, 1, 1)
        pr.dlbcl_transformation_date_precision = 'year'
        pr.save()
        pr.refresh_from_db()
        assert pr.dlbcl_transformation_date == date(2021, 1, 1)
        assert pr.dlbcl_transformation_date_precision == 'year'


# ---------------------------------------------------------------------------
# FHIR partial birthDate parsing
# ---------------------------------------------------------------------------

class TestFhirPartialBirthDate:
    """upload_fhir_bundle should handle YYYY and YYYY-MM birthDate."""

    @staticmethod
    def _import_bundle(client, birth_date_str, family='PartialDob'):
        """Upload a minimal FHIR bundle with the given birthDate string."""
        import io
        import json

        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [{
                'resource': {
                    'resourceType': 'Patient',
                    'id': 'test-partial-dob',
                    'name': [{'family': family, 'given': ['Test']}],
                    'gender': 'female',
                    'birthDate': birth_date_str,
                },
            }],
        }
        fhir_file = io.BytesIO(json.dumps(bundle).encode('utf-8'))
        fhir_file.name = 'test_bundle.json'
        return client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

    def test_year_only_birthdate(self):
        """FHIR birthDate '1985' → Person has year only, precision='year' after refresh."""
        from patient_portal.models import Identity
        from rest_framework.test import APIClient

        user = Identity.objects.create_superuser(email='fhir_y@test.com', password='test')
        client = APIClient()
        client.force_authenticate(user=user)

        resp = self._import_bundle(client, '1985', family='PartialDobY')
        assert resp.status_code in (200, 201), resp.content
        person = Person.objects.get(family_name='PartialDobY')
        assert person.year_of_birth == 1985
        assert person.month_of_birth is None
        assert person.day_of_birth is None
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'year'

    def test_year_month_birthdate(self):
        """FHIR birthDate '1985-06' → Person has year+month, precision='month'."""
        from patient_portal.models import Identity
        from rest_framework.test import APIClient

        user = Identity.objects.create_superuser(email='fhir_ym@test.com', password='test')
        client = APIClient()
        client.force_authenticate(user=user)

        resp = self._import_bundle(client, '1985-06', family='PartialDobYM')
        assert resp.status_code in (200, 201), resp.content
        person = Person.objects.get(family_name='PartialDobYM')
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth is None
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'month'

    def test_full_birthdate_unchanged(self):
        """FHIR birthDate '1985-06-15' still works as before."""
        from patient_portal.models import Identity
        from rest_framework.test import APIClient

        user = Identity.objects.create_superuser(email='fhir_full@test.com', password='test')
        client = APIClient()
        client.force_authenticate(user=user)

        resp = self._import_bundle(client, '1985-06-15', family='PartialDobF')
        assert resp.status_code in (200, 201), resp.content
        person = Person.objects.get(family_name='PartialDobF')
        assert person.year_of_birth == 1985
        assert person.month_of_birth == 6
        assert person.day_of_birth == 15
        pr = PatientRecord.objects.get(person=person)
        assert pr.date_of_birth_precision == 'day'
