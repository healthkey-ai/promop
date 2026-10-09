from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, PatientRecord
from omop_core.services.patient_list_context import annotate_context, filter_context, latest_treatment, order_context
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.serializers import PatientListSerializer
from patient_portal.models import Identity, PatientUser
from tests.factories import MeasurementFactory, ObservationFactory, OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff():
    user = Identity.objects.create_user(email='list-context@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return user, client


def test_latest_line_handles_unsorted_later_lines_and_missing_dates_without_active_inference():
    record = PatientRecordFactory(first_line_therapy='A', second_line_therapy='B',
        later_therapies=[{'lineNumber': 4, 'therapy': 'D', 'startDate': '2025-06-01'},
                         {'lineNumber': 3, 'therapy': 'C', 'startDate': 'invalid'}])
    summary = latest_treatment(record)
    assert summary == {'name': 'D', 'line': 4, 'start_date': date(2025, 6, 1), 'end_date': None}
    assert latest_treatment(PatientRecordFactory()) is None


def test_negative_genomics_and_ecog_zero_are_recorded_not_missing(staff):
    actor, _ = staff
    record = PatientRecordFactory(stage='  unknown ', ecog_performance_status=0,
        genetic_mutations=[{'gene': 'BRCA1', 'status': 'absent'}], hr_status='Negative')
    annotated = annotate_context(PatientRecord.objects.filter(pk=record.pk)).get()
    data = PatientListSerializer(annotated).data
    assert data['data_gaps'] == ['Stage']
    assert annotated.key_gap_count == 1
    assert data['ecog_performance_status'] == 0
    assert data['subtype_biomarkers'] == 'HR: Negative'
    assert filter_context(PatientRecord.objects.all(), {'ecog': '0', 'data_gap': 'stage'}, actor).get().pk == record.pk
    assert not filter_context(PatientRecord.objects.all(), {'ecog': 'unknown'}, actor).exists()


def test_freshness_uses_result_dates_and_excludes_erroneous_future_and_clear_rows(django_assert_num_queries):
    record = PatientRecordFactory()
    today = timezone.localdate()
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_date=today-timedelta(days=100), value_as_number=0)
        ObservationFactory(person=record.person, observation_date=today-timedelta(days=10), value_as_string='negative')
        MeasurementFactory(person=record.person, measurement_date=today, value_as_number=1, is_erroneous=True)
        MeasurementFactory(person=record.person, measurement_date=today+timedelta(days=1), value_as_number=1)
        ObservationFactory(person=record.person, observation_date=today, value_as_string='old', value_source_value='PatientRecord:cleared')
    other = PatientRecordFactory()
    with django_assert_num_queries(1), patch('omop_core.services.patient_record_service.refresh_patient_record') as refresh:
        data = PatientListSerializer(annotate_context(PatientRecord.objects.select_related('person', 'organization')).order_by('pk'), many=True).data
    refresh.assert_not_called()
    by_id = {item['person_id']: item for item in data}
    assert by_id[record.person_id]['latest_result_date'] == (today-timedelta(days=10)).isoformat()
    assert by_id[other.person_id]['latest_result_date'] is None


def test_filters_and_ordering_apply_before_pagination_and_unknowns_sort_last(staff):
    actor, client = staff
    unknown = PatientRecordFactory(ecog_performance_status=None)
    high = PatientRecordFactory(ecog_performance_status=2, condition_clinical_status='Remission')
    low = PatientRecordFactory(ecog_performance_status=0, condition_clinical_status='Remission')
    response = client.get('/api/patient-info/', {'page': 1, 'page_size': 1, 'ordering': 'ecog'})
    assert response.status_code == 200, response.data
    assert response.data['count'] == 3
    assert response.data['results'][0]['person_id'] == low.person_id
    query = annotate_context(PatientRecord.objects.all())
    assert list(order_context(query, '-ecog', actor).values_list('pk', flat=True)) == [high.pk, low.pk, unknown.pk]
    response = client.get('/api/patient-info/', {'page': 1, 'clinical_status': 'Remission', 'ecog': '0'})
    assert response.data['count'] == 1
    assert response.data['results'][0]['person_id'] == low.person_id
    assert 'Remission' in response.data['filter_options']['clinical_statuses']


