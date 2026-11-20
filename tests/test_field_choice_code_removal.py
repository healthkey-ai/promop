"""Removing a coded association must not remove its allowed value or other codes."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldChoice, FieldChoiceCode, GroupAccess, Organization
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db


@pytest.fixture
def choice():
    return FieldChoice.objects.create(field_name='disease_activity', display='Active', sort_order=7)


@pytest.fixture
def code(choice):
    return FieldChoiceCode.objects.create(
        choice=choice, vocabulary_id='SNOMED', code='55561003', is_primary=True,
    )


@pytest.fixture
def client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='choice-staff@example.com', is_staff=True))
    return client


def test_remove_one_code_preserves_choice_and_other_codes(client, choice, code):
    other = FieldChoiceCode.objects.create(choice=choice, vocabulary_id='LOCAL', code='active')
    created_at = choice.created_at
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/?code_id={code.pk}')
    assert response.status_code == 204
    choice.refresh_from_db()
    assert (choice.display, choice.sort_order, choice.created_at) == ('Active', 7, created_at)
    assert not FieldChoiceCode.objects.filter(pk=code.pk).exists()
    assert list(choice.codes.values_list('pk', flat=True)) == [other.pk]
    response = client.get('/api/v1/field-choices/?field_name=disease_activity')
    assert response.status_code == 200
    saved = next(row for row in response.data if row['id'] == choice.pk)
    assert [item['id'] for item in saved['codes']] == [other.pk]


def test_remove_last_code_then_add_corrected_code_without_recreating_choice(client, choice, code):
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/?code_id={code.pk}')
    assert response.status_code == 204
    response = client.get(f'/api/v1/field-choices/{choice.pk}/')
    assert response.status_code == 200
    assert response.data['display'] == 'Active'
    assert response.data['codes'] == []
    response = client.post(f'/api/v1/field-choices/{choice.pk}/codes/', {
        'vocabulary_id': 'LOCAL', 'code': 'active', 'is_primary': True,
    }, format='json')
    assert response.status_code == 201
    choice.refresh_from_db()
    assert (choice.display, choice.sort_order) == ('Active', 7)
    assert choice.codes.get().code == 'active'


@pytest.mark.parametrize('code_id', ['', 'bad', '0', '-1', '1.5'])
def test_invalid_code_id_never_falls_back_to_remove_all(client, choice, code, code_id):
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/?code_id={code_id}')
    assert response.status_code == 400
    assert 'code_id' in response.data
    assert choice.codes.filter(pk=code.pk).exists()


def test_code_from_another_choice_cannot_be_removed(client, choice, code):
    other_choice = FieldChoice.objects.create(field_name='disease_activity', display='Inactive')
    other_code = FieldChoiceCode.objects.create(choice=other_choice, vocabulary_id='LOCAL', code='inactive')
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/?code_id={other_code.pk}')
    assert response.status_code == 404
    assert choice.codes.filter(pk=code.pk).exists()
    assert other_choice.codes.filter(pk=other_code.pk).exists()


def test_missing_code_returns_not_found_without_deleting_others(client, choice, code):
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/?code_id={code.pk + 1000000}')
    assert response.status_code == 404
    assert choice.codes.filter(pk=code.pk).exists()


@pytest.mark.parametrize('role', ['reader', 'anonymous', 'org_admin'])
def test_code_removal_uses_existing_curator_permissions(choice, code, role):
    client = APIClient()
    if role != 'anonymous':
        user = Identity.objects.create_user(email=f'choice-{role}@example.com')
        if role == 'org_admin':
            org = Organization.objects.create(name='Choice Org', slug='choice-removal-org')
            GroupAccess.objects.create(identity=user, org=org, role='org_admin')
        client.force_authenticate(user)
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/?code_id={code.pk}')
    if role == 'org_admin':
        assert response.status_code == 204
        assert not choice.codes.exists()
        assert FieldChoice.objects.filter(pk=choice.pk).exists()
    else:
        assert response.status_code in (401, 403)
        assert choice.codes.filter(pk=code.pk).exists()


def test_existing_remove_all_codes_keeps_choice(client, choice, code):
    response = client.delete(f'/api/v1/field-choices/{choice.pk}/codes/')
    assert response.status_code == 204
    assert FieldChoice.objects.filter(pk=choice.pk).exists()
    assert not choice.codes.exists()
