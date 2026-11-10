"""A curator's note on a concept (#2411): save, clear, the list, permission, copying."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import ConceptMappingLock, ConceptMappingNote, FieldConceptMapping
from omop_core.services.reference_transfer import apply_reference, read_reference
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db
BASE = '/api/v1/concept-to-code/'


def curator(django_user_model, email, role='org_admin'):
    from omop_core.models import GroupAccess, Organization
    client = APIClient()
    client.user = django_user_model.objects.create_user(email=email, is_staff=False)
    org, _ = Organization.objects.get_or_create(slug='note-org', defaults={'name': 'Note Org'})
    GroupAccess.objects.create(identity=client.user, org=org, role=role)
    client.force_authenticate(client.user)
    return client


def field_concept(name, code):
    concept = ConceptFactory(concept_name=name, concept_code=code)
    FieldConceptMapping.objects.create(concept=concept, field_name=code.lower(), omop_table='measurement')
    return concept


@pytest.fixture
def concept():
    return field_concept('Serum albumin', 'ALBUMIN')


@pytest.fixture
def alice(django_user_model):
    return curator(django_user_model, 'alice@test.com')


@pytest.fixture
def bob(django_user_model):
    return curator(django_user_model, 'bob@test.com')


def row(client, concept):
    return next(r for r in client.get(BASE).data['results'] if r['concept_id'] == concept.pk)


def put(client, concept, note):
    return client.put(f'{BASE}{concept.pk}/note/', {'note': note}, format='json')


def test_a_saved_note_shows_on_the_row_for_every_curator(alice, bob, concept):
    assert row(alice, concept)['note'] is None

    response = put(alice, concept, '  Parked until the LOINC reload.\n')
    assert response.status_code == 200, response.data
    assert response.data['concept_id'] == concept.pk
    assert response.data['note']['text'] == 'Parked until the LOINC reload.'
    assert response.data['note']['updated_by'] == 'alice@test.com'
    assert row(bob, concept)['note']['text'] == 'Parked until the LOINC reload.'

    # One shared note: the next curator's save replaces it and takes the byline.
    assert put(bob, concept, 'Done with Epic; Cerner still open.').status_code == 200
    note = ConceptMappingNote.objects.get(concept=concept)
    assert (note.text, note.updated_by) == ('Done with Epic; Cerner still open.', bob.user)
    assert row(alice, concept)['note']['updated_by'] == 'bob@test.com'


@pytest.mark.parametrize('clear', [
    lambda client, concept: put(client, concept, '   '),
    lambda client, concept: client.delete(f'{BASE}{concept.pk}/note/'),
])
def test_a_blank_note_or_a_delete_clears_it(alice, concept, clear):
    put(alice, concept, 'Temporary')
    response = clear(alice, concept)
    assert response.status_code == 200 and response.data == {'concept_id': concept.pk, 'note': None}
    assert not ConceptMappingNote.objects.exists()
    assert row(alice, concept)['note'] is None
    # Clearing what is already clear is not an error.
    assert clear(alice, concept).status_code == 200


def test_another_curators_lock_does_not_block_a_note(alice, bob, concept):
    ConceptMappingLock.objects.create(concept=concept, locked_by=alice.user)
    assert put(bob, concept, 'Ask Alice before mapping this.').status_code == 200
    assert row(alice, concept)['note']['text'] == 'Ask Alice before mapping this.'


@pytest.mark.parametrize('payload', [{}, {'note': None}, {'note': 7}, {'note': ['a']}, ['a'], {'note': 'a\x00b'},
                                     {'note': 'x' * (ConceptMappingNote.MAX_LENGTH + 1)}])
def test_note_endpoint_validates(alice, concept, payload):
    response = alice.put(f'{BASE}{concept.pk}/note/', payload, format='json')
    assert response.status_code == 400, response.data
    assert not ConceptMappingNote.objects.exists()


def test_note_endpoint_is_for_code_mapping_users_and_standard_concepts(alice, concept, django_user_model):
    anonymous = APIClient()
    assert put(anonymous, concept, 'x').status_code in (401, 403)
    patient = APIClient()
    patient.force_authenticate(django_user_model.objects.create_user(email='patient-note@test.com'))
    assert put(patient, concept, 'x').status_code == 403
    assert patient.delete(f'{BASE}{concept.pk}/note/').status_code == 403
    assert not ConceptMappingNote.objects.exists()
    # Every professional role uses the tab, the analyst included (#2398).
    assert put(curator(django_user_model, 'analyst@test.com', role='analyst'), concept, 'x').status_code == 200
    nonstandard = ConceptFactory(standard_concept=None)
    assert put(alice, nonstandard, 'x').status_code == 404
    assert alice.post(f'{BASE}{concept.pk}/note/', {'note': 'x'}, format='json').status_code == 405


def test_the_list_reads_notes_in_one_query_whatever_the_page_holds(alice, concept):
    def queries():
        with CaptureQueriesContext(connection) as captured:
            assert alice.get(BASE).status_code == 200
        return len(captured)

    put(alice, concept, 'first')
    one = queries()
    for index in range(3):
        put(alice, field_concept(f'Extra {index}', f'EXTRA{index}'), f'note {index}')
    assert queries() == one


def test_copy_reference_data_carries_notes_by_concept_key(alice, concept):
    put(alice, concept, 'Travels with the reference data.')
    payload = read_reference('default')
    ConceptMappingNote.objects.all().delete()

    stats = apply_reference(payload)

    note = ConceptMappingNote.objects.get(concept=concept)
    # The author is this instance's user and does not travel.
    assert (note.text, note.updated_by) == ('Travels with the reference data.', None)
    assert stats.created['ConceptMappingNote'] == 1