@pytest.mark.parametrize('params', [
    {'treatment': 'regimen'}, {'biomarker': 'HER2'}, {'location': 'Portland'},
    {'data_gap': 'none'}, {'contact': 'available'}, {'freshness': '30d'},
])
def test_filters_find_matching_records_without_changing_tenant_scope(staff, params):
    actor, client = staff
    org = OrganizationFactory()
    actor.is_staff = False
    actor.save()
    GroupAccess.objects.create(identity=actor, org=org, role='org_admin')
    matching = PatientRecordFactory(organization=org, first_line_therapy='Example regimen', city='Portland',
        stage='II', ecog_performance_status=1, genetic_mutations=[{'gene': 'HER2', 'status': 'absent'}], email='test@example.test')
    PatientRecordFactory(organization=org)
    PatientRecordFactory(first_line_therapy='Example regimen', city='Portland', stage='II', ecog_performance_status=1,
        genetic_mutations=[{'gene': 'HER2'}], email='test@example.test')
    with suppress_patient_record_refresh():
        MeasurementFactory(person=matching.person, measurement_date=timezone.localdate(), value_as_number=5)
    response = client.get('/api/patient-info/', {'page': 1, **params})
    assert response.status_code == 200, response.data
    assert response.data['count'] == 1
    assert response.data['results'][0]['person_id'] == matching.person_id


def test_status_unknown_matches_display_and_falls_back_to_recorded_progression(staff):
    actor, _ = staff
    missing = PatientRecordFactory(condition_clinical_status='Unknown', progression='N/A')
    recorded = PatientRecordFactory(condition_clinical_status='Unknown', progression='Progression')
    query = filter_context(PatientRecord.objects.all(), {'clinical_status': '__unknown__'}, actor)
    assert list(query.values_list('pk', flat=True)) == [missing.pk]
    assert annotate_context(PatientRecord.objects.filter(pk=recorded.pk)).get().list_disease_status == 'Progression'


def test_redacted_location_cannot_be_exposed_via_display_or_location_filter(staff):
    actor, client = staff
    record = PatientRecordFactory(city='PrivateCity', suppress_demographics_for_others=True)
    response = client.get('/api/patient-info/', {'page': 1})
    row = response.data['results'][0]
    assert row['location_summary'] is None and row['age'] is None and row['patient_name'] is None
    assert row['demographics_redacted'] is True
    assert client.get('/api/patient-info/', {'page': 1, 'location': 'PrivateCity'}).data['count'] == 0
    PatientUser.objects.create(identity=actor, person=record.person)
    assert client.get('/api/patient-info/', {'page': 1, 'location': 'PrivateCity'}).data['count'] == 1


def test_filtered_delete_uses_the_same_new_filters_as_the_list(staff):
    _, client = staff
    keep = PatientRecordFactory(ecog_performance_status=None)
    remove = PatientRecordFactory(ecog_performance_status=0)
    response = client.delete('/api/patient-info/bulk_delete_filtered/?ecog=0')
    assert response.status_code == 200, response.data
    assert PatientRecord.objects.filter(pk=keep.pk).exists()
    assert not PatientRecord.objects.filter(pk=remove.pk).exists()


@pytest.mark.parametrize('params', [{'ecog': 'bad'}, {'freshness': 'bad'}, {'data_gap': 'bad'}, {'ordering': 'secret_column'}])
def test_invalid_filters_fail_explicitly(staff, params):
    response = staff[1].get('/api/patient-info/', {'page': 1, **params})
    assert response.status_code == 400
