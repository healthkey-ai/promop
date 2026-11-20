"""Tests for the cascading disease-category / disease filter on the patient list."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import DiseaseCategoryChoice, DiseaseCategoryMapping
from patient_portal.models import Identity
from tests.factories import OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db

MM_DISEASES = ['Multiple Myeloma', 'Smoldering Myeloma']
BC_DISEASES = ['Invasive Ductal Carcinoma', 'Triple-Negative Breast Cancer']


@pytest.fixture(autouse=True)
def seed_categories():
    DiseaseCategoryChoice.objects.get_or_create(code='MM', defaults={'label': 'Multiple Myeloma'})
    DiseaseCategoryChoice.objects.get_or_create(code='BC', defaults={'label': 'Breast Cancer'})
    for name in MM_DISEASES:
        DiseaseCategoryMapping.objects.get_or_create(disease_name=name, defaults={'category': 'MM'})
    for name in BC_DISEASES:
        DiseaseCategoryMapping.objects.get_or_create(disease_name=name, defaults={'category': 'BC'})


@pytest.fixture
def staff_client():
    user = Identity.objects.create_user(email='dcf-staff@example.test', is_staff=True)
    OrganizationFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# 1. Mappings endpoint: ?category= filter
# ---------------------------------------------------------------------------

class TestMappingsFilterByCategory:
    def test_returns_only_matching_category(self, staff_client):
        resp = staff_client.get('/api/v1/disease-category-mappings/', {'category': 'MM'})
        assert resp.status_code == 200
        names = {m['disease_name'] for m in resp.data}
        assert names == set(MM_DISEASES)

    def test_category_filter_is_case_insensitive(self, staff_client):
        resp = staff_client.get('/api/v1/disease-category-mappings/', {'category': 'mm'})
        assert resp.status_code == 200
        assert len(resp.data) == len(MM_DISEASES)

    def test_unknown_category_returns_empty(self, staff_client):
        resp = staff_client.get('/api/v1/disease-category-mappings/', {'category': 'UNKNOWN'})
        assert resp.status_code == 200
        assert resp.data == []


# ---------------------------------------------------------------------------
# 2. Filter options include categories and diseases_by_category
# ---------------------------------------------------------------------------

class TestFilterOptionsIncludeCategories:
    def test_filter_options_have_disease_categories(self, staff_client):
        PatientRecordFactory(disease='Multiple Myeloma')
        resp = staff_client.get('/api/patient-info/', {'page': 1, 'page_size': 1})
        assert resp.status_code == 200
        opts = resp.data['filter_options']
        assert 'disease_categories' in opts
        codes = {c['value'] for c in opts['disease_categories']}
        assert 'MM' in codes
        assert 'BC' in codes

    def test_filter_options_have_diseases_by_category(self, staff_client):
        PatientRecordFactory(disease='Multiple Myeloma')
        resp = staff_client.get('/api/patient-info/', {'page': 1, 'page_size': 1})
        assert resp.status_code == 200
        opts = resp.data['filter_options']
        assert 'diseases_by_category' in opts
        assert sorted(opts['diseases_by_category']['MM']) == sorted(MM_DISEASES)
        assert sorted(opts['diseases_by_category']['BC']) == sorted(BC_DISEASES)


# ---------------------------------------------------------------------------
# 3. Patient list: ?disease_category= filter
# ---------------------------------------------------------------------------

class TestPatientListFilterByCategory:
    def test_category_filter_returns_matching_patients(self, staff_client):
        org = OrganizationFactory()
        r_mm = PatientRecordFactory(disease='Multiple Myeloma', organization=org)
        r_bc = PatientRecordFactory(disease='Invasive Ductal Carcinoma', organization=org)
        resp = staff_client.get('/api/patient-info/', {'page': 1, 'page_size': 50, 'disease_category': 'MM'})
        assert resp.status_code == 200
        ids = {r['person_id'] for r in resp.data['results']}
        assert r_mm.person_id in ids
        assert r_bc.person_id not in ids

    def test_category_filter_is_case_insensitive(self, staff_client):
        org = OrganizationFactory()
        r_mm = PatientRecordFactory(disease='Smoldering Myeloma', organization=org)
        resp = staff_client.get('/api/patient-info/', {'page': 1, 'page_size': 50, 'disease_category': 'mm'})
        assert resp.status_code == 200
        ids = {r['person_id'] for r in resp.data['results']}
        assert r_mm.person_id in ids


# ---------------------------------------------------------------------------
# 4. Patient list: ?disease_category= + ?disease= combined
# ---------------------------------------------------------------------------

class TestPatientListFilterByCategoryAndDisease:
    def test_both_filters_narrow_results(self, staff_client):
        org = OrganizationFactory()
        r1 = PatientRecordFactory(disease='Multiple Myeloma', organization=org)
        r2 = PatientRecordFactory(disease='Smoldering Myeloma', organization=org)
        resp = staff_client.get('/api/patient-info/', {
            'page': 1, 'page_size': 50,
            'disease_category': 'MM',
            'disease': 'Smoldering Myeloma',
        })
        assert resp.status_code == 200
        ids = {r['person_id'] for r in resp.data['results']}
        assert r2.person_id in ids
        assert r1.person_id not in ids


# ---------------------------------------------------------------------------
# 5. Existing ?disease= filter still works alone
# ---------------------------------------------------------------------------

class TestDiseaseFilterAlone:
    def test_disease_param_works_without_category(self, staff_client):
        org = OrganizationFactory()
        r1 = PatientRecordFactory(disease='Multiple Myeloma', organization=org)
        r2 = PatientRecordFactory(disease='Invasive Ductal Carcinoma', organization=org)
        resp = staff_client.get('/api/patient-info/', {
            'page': 1, 'page_size': 50,
            'disease': 'Multiple Myeloma',
        })
        assert resp.status_code == 200
        ids = {r['person_id'] for r in resp.data['results']}
        assert r1.person_id in ids
        assert r2.person_id not in ids
