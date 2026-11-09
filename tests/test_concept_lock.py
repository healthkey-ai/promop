"""A curator's claim on a concept (#2390): lock, review-only for others, release."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import ConceptMappingLock, FieldConceptMapping, SourceCodeConceptMapping
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db
BASE = '/api/v1/concept-to-code/'


def curator(django_user_model, email, **extra):
    client = APIClient()
    client.user = django_user_model.objects.create_user(email=email, is_staff=False, **extra)
    from omop_core.models import GroupAccess, Organization
    org, _ = Organization.objects.get_or_create(slug='lock-org', defaults={'name': 'Lock Org'})
    GroupAccess.objects.create(identity=client.user, org=org, role='org_admin')
    client.force_authenticate(client.user)
    return client


@pytest.fixture
def concept():
    concept = ConceptFactory(concept_name='Serum albumin', concept_code='ALBUMIN')
    FieldConceptMapping.objects.create(concept=concept, field_name='albumin', omop_table='measurement')
    return concept


@pytest.fixture
def alice(django_user_model):
    return curator(django_user_model, 'alice@test.com')


@pytest.fixture
def bob(django_user_model):
    return curator(django_user_model, 'bob@test.com')


def source(code, **overrides):
    return SourceCodeConceptMapping.objects.create(**{
        'source_code': code, 'source_vocabulary_id': 'SNOMED', 'source_code_description': 'Serum albumin',
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'occurrence_count': 1, **overrides,
    })


def test_lock_shows_on_the_row_with_the_holders_name_and_who_owns_it(alice, bob, concept):
    response = alice.post(f'{BASE}{concept.pk}/lock/')
    assert response.status_code == 200, response.data
    assert response.data['lock'] == {'username': 'alice@test.com', 'mine': True,
                                     'locked_at': response.data['lock']['locked_at']}
    mine = next(r for r in alice.get(BASE).data['results'] if r['concept_id'] == concept.pk)
    theirs = next(r for r in bob.get(BASE).data['results'] if r['concept_id'] == concept.pk)
    assert mine['lock']['mine'] is True and theirs['lock']['mine'] is False
    assert theirs['lock']['username'] == 'alice@test.com'
    # Idempotent for the holder; refused for anyone else.
    assert alice.post(f'{BASE}{concept.pk}/lock/').status_code == 200
    refused = bob.post(f'{BASE}{concept.pk}/lock/')
    assert refused.status_code == 423 and refused.data['locked_by'] == 'alice@test.com'


def test_others_can_review_but_not_map_a_locked_concept(alice, bob, concept):
    row = source('S1', target_concept=concept)
    alice.post(f'{BASE}{concept.pk}/lock/')
    # Reading stays open.
    assert bob.get(f'{BASE}{concept.pk}/', {'status': 'proposed'}).status_code == 200
    # Every write for the concept is refused for Bob, naming Alice.
    apply = bob.post(f'{BASE}{concept.pk}/mappings/{row.pk}/', {
        'status': 'approved', 'expected_updated_at': row.updated_at.isoformat()}, format='json')
    assert apply.status_code == 423 and 'alice@test.com' in apply.data['detail']
    propose = bob.post(f'{BASE}{concept.pk}/mappings/', {
        'run_id': '00000000-0000-0000-0000-000000000000', 'source_vocabulary_id': 'SNOMED',
        'source_code': 'S2', 'status': 'proposed'}, format='json')
    assert propose.status_code == 423
    suggest = bob.post(f'{BASE}suggest/', {'concept_ids': [concept.pk], 'strategies': ['lexical']}, format='json')
    assert suggest.status_code == 423
    dialog = bob.patch(f'/api/v1/code-mappings/{row.pk}/', {'status': 'approved'}, format='json')
    assert dialog.status_code == 423
    row.refresh_from_db()
    assert row.status == 'proposed'
    # The holder maps as usual.
    mine = alice.post(f'{BASE}{concept.pk}/mappings/{row.pk}/', {
        'status': 'approved', 'expected_updated_at': row.updated_at.isoformat()}, format='json')
    assert mine.status_code == 200, mine.data


def test_only_the_holder_or_staff_unlocks(alice, bob, concept, django_user_model):
    alice.post(f'{BASE}{concept.pk}/lock/')
    assert bob.delete(f'{BASE}{concept.pk}/lock/').status_code == 423
    assert ConceptMappingLock.objects.filter(concept=concept).exists()
    staff = APIClient()
    staff.force_authenticate(django_user_model.objects.create_user(email='staff@test.com', is_staff=True))
    assert staff.delete(f'{BASE}{concept.pk}/lock/').status_code == 200
    assert not ConceptMappingLock.objects.filter(concept=concept).exists()
    # Releasing a free concept is a no-op, and the row shows no lock.
    assert alice.delete(f'{BASE}{concept.pk}/lock/').status_code == 200
    assert next(r for r in alice.get(BASE).data['results'] if r['concept_id'] == concept.pk)['lock'] is None


def test_lock_endpoint_needs_a_curator_and_a_standard_concept(alice, concept, django_user_model):
    assert alice.post(f'{BASE}{ConceptFactory(standard_concept=None).pk}/lock/').status_code == 404
    patient = APIClient()
    patient.force_authenticate(django_user_model.objects.create_user(email='patient-lock@test.com'))
    assert patient.post(f'{BASE}{concept.pk}/lock/').status_code == 403
