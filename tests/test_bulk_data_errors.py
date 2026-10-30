"""A value the database refuses is a 400 that says why, never a bare 500 (#2101).

#2101: a bulk measurement create raised ``DataError: numeric field overflow``
after validation, and the caller got a 500 with no detail, so it could not tell
which input to fix and retried the same payload 14 times. These tests make the
write itself run a real overflowing statement, so they do not depend on what
server-side code produced the value.
"""
from decimal import Decimal

import pytest
from django.db import connection
from django.db.models.query import QuerySet
from rest_framework.test import APIClient

from omop_core.models import Measurement
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory

OVERFLOW = 'numeric field overflow'


def _overflow(*args, **kwargs):
    with connection.cursor() as cur:
        cur.execute('SELECT 250000000000::numeric(15, 5)')


@pytest.fixture
def client(db):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='bulk-data-errors@example.org', password='test', is_staff=True))
    return client


@pytest.fixture
def rows(db):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code='777-3', concept_name='Platelets')
    type_concept = ConceptFactory(concept_code='EHR-2101', concept_name='EHR')

    def row(value='250', source_value='PLT'):
        return {
            'person': person.person_id, 'measurement_concept': concept.concept_id,
            'measurement_date': '2026-10-01',
            'measurement_type_concept': type_concept.concept_id,
            'value_as_number': value, 'unit_source_value': '10*3/uL',
            'measurement_source_value': source_value,
        }
    row.person = person
    return row


@pytest.mark.parametrize('upsert', ['true', 'false'])
def test_bulk_create_data_error_is_400_with_the_error(client, rows, monkeypatch, upsert):
    monkeypatch.setattr(QuerySet, 'bulk_create', _overflow)

    resp = client.post(f'/api/v1/measurements/?skip_refresh=true&upsert={upsert}',
                       [rows(), rows('251')], format='json')

    assert resp.status_code == 400, resp.content
    assert OVERFLOW in resp.json()['error']
    assert Measurement.objects.filter(person=rows.person).count() == 0


def test_single_row_data_error_is_400(client, rows, monkeypatch):
    monkeypatch.setattr(QuerySet, 'bulk_create', _overflow)

    resp = client.post('/api/v1/measurements/?skip_refresh=true', rows(), format='json')

    assert resp.status_code == 400, resp.content
    assert OVERFLOW in resp.json()['error']


def test_bulk_update_data_error_is_400(client, rows, monkeypatch):
    stored = MeasurementFactory(person=rows.person, value_as_number=Decimal('1'))
    monkeypatch.setattr(QuerySet, 'bulk_update', _overflow)

    resp = client.patch('/api/v1/measurements/bulk_update/?skip_refresh=true',
                        [{'measurement_id': stored.pk, 'value_as_number': '2'}],
                        format='json')

    assert resp.status_code == 400, resp.content
    assert OVERFLOW in resp.json()['error']
    stored.refresh_from_db()
    assert stored.value_as_number == Decimal('1')


def test_a_sent_value_too_large_for_the_column_is_a_row_error(client, rows):
    # 2.5e11 does not fit Decimal(15, 5). Validation catches it before the
    # database, and names the row, so the caller knows what to fix.
    resp = client.post('/api/v1/measurements/?skip_refresh=true&upsert=false',
                       [rows(), rows('250000000000', 'PLT2')], format='json')

    assert resp.status_code == 400, resp.content
    errors = resp.json()
    assert errors[0] == {}
    assert 'value_as_number' in errors[1]
    assert Measurement.objects.filter(person=rows.person).count() == 0
