"""Tests for DiseaseCategoryMapping model and API."""

import pytest
from rest_framework.test import APIClient

from omop_core.models import DiseaseCategoryMapping, DiseaseCategory, Organization
from patient_portal.models import Identity


@pytest.fixture
def staff_client():
    # has_org_admin_access requires at least one Organization row for staff.
    Organization.objects.get_or_create(slug='test-org', defaults={'name': 'Test Org'})
    user = Identity.objects.create_user(email='dcm-staff@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def patient_client():
    user = Identity.objects.create_user(email='dcm-patient@example.test', is_staff=False)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def anon_client():
    return APIClient()


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDiseaseCategoryMappingModel:
    def test_create_and_retrieve(self):
        obj = DiseaseCategoryMapping.objects.create(
            disease_name='Smoldering myeloma', category='MM',
        )
        assert obj.pk is not None
        fetched = DiseaseCategoryMapping.objects.get(pk=obj.pk)
        assert fetched.disease_name == 'Smoldering myeloma'
        assert fetched.category == 'MM'

    def test_unique_disease_name(self):
        DiseaseCategoryMapping.objects.create(disease_name='Myeloma', category='MM')
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            DiseaseCategoryMapping.objects.create(disease_name='Myeloma', category='Other')

    def test_valid_categories(self):
        for code in ('BC', 'MM', 'FL', 'CLL', 'MCL', 'Other'):
            assert code in DiseaseCategory.values

    def test_str(self):
        obj = DiseaseCategoryMapping(disease_name='Test', category='BC')
        assert str(obj) == 'Test → BC'


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------

URL_LIST = '/api/v1/disease-category-mappings/'


def _detail_url(pk):
    return f'/api/v1/disease-category-mappings/{pk}/'


@pytest.mark.django_db
class TestDiseaseCategoryMappingListAPI:
    def test_get_list(self, staff_client):
        DiseaseCategoryMapping.objects.create(disease_name='A disease', category='MM')
        DiseaseCategoryMapping.objects.create(disease_name='B disease', category='BC')
        resp = staff_client.get(URL_LIST)
        assert resp.status_code == 200
        names = [item['disease_name'] for item in resp.json()]
        assert 'A disease' in names
        assert 'B disease' in names

    def test_get_list_search(self, staff_client):
        DiseaseCategoryMapping.objects.create(disease_name='Multiple myeloma', category='MM')
        DiseaseCategoryMapping.objects.create(disease_name='Breast cancer', category='BC')
        resp = staff_client.get(URL_LIST, {'search': 'myeloma'})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]['disease_name'] == 'Multiple myeloma'

    def test_post_create(self, staff_client):
        resp = staff_client.post(URL_LIST, {
            'disease_name': 'New disease', 'category': 'FL',
        }, format='json')
        assert resp.status_code == 201
        assert resp.json()['disease_name'] == 'New disease'
        assert resp.json()['category'] == 'FL'
        assert DiseaseCategoryMapping.objects.filter(disease_name='New disease').exists()

    def test_post_duplicate_409(self, staff_client):
        DiseaseCategoryMapping.objects.create(disease_name='Dup disease', category='MM')
        resp = staff_client.post(URL_LIST, {
            'disease_name': 'dup disease', 'category': 'BC',  # case-insensitive duplicate
        }, format='json')
        assert resp.status_code == 409

    def test_post_invalid_category(self, staff_client):
        resp = staff_client.post(URL_LIST, {
            'disease_name': 'Some disease', 'category': 'INVALID',
        }, format='json')
        assert resp.status_code == 400

    def test_post_missing_fields(self, staff_client):
        resp = staff_client.post(URL_LIST, {'disease_name': 'Only name'}, format='json')
        assert resp.status_code == 400


