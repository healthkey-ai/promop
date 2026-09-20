from decimal import Decimal
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from omop_core.models import CanonicalUnitPreference, CanonicalUnitChange, LoincClass, LoincCodeClass
from omop_core.services.canonical_units import convert, normalize, property_for
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory, PatientRecordFactory


@pytest.mark.parametrize('value,source,target,prop,expected', [
    (20, 'g/L', 'g/dL', 'MCnc', '2'), (2, 'g/dL', 'g/L', 'MCnc', '20'),
    (0, 'mg/L', 'mg/dL', 'MCnc', '0'), (1200, 'ng/mL', 'ug/mL', 'MCnc', '1.2'),
    (5, 'mmol/L', 'umol/L', 'SCnc', '5000'),
    (1500, 'cells/uL', '10*9/L', 'NCnc', '1.5'),
    (1, 'G/L', '10*3/uL', 'NCnc', '1'),
    (98.6, '[degF]', 'Cel', 'Temp', '37'), (0, 'Cel', '[degF]', 'Temp', '32'),
    (2, 'min', 's', 'Time', '120'), (500, 'µg/mL', 'mg/dL', 'MCnc', '50'),
])
def test_conversion(value, source, target, prop, expected):
    assert convert(value, source, target, prop) == Decimal(expected)


@pytest.mark.parametrize('value,source,target,prop', [
    (20, 'mmol/L', 'g/L', 'MCnc'), (20, 'g/L', '/L', 'NCnc'),
    (20, 'G/L', 'g/L', 'MCnc'), (20, '', 'g/L', 'MCnc'),
    (20, None, 'g/L', 'MCnc'), (20, 'mg/dL', 'g/L', 'SCnc'),
    ('NaN', 'g/L', 'g/dL', 'MCnc'), ('Infinity', 'g/L', 'g/dL', 'MCnc'),
    (True, 'g/L', 'g/dL', 'MCnc'), ('bad', 'g/L', 'g/dL', 'MCnc'),
])
def test_invalid_conversion(value, source, target, prop):
    with pytest.raises(ValueError):
        convert(value, source, target, prop)


def test_normalize_ranges_and_fail_closed():
    policy = SimpleNamespace(unit='g/dL', property='MCnc', revision=1)
    assert normalize(policy, 20, 'g/L', 10, 30) == {
        'unit': 'g/dL', 'revision': 1, 'value': Decimal(2),
        'range_low': Decimal(1), 'range_high': Decimal(3), 'error': None}
    result = normalize(policy, 20, 'mmol/L', 10, 30)
    assert result['value'] is result['range_low'] is result['range_high'] is None
    assert result['error']


def test_imported_property_wins_over_name():
    concept = SimpleNamespace(vocabulary_id='LOINC', domain_id='Measurement', concept_name='Test [Mass/volume]')
    assert property_for(concept) == 'MCnc'
    assert property_for(concept, SimpleNamespace(property='SCnc', scale_type='Qn')) == 'SCnc'
    assert property_for(concept, SimpleNamespace(property='MCnc', scale_type='Ord')) == ''


@pytest.fixture
def setup_units(db):
    client = APIClient()
    admin = Identity.objects.create_user(email='unit-admin@example.org', password='test', is_staff=True)
    client.force_authenticate(admin)
    concept = ConceptFactory(concept_code='33358-3', concept_name='Protein.monoclonal [Mass/volume]', standard_concept='S')
    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
    LoincCodeClass.objects.create(loinc_num='33358-3', loinc_class_id='CHEM', example_units='g/L;g/dL', property='MCnc', scale_type='Qn')
    url = f'/api/v1/concepts/{concept.pk}/canonical-unit/'
    return client, admin, concept, url


