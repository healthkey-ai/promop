"""An answer that is already offered is changed by approvers (#1225).

A mapping curator (the analyst role) may add an answer, reorder them and
propose a concept; renaming, retiring, restoring, rescoping, changing spellings
or codes have no proposal stage and take effect at once, so they are for org
admins and staff, as approving a mapping is.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    DiseaseCategoryChoice, FieldChoice, FieldChoiceAlias, FieldChoiceCode, FieldChoiceDisease, GroupAccess,
)
from patient_portal.models import Identity
from tests.factories import ConceptFactory, DomainFactory, OrganizationFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

CHOICES = '/api/v1/field-choices/'


def client_as(role):
    if role == 'staff':
        user = Identity.objects.create_user(email='ap-staff@example.org', is_staff=True)
        OrganizationFactory()
    else:
        user = Identity.objects.create_user(email=f'ap-{role}@example.org')
        GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture(autouse=True)
def categories():
    for code in ('MM', 'FL'):
        DiseaseCategoryChoice.objects.get_or_create(code=code, defaults={'label': code})


@pytest.fixture
def choice():
    """An answer that is live: it has a code, a spelling and a scope."""
    made = FieldChoice.objects.create(field_name='stage', display='Stage IV')
    FieldChoiceCode.objects.create(choice=made, code='4', vocabulary_id='LOCAL', is_primary=True)
    FieldChoiceAlias.objects.create(choice=made, field_name='stage', alias='IV', alias_key='iv', source='curator')
    FieldChoiceDisease.objects.create(choice=made, disease='FL')
    return made


@pytest.mark.parametrize('body', (
    {'display': 'Stage 4'},
    {'retired': True},
    {'diseases': ['MM']},
    {'aliases': ['IV']},
    {'value': '4'},
    {'value_type': 'integer'},
    {'codes': []},
    {'codes': [{'code': '9', 'vocabulary_id': 'LOCAL', 'is_primary': True}]},
    {'sort_order': 3, 'retired': True},
))
def test_an_analyst_cannot_change_a_live_answer(choice, body):
    response = client_as('analyst').patch(f'{CHOICES}{choice.pk}/', body, format='json')
    assert response.status_code == 403
    assert 'org admins and staff' in response.data['detail']
    choice.refresh_from_db()
    assert (choice.display, choice.retired_at, choice.sort_order, choice.value, choice.value_type) == (
        'Stage IV', None, 0, '', 'text')
    assert sorted(choice.aliases.values_list('alias', flat=True)) == ['IV']
    assert list(choice.disease_scope.values_list('disease', flat=True)) == ['FL']
    assert list(choice.codes.values_list('code', flat=True)) == ['4']


def test_an_analyst_can_reorder(choice):
    response = client_as('analyst').patch(f'{CHOICES}{choice.pk}/', {'sort_order': 5}, format='json')
    assert response.status_code == 200
    choice.refresh_from_db()
    assert choice.sort_order == 5


@pytest.mark.parametrize('role', ('staff', 'org_admin'))
def test_approvers_change_a_live_answer(choice, role):
    client = client_as(role)
    response = client.patch(
        f'{CHOICES}{choice.pk}/',
        {'display': 'Stage 4', 'diseases': ['MM'], 'aliases': ['IV'], 'retired': True}, format='json')
    assert response.status_code == 200, response.data
    choice.refresh_from_db()
    assert choice.display == 'Stage 4' and choice.retired_at is not None
    assert FieldChoiceDisease.objects.filter(choice=choice, disease='MM').exists()
    assert FieldChoiceAlias.objects.filter(choice=choice, alias='IV').exists()


def test_a_doctor_still_cannot_edit_anything(choice):
    assert client_as('doctor').patch(f'{CHOICES}{choice.pk}/', {'sort_order': 5}, format='json').status_code == 403


def test_an_analyst_adds_a_plain_answer_but_not_one_with_scope_spellings_or_codes():
    analyst = client_as('analyst')
    plain = analyst.post(CHOICES, {'field_name': 'stage', 'display': 'Stage V', 'sort_order': 9}, format='json')
    assert plain.status_code == 201
    for extra in ({'diseases': ['MM']}, {'aliases': ['V']}, {'retired': True},
                  {'codes': [{'code': '1', 'vocabulary_id': 'LOCAL', 'is_primary': True}]}):
        response = analyst.post(CHOICES, {'field_name': 'stage', 'display': 'Stage VI', **extra}, format='json')
        assert response.status_code == 403, extra
    assert list(FieldChoice.objects.values_list('display', flat=True)) == ['Stage V']


def test_an_approver_adds_an_answer_with_scope_and_codes():
    response = client_as('org_admin').post(CHOICES, {
        'field_name': 'stage', 'display': 'Stage V', 'diseases': ['FL'],
        'codes': [{'code': '1', 'vocabulary_id': 'LOCAL', 'is_primary': True}],
    }, format='json')
    assert response.status_code == 201, response.data
    made = FieldChoice.objects.get(display='Stage V')
    assert made.codes.count() == 1 and list(made.disease_scope.values_list('disease', flat=True)) == ['FL']


def test_codes_are_for_approvers(choice):
    code = FieldChoiceCode.objects.create(choice=choice, code='1', vocabulary_id='LOCAL', is_primary=True)
    before = choice.codes.count()
    analyst = client_as('analyst')
    body = {'code': '2', 'vocabulary_id': 'LOCAL', 'is_primary': False}
    assert analyst.post(f'{CHOICES}{choice.pk}/codes/', body, format='json').status_code == 403
    assert analyst.delete(f'{CHOICES}{choice.pk}/codes/?code_id={code.pk}').status_code == 403
    assert choice.codes.count() == before
    admin = client_as('org_admin')
    assert admin.post(f'{CHOICES}{choice.pk}/codes/', body, format='json').status_code == 201
    assert admin.delete(f'{CHOICES}{choice.pk}/codes/?code_id={code.pk}').status_code == 204


def test_an_analyst_cannot_delete_an_answer_and_add_it_again_plain(choice):
    analyst = client_as('analyst')
    assert analyst.delete(f'{CHOICES}{choice.pk}/').status_code == 403
    assert FieldChoice.objects.filter(pk=choice.pk).exists()
    assert choice.codes.count() == 1 and choice.disease_scope.count() == 1
    assert client_as('org_admin').delete(f'{CHOICES}{choice.pk}/').status_code == 204


@pytest.mark.parametrize('body', ([], [{'retired': True}]))
def test_a_body_that_is_not_an_object_changes_nothing(choice, body):
    analyst = client_as('analyst')
    assert analyst.patch(f'{CHOICES}{choice.pk}/', body, format='json').status_code in (400, 403)
    assert analyst.post(CHOICES, body, format='json').status_code in (400, 403)
    choice.refresh_from_db()
    assert choice.retired_at is None and FieldChoice.objects.count() == 1


def test_an_analyst_still_proposes_a_concept_for_an_answer(choice):
    VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED', vocabulary_version='2026-09')
    DomainFactory(domain_id='Observation', domain_name='Observation')
    ConceptFactory(vocabulary_id='SNOMED', concept_code='4', domain_id='Observation', concept_name='Stage 4')
    analyst = client_as('analyst')
    response = analyst.post(f'{CHOICES}{choice.pk}/value-mappings/',
                            {'vocabulary_id': 'SNOMED', 'concept_code': '4', 'rationale': 'ok'}, format='json')
    assert response.status_code == 201, response.data
    approve = analyst.post(f"{CHOICES}{choice.pk}/value-mappings/{response.data['id']}/approve/", {}, format='json')
    assert approve.status_code == 403