@pytest.mark.django_db
class TestDiseaseCategoryMappingDetailAPI:
    def test_patch_category(self, staff_client):
        obj = DiseaseCategoryMapping.objects.create(disease_name='Patch me', category='MM')
        resp = staff_client.patch(_detail_url(obj.pk), {'category': 'Other'}, format='json')
        assert resp.status_code == 200
        obj.refresh_from_db()
        assert obj.category == 'Other'

    def test_patch_disease_name(self, staff_client):
        obj = DiseaseCategoryMapping.objects.create(disease_name='Old name', category='MM')
        resp = staff_client.patch(_detail_url(obj.pk), {'disease_name': 'New name'}, format='json')
        assert resp.status_code == 200
        obj.refresh_from_db()
        assert obj.disease_name == 'New name'

    def test_patch_duplicate_name_409(self, staff_client):
        DiseaseCategoryMapping.objects.create(disease_name='Existing', category='MM')
        obj = DiseaseCategoryMapping.objects.create(disease_name='Other name', category='BC')
        resp = staff_client.patch(_detail_url(obj.pk), {'disease_name': 'existing'}, format='json')
        assert resp.status_code == 409

    def test_delete(self, staff_client):
        obj = DiseaseCategoryMapping.objects.create(disease_name='Delete me', category='MM')
        resp = staff_client.delete(_detail_url(obj.pk))
        assert resp.status_code == 204
        assert not DiseaseCategoryMapping.objects.filter(pk=obj.pk).exists()

    def test_delete_not_found(self, staff_client):
        resp = staff_client.delete(_detail_url(99999))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestDiseaseCategoryMappingPermissions:
    def test_anon_get_401(self, anon_client):
        resp = anon_client.get(URL_LIST)
        assert resp.status_code == 401

    def test_patient_get_allowed(self, patient_client):
        """Non-admin users can read (GET) the list."""
        resp = patient_client.get(URL_LIST)
        assert resp.status_code == 200

    def test_patient_post_403(self, patient_client):
        resp = patient_client.post(URL_LIST, {
            'disease_name': 'Forbidden', 'category': 'MM',
        }, format='json')
        assert resp.status_code == 403

    def test_patient_patch_403(self, patient_client):
        obj = DiseaseCategoryMapping.objects.create(disease_name='No touch', category='MM')
        resp = patient_client.patch(_detail_url(obj.pk), {'category': 'BC'}, format='json')
        assert resp.status_code == 403

    def test_patient_delete_403(self, patient_client):
        obj = DiseaseCategoryMapping.objects.create(disease_name='No delete', category='MM')
        resp = patient_client.delete(_detail_url(obj.pk))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# disease_code / _canonicalize_disease wiring
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDiseaseCategoryWiring:
    def test_genomics_disease_code_fallback(self):
        """genomics_catalog.disease_code() consults the table for unknown names."""
        from omop_core.services.genomics_catalog import disease_code
        # Known alias — handled by hardcoded dict
        assert disease_code('breast cancer') == 'BC'
        # Unknown name — no mapping yet
        assert disease_code('Smoldering myeloma') is None
        # Add a mapping
        DiseaseCategoryMapping.objects.create(disease_name='Smoldering myeloma', category='MM')
        assert disease_code('Smoldering myeloma') == 'MM'

    def test_canonicalize_disease_fallback(self):
        """_canonicalize_disease() consults the table for unknown names."""
        from omop_core.services.patient_record_service import _canonicalize_disease
        # Known alias
        assert _canonicalize_disease('myeloma') == 'Multiple Myeloma'
        # Unknown name — returned unchanged
        assert _canonicalize_disease('Chronic lymphoid leukemia, disease') == 'Chronic lymphoid leukemia, disease'
        # Add mapping
        DiseaseCategoryMapping.objects.create(
            disease_name='Chronic lymphoid leukemia, disease', category='CLL',
        )
        assert _canonicalize_disease('Chronic lymphoid leukemia, disease') == 'Chronic Lymphocytic Leukemia'

    def test_treatment_disease_code_fallback(self):
        """treatment_catalog.disease_code() consults the table for unknown names."""
        from omop_core.services.treatment_catalog import disease_code
        # Known alias — substring 'breast' matches the hardcoded pattern
        assert disease_code('breast') == 'C9335'
        # Unknown name — no hardcoded pattern match, no mapping row yet
        assert disease_code('Marginal zone lymphoma') == 'Marginal zone lymphoma'
        # Add a mapping
        DiseaseCategoryMapping.objects.create(disease_name='Marginal zone lymphoma', category='FL')
        assert disease_code('Marginal zone lymphoma') == 'C3209'

    def test_other_category_not_returned_as_code(self):
        """genomics_catalog.disease_code() returns None for 'Other' category."""
        from omop_core.services.genomics_catalog import disease_code
        DiseaseCategoryMapping.objects.create(
            disease_name='Diffuse large B-cell lymphoma', category='Other',
        )
        assert disease_code('Diffuse large B-cell lymphoma') is None


# ---------------------------------------------------------------------------
# Seed migration sanity
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSeedData:
    def test_seed_migration_populates_rows(self):
        """The seed migration should have pre-populated rows (pytest runs with
        --no-migrations, so we call the seed function directly)."""
        import importlib
        mod = importlib.import_module('omop_core.migrations.0318_seed_disease_category_mappings')
        from django.apps import apps
        mod.seed_forward(apps, None)
        assert DiseaseCategoryMapping.objects.count() >= len(mod._SEED)
        # Spot check a few
        assert DiseaseCategoryMapping.objects.filter(
            disease_name='Multiple Myeloma', category='MM',
        ).exists()
        assert DiseaseCategoryMapping.objects.filter(
            disease_name='Breast Cancer', category='BC',
        ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('name', [
    'Family history of breast cancer', 'Family history of multiple myeloma',
    'At high risk for malignant neoplasm of breast',
])
def test_family_history_and_risk_do_not_become_the_patients_cancer(name):
    from omop_core.services.patient_record_service import _canonicalize_disease, _get_disease_data
    from tests.factories import ConceptFactory, ConditionOccurrenceFactory, PersonFactory
    DiseaseCategoryMapping.objects.create(disease_name=name, category='MM' if 'myeloma' in name else 'BC')
    person = PersonFactory()
    ConditionOccurrenceFactory(person=person, condition_concept=ConceptFactory(concept_name=name))
    assert _canonicalize_disease(name) == name
    assert not _get_disease_data(person).get('disease')


@pytest.mark.django_db
def test_patient_list_keeps_literal_diseases_and_filters_other_category():
    from types import SimpleNamespace
    from patient_portal.api.views import PatientRecordViewSet
    from tests.factories import PatientRecordFactory
    from omop_core.models import PatientRecord
    disease = 'Diffuse large B-cell lymphoma'
    DiseaseCategoryMapping.objects.create(disease_name=disease, category='Other')
    other = PatientRecordFactory(disease=disease)
    known = PatientRecordFactory(disease='Multiple Myeloma')
    view = PatientRecordViewSet()
    user = Identity.objects.create_user(email='filter-other@example.test', is_staff=True)
    view.request = SimpleNamespace(query_params={'disease': 'Other'}, user=user)
    queryset = PatientRecord.objects.filter(pk__in=[other.pk, known.pk])
    assert disease in view._build_filter_options(queryset)['diseases']
    assert set(view._apply_patient_list_filters(queryset).values_list('pk', flat=True)) == {other.pk}
