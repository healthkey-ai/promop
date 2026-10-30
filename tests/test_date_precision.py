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


@pytest.mark.parametrize(('prefix', 'shared'), [
    ('/api/v1/patient-records/', False), ('/api/patient-info/', False),
    ('/api/v1/patient-records/', True),
])
@pytest.mark.parametrize(('precision', 'month', 'day'), [('year', None, None), ('month', 6, None)])
def test_date_only_patch_keeps_existing_precision(prefix, shared, precision, month, day):
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    person = PersonFactory(year_of_birth=1985, month_of_birth=month, day_of_birth=day)
    record = PatientRecordFactory(person=person, date_of_birth=date(1985, month or 1, 1),
                                  date_of_birth_precision=precision)
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='dob-precision@example.test', is_staff=True))
    headers = {'HTTP_X_PATIENT_FORM': '1'} if shared else {}
    response = client.patch(f'{prefix}{person.pk}/',
        {'date_of_birth': f'1986-{month or 1:02d}-01'}, format='json', **headers)
    assert response.status_code == 200, response.data
    person.refresh_from_db()
    assert (person.year_of_birth, person.month_of_birth, person.day_of_birth) == (1986, month, day)
    record = refresh_patient_record(person)
    assert record.date_of_birth_precision == precision


@pytest.mark.parametrize('prefix', ['/api/v1/patient-records/', '/api/patient-info/'])
@pytest.mark.parametrize('changes', [
    {'date_of_birth': '1986-03-15'},
    {'date_of_birth_precision': 'month'},
    {'date_of_birth': '1986-03-15', 'date_of_birth_precision': 'day'},
])
def test_native_dob_patch_keeps_record_person_and_response_consistent(prefix, changes):
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    person = PersonFactory(year_of_birth=1985, month_of_birth=None, day_of_birth=None)
    record = PatientRecordFactory(person=person, date_of_birth=date(1985, 1, 1), date_of_birth_precision='year')
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='native-dob-pair@example.test', is_staff=True))
    response = client.patch(f'{prefix}{person.pk}/', changes, format='json')
    assert response.status_code == 200, response.data
    precision = changes.get('date_of_birth_precision', 'year')
    expected = {'year': date(1986, 1, 1), 'month': date(1985, 1, 1), 'day': date(1986, 3, 15)}[precision]
    record.refresh_from_db(); person.refresh_from_db()
    assert record.date_of_birth == expected
    assert record.date_of_birth_precision == precision
    assert response.data['date_of_birth'] == expected.isoformat()
    assert response.data['date_of_birth_precision'] == precision
    assert (person.year_of_birth, person.month_of_birth, person.day_of_birth) == (
        expected.year, expected.month if precision != 'year' else None,
        expected.day if precision == 'day' else None)


@pytest.mark.parametrize('changes', [
    {'date_of_birth_precision': 'year', 'date_of_birth': '1985-01-01'},
    {'date_of_birth': '1985-01-01', 'date_of_birth_precision': 'year'},
    {'date_of_birth_precision': 'year'},
])
def test_shared_precision_patch_is_order_independent_and_projects_to_person(changes):
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    person = PersonFactory(year_of_birth=1985, month_of_birth=6, day_of_birth=15)
    record = PatientRecordFactory(person=person, date_of_birth=date(1985, 6, 15),
                                  date_of_birth_precision='day')
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='dob-shared@example.test', is_staff=True))
    response = client.patch(f'/api/v1/patient-records/{person.pk}/', changes,
                            format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    person.refresh_from_db(); record.refresh_from_db()
    assert (person.year_of_birth, person.month_of_birth, person.day_of_birth) == (1985, None, None)
    assert record.date_of_birth_precision == 'year'
    assert record.date_of_birth == date(1985, 1, 1)


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