def test_admin_setting_validation_revision_and_audit(setup_units):
    client, admin, concept, url = setup_units
    before = client.get(url).json()
    assert before['example_units'] == ['g/L', 'g/dL']
    assert 'mmol/L' not in before['available_units']
    assert client.put(url, {'unit': 'mmol/L', 'revision': 0}, format='json').status_code == 400
    response = client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json')
    assert response.status_code == 200
    assert response.json()['revision'] == 1
    assert client.put(url, {'unit': 'g/L', 'revision': 0}, format='json').status_code == 409
    assert CanonicalUnitPreference.objects.get(concept=concept).unit == 'g/dL'
    change = CanonicalUnitChange.objects.get()
    assert change.changed_by == admin and change.previous_unit == '' and change.unit == 'g/dL'
    assert client.put(url, {'unit': '', 'revision': 1}, format='json').status_code == 200
    assert CanonicalUnitChange.objects.count() == 2
    assert client.get(url).json()['unit'] == ''


def test_non_admin_cannot_change_instance_setting(setup_units):
    client, _, _, url = setup_units
    user = Identity.objects.create_user(email='curator@example.org', password='test')
    client.force_authenticate(user)
    assert not client.get(url).json()['can_edit']
    assert client.put(url, {'unit': 'g/L', 'revision': 0}, format='json').status_code == 403
    client.force_authenticate(None)
    assert client.get(url).status_code in (401, 403)
    assert not CanonicalUnitPreference.objects.exists()


def test_inactive_or_nonstandard_concept_rejected(setup_units):
    client, _, concept, url = setup_units
    concept.standard_concept = None
    concept.save()
    assert client.put(url, {'unit': 'g/L', 'revision': 0}, format='json').status_code == 400


def test_existing_and_future_measurements_follow_current_policy_without_mutation(setup_units):
    client, _, concept, url = setup_units
    person = PersonFactory()
    PatientRecordFactory(person=person)
    row = MeasurementFactory(person=person, measurement_concept=concept, value_as_number=20,
                             unit_source_value='g/L', range_low=10, range_high=30)
    assert client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json').status_code == 200
    detail = f'/api/v1/measurements/{row.pk}/'
    response = client.get(detail)
    assert response.status_code == 200, response.data
    payload = response.json()
    assert Decimal(payload['value_as_number']) == 20
    assert Decimal(str(payload['normalized']['value'])) == 2
    assert Decimal(str(payload['normalized']['range_low'])) == 1
    assert client.put(url, {'unit': 'mg/L', 'revision': 1}, format='json').status_code == 200
    assert Decimal(str(client.get(detail).json()['normalized']['value'])) == 20000
    later = MeasurementFactory(person=person, measurement_concept=concept, value_as_number=2, unit_source_value='g/dL')
    assert Decimal(str(client.get(f'/api/v1/measurements/{later.pk}/').json()['normalized']['value'])) == 20000
    row.refresh_from_db()
    assert row.value_as_number == 20 and row.unit_source_value == 'g/L' and row.range_low == 10
    assert client.put(url, {'unit': '', 'revision': 2}, format='json').status_code == 200
    assert client.get(detail).json()['normalized'] is None


def test_lab_views_expose_same_read_model_and_keep_edit_units(setup_units):
    client, _, concept, url = setup_units
    person = PersonFactory()
    PatientRecordFactory(person=person)
    row = MeasurementFactory(person=person, measurement_concept=concept, value_as_number=20,
                             unit_source_value='g/L', range_low=10, range_high=30)
    client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json')
    # Lab views use a different serializer and SQL hydration for summary.
    endpoints = [f'/api/v1/lab-results/measurements/{row.pk}/',
                 f'/api/v1/lab-results/values/?concept_code=33358-3&person_id={person.pk}',
                 f'/api/v1/lab-results/summary/?person_id={person.pk}']
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200, (endpoint, response.data)
        data = response.json()
        if 'results' in data:
            data = data['results'][0]
        if 'values' in data:
            data = data['values'][0]
        assert Decimal(str(data['normalized']['value'])) == 2
        assert Decimal(data['value']) == 20 and data['unit'] == 'g/L'
    # Existing write contract edits ORIGINAL units, never the normalized number.
    assert client.patch(endpoints[0], {'value': 30}, format='json').status_code == 200
    assert Decimal(str(client.get(endpoints[0]).json()['normalized']['value'])) == 3
    row.refresh_from_db()
    assert row.value_as_number == 30 and row.unit_source_value == 'g/L'


