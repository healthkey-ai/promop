"""Chronic-condition overrides: model helpers and the admin API (#2566)."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import ChronicConditionOverride, Organization
from omop_core.services.chronic_conditions import normalize_icd10cm, override_for
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db

URL = '/api/v1/chronic-condition-overrides/'


@pytest.fixture
def admin():
    Organization.objects.get_or_create(slug='test-org', defaults={'name': 'Test Org'})
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='cco-staff@example.test', is_staff=True))
    return client


@pytest.fixture
def reader():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='cco-reader@example.test', is_staff=False))
    return client


@pytest.mark.parametrize('raw, code', [
    ('E03.9', 'E03.9'), ('E039', 'E03.9'), ("'E039", 'E03.9'), (' m85.8 ', 'M85.8'),
    ('E03', 'E03'), ('M8580', 'M85.80'),
])
def test_normalize_accepts_icd10cm_in_any_spelling(raw, code):
    assert normalize_icd10cm(raw) == code


@pytest.mark.parametrize('raw', [None, '', '0', 'Osteopenia', '12345', 'E03.99999', 'E.039'])
def test_normalize_refuses_what_is_not_a_code(raw):
    assert normalize_icd10cm(raw) is None


def test_a_code_is_covered_by_itself_and_by_its_ancestors_nearest_first():
    ChronicConditionOverride.objects.create(code='M85.8', is_chronic=True)
    ChronicConditionOverride.objects.create(code='M85.85', is_chronic=False)
    ChronicConditionOverride.objects.create(code='E03', is_chronic=True)

    assert override_for('M85.89').code == 'M85.8'
    assert override_for('M85.8').code == 'M85.8'
    assert override_for('M858').code == 'M85.8', 'dotless spelling'
    assert override_for('M85.851').code == 'M85.85', 'the most specific wins'
    assert override_for('E03.9').code == 'E03'
    assert override_for('M85.9') is None
    assert override_for('M86.8') is None
    assert override_for('Osteopenia') is None and override_for(None) is None


def test_admin_creates_lists_changes_and_deletes(admin):
    created = admin.post(URL, {'code': 'e039', 'is_chronic': False, 'label': ' Hypothyroidism '}, format='json')
    assert created.status_code == 201
    assert created.data['code'] == 'E03.9' and created.data['label'] == 'Hypothyroidism'
    pk = created.data['id']

    assert [r['code'] for r in admin.get(URL).data] == ['E03.9']
    assert admin.get(URL, {'search': 'thyroid'}).data[0]['id'] == pk
    assert admin.get(URL, {'search': 'nothing'}).data == []

    patched = admin.patch(f'{URL}{pk}/', {'is_chronic': True, 'notes': 'ok'}, format='json')
    assert patched.status_code == 200 and patched.data['is_chronic'] is True and patched.data['label'] == 'Hypothyroidism'
    assert ChronicConditionOverride.objects.get(pk=pk).notes == 'ok'

    assert admin.delete(f'{URL}{pk}/').status_code == 204
    assert not ChronicConditionOverride.objects.exists()


def test_a_duplicate_code_is_refused_however_spelled(admin):
    admin.post(URL, {'code': 'E03.9', 'is_chronic': True}, format='json')
    assert admin.post(URL, {'code': 'e039', 'is_chronic': False}, format='json').status_code == 409
    other = admin.post(URL, {'code': 'E11.9', 'is_chronic': True}, format='json').data['id']
    assert admin.patch(f'{URL}{other}/', {'code': 'E039'}, format='json').status_code == 409


@pytest.mark.parametrize('body', [
    {'code': 'Osteopenia', 'is_chronic': True},
    {'code': 'E03.9'},
    {'code': 'E03.9', 'is_chronic': 'yes'},
    {'code': 'E03.9', 'is_chronic': True, 'label': 'x' * 201},
    {'is_chronic': True},
])
def test_invalid_input_is_a_400(admin, body):
    assert admin.post(URL, body, format='json').status_code == 400
    assert not ChronicConditionOverride.objects.exists()


def test_a_patch_checks_only_what_it_sends(admin):
    pk = admin.post(URL, {'code': 'E03.9', 'is_chronic': True}, format='json').data['id']
    assert admin.patch(f'{URL}{pk}/', {'is_chronic': 'no'}, format='json').status_code == 400
    assert admin.patch(f'{URL}{pk}/', {'code': 'nope'}, format='json').status_code == 400
    assert admin.patch(f'{URL}999999/', {'is_chronic': True}, format='json').status_code == 404


def test_only_an_admin_writes_while_a_signed_in_user_reads(admin, reader):
    pk = admin.post(URL, {'code': 'E03.9', 'is_chronic': True}, format='json').data['id']

    assert reader.get(URL).status_code == 200
    assert reader.post(URL, {'code': 'E11.9', 'is_chronic': True}, format='json').status_code == 403
    assert reader.patch(f'{URL}{pk}/', {'is_chronic': False}, format='json').status_code == 403
    assert reader.delete(f'{URL}{pk}/').status_code == 403
    assert ChronicConditionOverride.objects.get(pk=pk).is_chronic is True


def test_anonymous_is_refused():
    assert APIClient().get(URL).status_code in (401, 403)


def test_mapping_stats_counts_overrides(admin):
    ChronicConditionOverride.objects.create(code='E03.9', is_chronic=True)
    assert admin.get('/api/v1/mapping-stats/').data['chronic_overrides'] == {'total': 1}


def test_the_seed_adds_osteopenia_and_keeps_a_curators_row():
    from importlib import import_module

    from django.apps import apps

    seed = import_module('omop_core.migrations.0342_seed_chronic_condition_override').seed
    seed(apps, None)
    seed(apps, None)  # a replay adds nothing
    assert ChronicConditionOverride.objects.get(code='M85.8').is_chronic is True
    assert ChronicConditionOverride.objects.count() == 1

    ChronicConditionOverride.objects.filter(code='M85.8').update(is_chronic=False, label='Curated')
    seed(apps, None)
    assert ChronicConditionOverride.objects.get(code='M85.8').label == 'Curated'
