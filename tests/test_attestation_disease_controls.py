"""The disease tab and the controls that bypass `Field` count for the attestation (#2761).

A patient's "Confirm my record" is refused while a marked field the page shows and the
patient can edit is on display. The disease tab shows one section, for the disease the
page reads from the disease text, so a field of that section blocks only on a record
with that disease; the treating institution picker and the GELF checklist render
outside `Field` but are named in the tabs like any other field, so they block too.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    GroupAccess, Organization, Person, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
)
from patient_portal.models import Identity, PatientUser

pytestmark = pytest.mark.django_db

CONFIRM = '/api/patient-info/me/confirm/'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''


def make_patient(disease=None):
    org = Organization.objects.create(name='Attest Disease Org', slug='attest-disease-org')
    person = Person.objects.create(person_id=88802)
    PatientRecord.objects.create(person=person, organization=org, disease=disease)
    identity = Identity.objects.create_user(email='patient@attest-disease.test', password='pw')
    GroupAccess.objects.create(identity=identity, org=org, role='patient')
    PatientUser.objects.create(identity=identity, person=person, is_active=True)
    client = APIClient()
    client.force_authenticate(identity)
    return client, person


def propose(person, **fields):
    values = {name: value for name, (value, _mark) in fields.items()}
    marks = {name: mark for name, (_value, mark) in fields.items()}
    PatientRecordProvisional.objects.update_or_create(person=person, defaults=values)
    PatientRecordConfidence.objects.update_or_create(person=person, defaults=marks)


def refusal(client):
    """The status and the blocking fields the confirmation answers with."""
    response = client.post(CONFIRM)
    return response.status_code, response.json().get('provisional_fields', [])


def test_a_field_of_the_disease_section_on_screen_blocks():
    client, person = make_patient('Follicular Lymphoma')
    propose(person, b_symptom_fever=(True, 80), bulky_disease=(False, 75))

    assert refusal(client) == (409, ['b_symptom_fever', 'bulky_disease'])


def test_the_same_field_does_not_block_when_the_page_shows_another_disease_section():
    # b_symptom_fever is a lymphoma field: a myeloma record's page never renders it,
    # so a block on it could never be lifted.
    client, person = make_patient('Multiple Myeloma')
    propose(person, b_symptom_fever=(True, 80))

    assert client.post(CONFIRM).status_code == 200


def test_a_record_with_no_disease_has_no_disease_section_to_block():
    client, person = make_patient(None)
    propose(person, b_symptom_fever=(True, 80), binet_stage=('A', 80))

    assert client.post(CONFIRM).status_code == 200


def test_each_disease_blocks_on_its_own_section_only():
    client, person = make_patient('Chronic lymphocytic leukemia')
    propose(person, binet_stage=('A', 80), b_symptom_fever=(True, 80))

    assert refusal(client) == (409, ['binet_stage'])


def test_a_proposed_disease_picks_the_section_that_is_on_screen():
    # The page shows the proposed disease, so it shows that disease's section too.
    client, person = make_patient(None)
    propose(person, disease=('Follicular Lymphoma', 88), b_symptom_fever=(True, 80))

    assert refusal(client) == (409, ['b_symptom_fever', 'disease'])


def test_the_treating_institution_is_never_proposed_so_it_never_blocks():
    # Identity and contact fields are never overlaid (`NEVER_OVERLAID`), so the picker
    # cannot show a proposal and needs no indicator or block.
    client, person = make_patient(None)
    propose(person, facility_name=('Example Cancer Center', 78))

    assert client.post(CONFIRM).status_code == 200


def test_the_gelf_checklist_blocks_on_a_lymphoma_record():
    client, person = make_patient('Follicular Lymphoma')
    propose(person, gelf_criteria_options=('large_mass,b_symptoms', 80))

    assert refusal(client) == (409, ['gelf_criteria_options'])


def test_entering_the_gelf_checklist_clears_the_block():
    client, person = make_patient('Follicular Lymphoma')
    propose(person, gelf_criteria_options=('large_mass,b_symptoms', 80))
    assert client.post(CONFIRM).status_code == 409

    patch = client.patch(
        '/api/patient-info/me/', {'gelf_criteria_options': 'large_mass,b_symptoms'}, format='json')

    assert patch.status_code == 200
    marks = client.get('/api/patient-info/me/').json()['patient_info']['provisional_fields']
    assert 'gelf_criteria_options' not in marks
    assert client.post(CONFIRM).status_code == 200


def test_the_gelf_checklist_does_not_block_where_the_page_does_not_show_it():
    client, person = make_patient('Multiple Myeloma')
    propose(person, gelf_criteria_options=('large_mass', 80))

    assert client.post(CONFIRM).status_code == 200


def test_the_flipi_score_follows_its_checklist_and_does_not_block_alone():
    # The score the FLIPI control shows is calculated from the factor checklist, so
    # it has no write path of its own and clears with the checklist.
    client, person = make_patient('Follicular Lymphoma')
    propose(person, flipi_score=(3, 80))

    assert client.post(CONFIRM).status_code == 200


def test_a_disease_field_the_descriptor_does_not_know_blocks(monkeypatch):
    client, person = make_patient('Follicular Lymphoma')
    propose(person, b_symptom_fever=(True, 80))
    monkeypatch.setattr(
        'omop_core.services.write_descriptor.build_writable_field_descriptor', lambda: {})

    assert refusal(client) == (409, ['b_symptom_fever'])


def test_with_the_switch_off_the_disease_tab_blocks_nothing(settings):
    client, person = make_patient('Follicular Lymphoma')
    propose(person, b_symptom_fever=(True, 80))
    settings.PROVISIONAL_DATA_ENABLED = False

    assert client.post(CONFIRM).status_code == 200
