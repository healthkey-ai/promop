from datetime import date, timedelta
from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import Death, Observation, PatientRecord, SupportiveTherapyCourse, TherapyOutcome
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.models import Identity, PatientUser
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def editor(settings):
    settings.CELERY_BROKER_URL = ''
    record = PatientRecordFactory(disease='Multiple Myeloma')
    identity = Identity.objects.create_user(email='treatment-editor@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)
    return record, client


@pytest.fixture
def catalog():
    migration = import_module('omop_core.migrations.0221_seed_treatment_editor_catalogs')
    migration.seed(apps, None)
    return migration


def patch_record(editor, payload):
    record, client = editor
    response = client.patch(f'/api/patient-info/{record.person_id}/', payload, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    return response


@pytest.mark.parametrize('field,value', [('relapse_count', 5), ('relapse_count', 0),
                                        ('refractory_status', 'Secondary Refractory')])
def test_override_survives_derivation_and_other_saves(editor, field, value):
    record, _ = editor
    patch_record(editor, {field: value})
    canonical = 'treatment_refractory_status' if field == 'refractory_status' else field
    assert getattr(record, canonical) == value
    assert record.therapy_overrides[canonical] == value
    record.save()
    assert getattr(refresh_patient_record(record.person), canonical) == value
    row = Observation.objects.get(person=record.person, observation_source_value='patient-record:' + canonical)
    assert row.observation_date == timezone.localdate()
    assert (row.value_as_number if field == 'relapse_count' else row.value_as_string) == value


def test_clear_overrides_restores_inference(editor):
    record, _ = editor
    patch_record(editor, {'relapse_count': 5, 'refractory_status': 'Multi-Refractory'})
    response = patch_record(editor, {'relapse_count': None, 'refractory_status': None})
    assert record.therapy_overrides == {}
    assert response.data['relapse_count'] == 0
    assert response.data['refractory_status'] == 'Unknown'
    assert refresh_patient_record(record.person).relapse_count == 0


def test_override_validation_and_internal_state_protection(editor):
    record, client = editor
    for payload in ({'relapse_count': -1}, {'refractory_status': 'anything'}):
        assert client.patch(f'/api/patient-info/{record.person_id}/', payload, format='json').status_code == 400
    patch_record(editor, {'therapy_overrides': {'relapse_count': -100}})
    assert record.therapy_overrides == {}


def test_death_date_roundtrip_correction_and_clear(editor):
    record, client = editor
    response = patch_record(editor, {'death_date': '2025-02-01'})
    assert response.data['death_date'] == '2025-02-01'
    assert Observation.objects.get(person=record.person, observation_source_value='patient-record:death_date').value_as_string == '2025-02-01'
    assert refresh_patient_record(record.person).death_date == date(2025, 2, 1)
    patch_record(editor, {'death_date': '2025-02-02'})
    assert Observation.objects.filter(person=record.person, observation_source_value='patient-record:death_date').count() == 1
    assert record.revisions.filter(field='death_date', old_value='2025-02-01', new_value='2025-02-02').exists()
    patch_record(editor, {'death_date': None})
    assert not Death.objects.filter(person=record.person).exists()
    assert refresh_patient_record(record.person).death_date is None
    response = client.patch(f'/api/patient-info/{record.person_id}/', {
        'death_date': (timezone.localdate() + timedelta(days=1)).isoformat()}, format='json')
    assert response.status_code == 400


def test_descriptor_exposes_overrides_and_death_date(editor):
    descriptors = build_writable_field_descriptor()
    for field in ('relapse_count', 'refractory_status', 'treatment_refractory_status', 'death_date'):
        assert descriptors[field]['writable'] is True


def test_catalog_is_idempotent_and_has_disease_specific_outcomes(editor, catalog):
    _, client = editor
    before = TherapyOutcome.objects.count()
    catalog.seed(apps, None)
    assert TherapyOutcome.objects.count() == before == 7
    bc = client.get('/api/v1/therapy-outcomes/', {'disease': 'C9335'})
    mm = client.get('/api/v1/therapy-outcomes/', {'disease': 'C3242'})
    assert {o['code'] for o in bc.data} == {'CR', 'PR', 'SD', 'PD'}
    assert {o['code'] for o in mm.data} == {'CR', 'sCR', 'VGPR', 'PR', 'MRD', 'SD', 'PD'}
    supportive = client.get('/api/v1/therapy-regimens/', {'disease': 'C3242', 'round': 'supportive_therapy'})
    assert supportive.status_code == 200
    assert any(r['code'] == 'ivig' for r in supportive.data)


def test_supportive_courses_create_edit_and_do_not_count_as_lines(editor, catalog):
    record, client = editor
    response = client.post('/api/v1/supportive-therapies/', {
        'person': record.person_id, 'regimen_code': 'ivig',
        'start_date': '2025-01-01', 'end_date': '2025-04-01',
        'intent': 'Supportive', 'discontinuation_reason': 'Toxicity'}, format='json')
    assert response.status_code == 201, response.data
    course = response.data['course']
    assert response.data['patient_info']['therapy_lines_count'] == 0
    assert response.data['patient_info']['supportive_therapy_courses'][0]['id'] == course['id']
    response = client.patch(f"/api/v1/supportive-therapies/{course['id']}/", {
        'end_date': None, 'intent': 'Palliative', 'discontinuation_reason': ''}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['course']['end_date'] is None
    assert response.data['course']['intent'] == 'Palliative'
    assert response.data['course']['discontinuation_reason'] == ''
    assert refresh_patient_record(record.person).therapy_lines_count == 0
    assert Observation.objects.filter(person=record.person, observation_source_value__startswith=f"supportive:{course['id']}:").count() == 5


def test_supportive_courses_validate_dates_and_patient_scope(editor, catalog):
    record, client = editor
    response = client.post('/api/v1/supportive-therapies/', {'person': record.person_id,
        'regimen_code': 'ivig', 'start_date': '2025-03-01', 'end_date': '2025-01-01'}, format='json')
    assert response.status_code == 400
    other = PatientRecordFactory()
    response = client.post('/api/v1/supportive-therapies/', {'person': other.person_id,
        'regimen_code': 'ivig', 'start_date': '2025-01-01'}, format='json')
    assert response.status_code in (403, 404)
    from omop_core.models import TherapyRegimen
    course = SupportiveTherapyCourse.objects.create(person=other.person, regimen=TherapyRegimen.objects.get(code='ivig'))
    response = client.patch(f'/api/v1/supportive-therapies/{course.pk}/', {'intent': 'Palliative'}, format='json')
    assert response.status_code in (403, 404)
    assert not SupportiveTherapyCourse.objects.filter(person=record.person).exists()


def test_manual_override_wins_over_active_formula(editor):
    from omop_core.models import FieldFormula
    record, _ = editor
    FieldFormula.objects.create(field_name='relapse_count', formula='99', is_active=True)
    patch_record(editor, {'relapse_count': 2})
    assert refresh_patient_record(record.person).relapse_count == 2
    record.refresh_from_db()
    assert record.relapse_count == 2


def test_supportive_course_preserves_prior_day_facts_and_replaces_legacy_summary(editor, catalog):
    record, client = editor
    record.supportive_therapies = 'Old free text'
    record.user_edited_fields = ['supportive_therapies']
    record.save()
    response = client.post('/api/v1/supportive-therapies/', {
        'person': record.person_id, 'regimen_code': 'ivig', 'start_date': '2025-01-01',
        'intent': 'Supportive'}, format='json')
    assert response.status_code == 201, response.data
    course = response.data['course']
    assert response.data['patient_info']['supportive_therapies'] == course['regimen_title']
    rows = Observation.objects.filter(person=record.person,
        observation_source_value__startswith=f"supportive:{course['id']}:")
    yesterday = timezone.localdate() - timedelta(days=1)
    rows.update(observation_date=yesterday)
    old_ids = list(rows.values_list('pk', flat=True))
    response = client.patch(f"/api/v1/supportive-therapies/{course['id']}/", {
        'intent': 'Palliative'}, format='json')
    assert response.status_code == 200, response.data
    assert rows.count() == 10
    assert rows.filter(pk__in=old_ids, observation_date=yesterday).count() == 5
    assert rows.get(pk__in=old_ids, observation_source_value__endswith=':intent').value_as_string == 'Supportive'
    assert rows.get(observation_date=timezone.localdate(), observation_source_value__endswith=':intent').value_as_string == 'Palliative'


def test_supportive_catalog_reuses_legacy_disease_codes(editor):
    from omop_core.models import Disease
    Disease.objects.create(code='MM', title='Multiple Myeloma')
    import_module('omop_core.migrations.0221_seed_treatment_editor_catalogs').seed(apps, None)
    record, client = editor
    response = client.get('/api/v1/therapy-regimens/', {'disease': 'C3242', 'round': 'supportive_therapy'})
    assert any(r['code'] == 'ivig' for r in response.data)
    response = client.post('/api/v1/supportive-therapies/', {
        'person': record.person_id, 'regimen_code': 'ivig'}, format='json')
    assert response.status_code == 201, response.data



def test_death_correction_preserves_imported_death_and_prior_day_assertions(editor):
    record, _ = editor
    Death.objects.create(person=record.person, death_date=date(2025, 1, 1), death_type_concept_id=0)
    patch_record(editor, {'death_date': '2025-02-01'})
    row = Observation.objects.get(person=record.person, observation_source_value='patient-record:death_date')
    row.observation_date = timezone.localdate() - timedelta(days=1)
    row.save()
    patch_record(editor, {'death_date': '2025-02-02'})
    assert Death.objects.get(person=record.person).death_date == date(2025, 1, 1)
    row.refresh_from_db()
    assert row.value_as_string == '2025-02-01'
    assert Observation.objects.filter(person=record.person, observation_source_value='patient-record:death_date').count() == 2
    patch_record(editor, {'death_date': None})
    assert refresh_patient_record(record.person).death_date is None
    assert Death.objects.get(person=record.person).death_date == date(2025, 1, 1)


@pytest.mark.parametrize('disease,outcome,valid,normalized', [
    ('Breast Cancer', 'VGPR', False, None),
    ('Breast Cancer', 'PR', True, 'Partial Response'),
    ('Multiple Myeloma', 'VGPR', True, 'Very Good Partial Response'),
])
def test_line_outcome_is_validated_for_patient_disease(editor, catalog, disease, outcome, valid, normalized):
    from patient_portal.api.serializers import TherapyLineWriteSerializer
    record, _ = editor
    record.disease = disease
    record.save()
    serializer = TherapyLineWriteSerializer(data={'person': record.person_id,
        'line_number': 1, 'drugs': [{'concept_id': 0}], 'outcome': outcome})
    assert serializer.is_valid() is valid
    if valid:
        assert serializer.validated_data['outcome'] == normalized
    else:
        assert 'outcome' in serializer.errors
