"""Cross-feature contracts for the dev -> cb-v2 backmerge (#2129).

Source preservation is the clinical-unit-policy contract, including during
imports and mapping approvals; normalized values are an additive read model.
"""
from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.api.lines import lines_of_therapy
from omop_core.api.serializers import TherapyLineDrugSerializer
from omop_core.mapping.code_resolution import repoint_clinical_rows
from omop_core.models import CanonicalUnitPreference, Measurement, SourceCodeConceptMapping
from omop_core.services.canonical_units import measurement_normalized
from omop_core.services.episode_service import author_therapy_line
from omop_core.services.patient_record_service import refresh_patient_record
from omop_oncology.models import TherapyLineExposure
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, ConditionOccurrenceFactory, MeasurementFactory,
    OrganizationFactory, PatientRecordFactory, PersonFactory, VocabularyFactory,
)
from tests.test_episode_service import _seed_episode_writer_concepts

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='backmerge@example.test', is_staff=True))
    return client


def unit(code):
    return ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='UCUM'), concept_code=code)


def mapping(code, concept, target_unit=None, **kwargs):
    return SourceCodeConceptMapping.objects.create(
        source_code=code, target_concept=concept, omop_table='measurement',
        destination_unit_concept=unit(target_unit) if target_unit else None,
        status='approved', **kwargs)


@pytest.mark.parametrize(('name', 'value', 'source_unit', 'destination'), [
    ('Albumin [Mass/volume]', '40', 'g/L', 'g/dL'),
    ('Platelets [#/volume]', '150', '10*3/uL', None),
    ('Analyte [Moles/volume]', '100', 'nmol/L', None),
])
def test_bulk_import_preserves_source_numbers_units_ranges_and_retry_identity(
        client, name, value, source_unit, destination):
    record = PatientRecordFactory()
    concept = ConceptFactory(concept_name=name, concept_code='RAW-2129')
    mapping('RAW-2129', concept, destination)
    payload = [{
        'person': record.person_id, 'measurement_concept': concept.pk,
        'measurement_type_concept': 32817, 'measurement_date': '2024-01-01',
        'measurement_source_value': 'RAW-2129', 'value_as_number': value,
        'unit_source_value': source_unit, 'range_low': '1', 'range_high': '200',
    }]
    url = '/api/v1/measurements/?skip_refresh=true'
    first = client.post(url, payload, format='json')
    assert first.status_code == 201, first.data
    row = Measurement.objects.get(pk=first.data['ids'][0])
    assert (row.value_as_number, row.unit_source_value, row.range_low, row.range_high) == (
        Decimal(value), source_unit, Decimal('1'), Decimal('200'))
    second = client.post(url, payload, format='json')
    assert second.status_code == 201, second.data
    assert second.data['ids'] == first.data['ids']
    assert second.data['created'] == 0
    assert Measurement.objects.filter(person=record.person).count() == 1


def test_mapping_approval_and_retarget_leave_source_and_normalized_read_model_intact():
    person = PersonFactory()
    old = ConceptFactory(concept_code='RAW-OLD')
    target = ConceptFactory(concept_code='RAW-NEW', concept_name='Albumin [Mass/volume]')
    next_target = ConceptFactory(concept_code='RAW-NEXT')
    entry = mapping('ALB-2129', target, 'g/dL')
    row = MeasurementFactory(person=person, measurement_concept=old,
        measurement_source_value='ALB-2129', value_as_number=40, unit_source_value='g/L')
    result = repoint_clinical_rows(mapping=entry, old_concept_id=old.pk, new_concept_id=target.pk)
    assert result['rows_updated'] == 1
    row.refresh_from_db()
    assert row.measurement_concept_id == target.pk
    assert row.value_as_number == 40
    pref = CanonicalUnitPreference.objects.create(concept=target, property='MCnc', unit='g/dL')
    assert measurement_normalized(row, {target.pk: pref})['value'] == 4
    entry.target_concept = next_target
    entry.destination_unit_concept = unit('mg/L')
    repoint_clinical_rows(mapping=entry, old_concept_id=target.pk, new_concept_id=next_target.pk)
    row.refresh_from_db()
    assert (row.value_as_number, row.unit_source_value, row.unit_concept_id) == (40, 'g/L', None)