def test_ucum_fallback_and_explicit_unknown_source_wins(setup_units):
    from omop_core.services.canonical_units import measurement_normalized, policies
    from tests.factories import VocabularyFactory
    client, _, concept, url = setup_units
    client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json')
    ucum = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='UCUM'), concept_code='g/L')
    row = MeasurementFactory(measurement_concept=concept, value_as_number=20, unit_source_value='', unit_concept=ucum)
    assert measurement_normalized(row, policies())['value'] == 2
    row.unit_source_value = 'unknown'
    assert measurement_normalized(row, policies())['error']


def test_metadata_loader_and_example_units_are_independent_of_suggested_unit(setup_units, tmp_path):
    from omop_core.management.commands.load_loinc_classes import Command
    from omop_core.services.concept_unit_info import concept_unit_fields, get_loinc_example_units, get_loinc_to_unit
    _, _, concept, _ = setup_units
    path = tmp_path / 'Loinc.csv'
    path.write_text('LOINC_NUM,CLASS,PROPERTY,SCALE_TYP,EXAMPLE_UNITS\n33358-3,CHEM,MCnc,Qn,g/L;g/dL;g/L\n')
    Command()._load_code_class_mapping(path)
    metadata = LoincCodeClass.objects.get(pk='33358-3')
    assert (metadata.property, metadata.scale_type) == ('MCnc', 'Qn')
    get_loinc_example_units.cache_clear()
    get_loinc_to_unit.cache_clear()
    try:
        assert concept_unit_fields(concept)['example_units'] == ['g/L', 'g/dL']
    finally:
        get_loinc_example_units.cache_clear()
        get_loinc_to_unit.cache_clear()


def test_policy_snapshot_does_not_query_once_per_measurement(setup_units, django_assert_num_queries):
    from patient_portal.api.serializers import MeasurementSerializer
    from omop_core.models import Measurement
    client, _, concept, url = setup_units
    client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json')
    person = PersonFactory()
    for value in (10, 20, 30):
        MeasurementFactory(person=person, measurement_concept=concept, value_as_number=value, unit_source_value='g/L')
    rows = list(Measurement.objects.select_related('measurement_concept', 'unit_concept').filter(person=person))
    with django_assert_num_queries(2):
        data = MeasurementSerializer(rows, many=True).data
    assert sorted(d['normalized']['value'] for d in data) == [1, 2, 3]


def test_vocabulary_property_change_invalidates_normalization(setup_units):
    from omop_core.services.canonical_units import measurement_normalized, policies
    client, _, concept, url = setup_units
    client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json')
    row = MeasurementFactory(measurement_concept=concept, value_as_number=20, unit_source_value='g/L')
    LoincCodeClass.objects.filter(pk='33358-3').update(property='SCnc')
    result = measurement_normalized(row, policies())
    assert result['value'] is None
    assert 'vocabulary changed' in result['error']


def test_summary_and_detail_agree_on_blank_unit_fallback(setup_units):
    from tests.factories import VocabularyFactory
    client, _, concept, url = setup_units
    client.put(url, {'unit': 'g/dL', 'revision': 0}, format='json')
    person = PersonFactory()
    PatientRecordFactory(person=person)
    unit = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='UCUM'), concept_code='g/L')
    row = MeasurementFactory(person=person, measurement_concept=concept, value_as_number=20,
                             unit_source_value='   ', unit_concept=unit)
    detail = client.get(f'/api/v1/measurements/{row.pk}/').json()['normalized']
    summary = client.get(f'/api/v1/lab-results/summary/?person_id={person.pk}').json()['results'][0]['values'][0]['normalized']
    assert detail == summary
    assert Decimal(str(detail['value'])) == 2
