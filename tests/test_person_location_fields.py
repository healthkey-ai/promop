"""Location fields are writable at the persons endpoint.

`city`, `region`, `postal_code`, `country`, `latitude` and `longitude` are
projected from the OMOP `Location` row identified by `Person.location_id` — a
plain IntegerField, not a ForeignKey, so the link is by id and there is nothing to
follow. The `Location` model already existed; only the API surface was missing, so
six fields sat read-only for want of a write path rather than for want of a
concept.

Replaceable rather than fill-if-empty: a patient who moves needs the new address
to win, which is the opposite of the rule that protects a recorded birth date.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import Location, PatientRecord, Person
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import OrganizationFactory, PatientRecordFactory, PersonFactory


def _loc(person):
    """Person.location_id is a plain IntegerField; there is no FK to follow."""
    person.refresh_from_db()
    return Location.objects.get(location_id=person.location_id)

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(django_user_model):
    from patient_portal.models import Identity

    user = Identity.objects.create_user(email='loc-staff@test.com', password='x')
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def person():
    p = PersonFactory()
    PatientRecordFactory(person=p, organization=OrganizationFactory())
    return p


def _patch(client, person, payload):
    return client.patch(
        f'/api/v1/persons/{person.person_id}/', payload, format='json'
    )


class TestWritingLocation:
    def test_creates_a_location_row_for_a_person_that_has_none(self):
        """An address that arrives after registration has nothing to update."""
        p = PersonFactory()
        assert p.location_id is None

    def test_first_write_creates_and_links_the_location(self, staff_client, person):
        resp = _patch(staff_client, person, {'city': 'Boston', 'region': 'MA'})

        assert resp.status_code == 200
        person.refresh_from_db()
        assert person.location_id is not None
        assert _loc(person).city == 'Boston'
        assert _loc(person).state == 'MA'

    def test_a_later_write_updates_the_same_row(self, staff_client, person):
        _patch(staff_client, person, {'city': 'Boston'})
        person.refresh_from_db()
        first_id = person.location_id

        _patch(staff_client, person, {'city': 'Cambridge'})

        person.refresh_from_db()
        assert person.location_id == first_id      # not a second row
        assert _loc(person).city == 'Cambridge'

    def test_an_existing_value_is_replaced_not_preserved(self, staff_client, person):
        """Unlike the fill-if-empty demographics, a corrected address must win."""
        _patch(staff_client, person, {'country': 'USA'})

        _patch(staff_client, person, {'country': 'Canada'})

        person.refresh_from_db()
        assert _loc(person).country == 'Canada'

    def test_the_projection_reflects_the_write_after_derivation(self, staff_client, person):
        _patch(staff_client, person, {
            'city': 'Boston', 'region': 'MA', 'postal_code': '02115',
            'country': 'USA', 'latitude': 42.3601, 'longitude': -71.0589,
        })

        refresh_patient_record(person)

        pr = PatientRecord.objects.get(person=person)
        assert pr.city == 'Boston'
        assert pr.region == 'MA'
        assert pr.postal_code == '02115'
        assert pr.country == 'USA'
        assert pr.latitude == pytest.approx(42.3601)
        assert pr.longitude == pytest.approx(-71.0589)

    def test_a_later_write_rederives_without_being_asked(self, staff_client, person):
        """The endpoint must derive, not merely store.

        The first address write creates the Location row, and creating it puts
        'location_id' on Person — which is what used to trigger the refresh. A
        later write only updates the Location, so nothing landed on Person and
        the refresh never fired: the row said one city and the projection kept
        another, under a 200 reporting no change.

        The existing projection test does not catch this because it calls
        refresh_patient_record itself, and because a single write is always the
        creating one.
        """
        _patch(staff_client, person, {'city': 'Boston'})

        _patch(staff_client, person, {'city': 'Cambridge'})

        # No manual refresh: the point is that the endpoint did it.
        pr = PatientRecord.objects.get(person=person)
        assert pr.city == 'Cambridge'

    def test_a_later_write_reports_what_it_changed(self, staff_client, person):
        # `updated_fields: []` over a write that did change the database is worse
        # than an error -- the caller has no way to know it needs to re-read.
        _patch(staff_client, person, {'city': 'Boston'})

        resp = _patch(staff_client, person, {'city': 'Cambridge', 'country': 'USA'})

        assert resp.status_code == 200
        assert set(resp.json()['updated_fields']) >= {'city', 'country'}

    def test_a_write_that_changes_nothing_reports_nothing(self, staff_client, person):
        # The converse still has to hold, or the caller learns nothing from the
        # field list.
        _patch(staff_client, person, {'city': 'Boston'})

        resp = _patch(staff_client, person, {'city': 'Boston'})

        assert resp.json()['updated_fields'] == []

    def test_a_field_not_sent_is_left_alone(self, staff_client, person):
        _patch(staff_client, person, {'city': 'Boston', 'country': 'USA'})

        _patch(staff_client, person, {'city': 'Cambridge'})

        person.refresh_from_db()
        assert _loc(person).country == 'USA'

    def test_null_clears_a_value(self, staff_client, person):
        _patch(staff_client, person, {'city': 'Boston'})

        _patch(staff_client, person, {'city': None})

        person.refresh_from_db()
        assert _loc(person).city is None


class TestValidation:
    def test_a_region_longer_than_the_cdm_allows_is_refused(self, staff_client, person):
        """Location.state is CharField(2). Truncating 'Massachusetts' to 'Ma' would
        be worse than refusing it."""
        resp = _patch(staff_client, person, {'region': 'Massachusetts'})

        assert resp.status_code == 400
        assert 'at most 2 characters' in resp.data['detail']
        person.refresh_from_db()
        assert person.location_id is None

    def test_an_over_long_postal_code_is_refused(self, staff_client, person):
        resp = _patch(staff_client, person, {'postal_code': '0' * 20})

        assert resp.status_code == 400

    def test_a_non_numeric_latitude_is_refused(self, staff_client, person):
        resp = _patch(staff_client, person, {'latitude': 'north'})

        assert resp.status_code == 400
        assert 'must be a number' in resp.data['detail']

    @pytest.mark.parametrize('field,value', [
        ('latitude', 91), ('latitude', -91),
        ('longitude', 181), ('longitude', -181),
    ])
    def test_an_out_of_range_coordinate_is_refused(self, staff_client, person, field, value):
        resp = _patch(staff_client, person, {field: value})

        assert resp.status_code == 400
        assert 'must be between' in resp.data['detail']

    def test_a_boundary_coordinate_is_accepted(self, staff_client, person):
        resp = _patch(staff_client, person, {'latitude': 90, 'longitude': -180})

        assert resp.status_code == 200
        person.refresh_from_db()
        assert _loc(person).latitude == Decimal('90')

    def test_cannot_clear_only_one_coordinate(self, staff_client, person):
        _patch(staff_client, person, {'latitude': 42.36, 'longitude': -71.06})

        resp = _patch(staff_client, person, {'latitude': None})

        assert resp.status_code == 400
        assert 'requires both coordinates or neither' in resp.data['detail']
        person.refresh_from_db()
        assert _loc(person).latitude == Decimal('42.36')
        assert _loc(person).longitude == Decimal('-71.06')

    def test_a_rejected_write_leaves_person_fields_untouched(self, staff_client, person):
        """Validation runs before anything is saved."""
        resp = _patch(staff_client, person, {'email': 'a@b.com', 'region': 'Massachusetts'})

        assert resp.status_code == 400
        person.refresh_from_db()
        assert person.email is None


class TestDescriptor:
    def test_the_six_fields_report_as_writable_profile(self):
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        d = build_writable_field_descriptor()
        for field in ('city', 'region', 'postal_code', 'country',
                      'latitude', 'longitude'):
            assert d[field]['kind'] == 'profile', field
            assert d[field]['writable'] is True, field
            assert d[field]['person_field'].startswith('Location.'), field

    def test_no_field_is_still_grouped_as_location(self):
        """The group existed only because these had no write path."""
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        groups = {e.get('group') for e in build_writable_field_descriptor().values()}
        assert 'location' not in groups