def test_large_repoint_preserves_every_source_row_without_mutating_its_selection():
    person = PersonFactory()
    old = ConceptFactory(concept_code='LARGE-OLD')
    target = ConceptFactory(concept_code='LARGE-NEW')
    entry = mapping('LARGE-2129', target, 'g/dL')
    Measurement.objects.bulk_create([
        Measurement(measurement_id=800000 + i, person=person,
            measurement_concept=old, measurement_type_concept_id=32817,
            measurement_date=date(2024, 1, 1), measurement_datetime=None,
            measurement_source_value='LARGE-2129', value_as_number=40,
            value_as_string=str(i), unit_source_value='g/L')
        for i in range(1001)
    ])
    result = repoint_clinical_rows(mapping=entry, old_concept_id=old.pk, new_concept_id=target.pk)
    assert result['rows_updated'] == 1001
    rows = Measurement.objects.filter(person=person)
    assert rows.count() == 1001
    assert rows.filter(measurement_concept=target, value_as_number=40, unit_source_value='g/L').count() == 1001


def test_repoint_of_large_count_does_not_silently_roll_back():
    person = PersonFactory()
    old = ConceptFactory(concept_code='COUNT-OLD')
    target = ConceptFactory(concept_code='COUNT-NEW', concept_name='Platelets [#/volume]')
    entry = mapping('PLT-2129', target)
    row = MeasurementFactory(person=person, measurement_concept=old,
        measurement_source_value='PLT-2129', value_as_number=150, unit_source_value='10*3/uL')
    repoint_clinical_rows(mapping=entry, old_concept_id=old.pk, new_concept_id=target.pk)
    row.refresh_from_db()
    assert row.measurement_concept_id == target.pk
    assert row.value_as_number == 150


def test_bulk_source_code_collision_cannot_apply_another_hospitals_unit(client):
    record = PatientRecordFactory()
    concept = ConceptFactory(concept_code='OUR-LAB')
    other = ConceptFactory(concept_code='THEIR-LAB')
    mapping('COLLISION', other, 'g/dL', source_vocabulary_id='EPIC', organization=OrganizationFactory())
    payload = [{
        'person': record.person_id, 'measurement_concept': concept.pk,
        'measurement_type_concept': 32817, 'measurement_date': '2024-01-01',
        'measurement_source_value': 'COLLISION', 'value_as_number': '40',
        'unit_source_value': 'g/L',
    }]
    response = client.post('/api/v1/measurements/?skip_refresh=true', payload, format='json')
    assert response.status_code == 201, response.data
    row = Measurement.objects.get(pk=response.data['ids'][0])
    assert (row.measurement_concept_id, row.value_as_number, row.unit_source_value, row.unit_concept_id) == (
        concept.pk, 40, 'g/L', None)


def therapy_patient():
    _seed_episode_writer_concepts()
    record = PatientRecordFactory(disease='Follicular Lymphoma')
    ConditionOccurrenceFactory(person=record.person, condition_concept=ConceptFactory(
        concept_name='Follicular lymphoma', concept_code='FL-2129'))
    drug = ConceptFactory(concept_name='Rituximab', concept_code='RITU-2129')
    return record.person, drug


@pytest.mark.parametrize('submitted', [True, None, False])
def test_noop_therapy_edit_preserves_maintenance_through_nested_serializer(submitted):
    person, drug = therapy_patient()
    result = author_therapy_line(person, line_number=1, start_date=date(2023, 1, 1),
        drugs=[{'concept_id': drug.pk, 'maintenance': True}])
    owned = TherapyLineExposure.objects.get(episode=result.episode)
    payload = {'drug_exposure_id': owned.drug_exposure_id, 'concept_id': drug.pk}
    if submitted is not None:
        payload['maintenance'] = submitted
    serializer = TherapyLineDrugSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors
    author_therapy_line(person, line_number=1, episode=result.episode, replace=True,
        start_date=date(2023, 1, 1), drugs=[dict(serializer.validated_data)])
    owned.refresh_from_db()
    assert owned.is_maintenance is (submitted is not False)


@pytest.mark.parametrize('fourth_progression', ['2024-12-01', None])
def test_later_line_progression_is_per_line_not_the_flat_summary(fourth_progression):
    person, drug = therapy_patient()
    expected = {3: '2023-12-01', 4: fourth_progression}
    for number in (3, 4):
        author_therapy_line(person, line_number=number, start_date=date(2023, 1, 1),
            drugs=[{'concept_id': drug.pk}],
            progression_date=date.fromisoformat(expected[number]) if expected[number] else None)
    record = refresh_patient_record(person)
    assert {line['line']: line['progression_date'] for line in lines_of_therapy(record)} == expected
