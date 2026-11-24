"""Staff, an org admin or a doctor confirms a patient's provisional values (#2870).

The record shows a proposed value, marked "Provisional", over a field the approved
record leaves empty. A patient makes one real by entering it; a professional who
reads the record can do the same in one step: ``POST
/api/v1/patient-records/<id>/confirm-provisional/``. The value is the server's own
proposal, saved through the editor's PATCH path, so the result is an approved edit
by the person who pressed the button.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import (
    FieldConceptMapping, GroupAccess, Measurement, Observation, Organization, PatientRecord,
    PatientRecordConfidence, PatientRecordProvisional, Person, ProposalRejection, RecordRevision,
    SourceCodeConceptMapping,
)
from omop_core.services import provisional_confirm, provisional_record
from omop_core.services.fhir_export import build_fhir_bundle
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import AuditEvent, Identity, PatientUser
from tests.factories import PatientRecordFactory
from tests.test_provisional_composite import cancer
from tests.test_provisional_computed import _height, _weight
from tests.test_provisional_derivation import (  # noqa: F401 (fixtures)
    _add_measurement, _add_observation, _concept, record, snomed,
)
from tests.test_provisional_rejection import Editor, hemoglobin, propose_hemoglobin

pytestmark = pytest.mark.django_db

CONFIRM_ME = '/api/patient-info/me/confirm/'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''


@pytest.fixture(autouse=True)
def type_concepts(snomed):
    # The facts a confirmation projects are typed: the recipe's type, or patient self-report.
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    _concept('Type Concept', 32817, '32817', 'EHR', 'Type Concept')


def account(email, **extra):
    return Identity.objects.create_user(email=email, password='pw', **extra)


def signed_in(identity):
    client = Editor()
    client.force_login(identity)
    return client


def grant(identity, org, role):
    return GroupAccess.objects.create(identity=identity, org=org, role=role)


@pytest.fixture()
def org():
    return Organization.objects.create(name='Confirm Org', slug='confirm-org')


@pytest.fixture()
def elsewhere():
    return Organization.objects.create(name='Confirm Elsewhere', slug='confirm-elsewhere')


@pytest.fixture()
def patient_record(snomed, org):
    """A record at ``org`` that shows a proposed hemoglobin."""
    made = PatientRecordFactory(organization=org, disease=None)
    propose_hemoglobin(made.person)
    return made


@pytest.fixture()
def person(patient_record):
    return patient_record.person


def person_of(made):
    return made.person


def as_role(role, org, email=None):
    identity = account(email or f'confirm-{role}@example.test', is_staff=(role == 'staff'))
    if role != 'staff':
        grant(identity, org, role)
    return signed_in(identity), identity


@pytest.fixture()
def doctor(org):
    return as_role('doctor', org)[0]


def url(person):
    return f'/api/v1/patient-records/{person.person_id}/'


def confirm_url(person):
    return f'{url(person)}confirm-provisional/'


def confirm(client, person, **body):
    return client.post(confirm_url(person), body, format='json')


def read(client, person):
    return client.get(url(person)).json()['patient_info']


def stored(person):
    return PatientRecord.objects.get(person=person)


def marks(person):
    return PatientRecordConfidence.objects.get(person=person)


def provisional_row(person):
    return PatientRecordProvisional.objects.get(person=person)


def reasons(response):
    return {item['field']: item['reason'] for item in response.json()['skipped']}


# --- one field -------------------------------------------------------------------------

@pytest.mark.parametrize('role', ['doctor', 'org_admin', 'staff'])
def test_a_professional_confirms_one_proposal_and_it_is_approved(role, org, person):
    client, identity = as_role(role, org)
    assert read(client, person)['provisional_fields']['hemoglobin_g_dl'] == 82

    response = confirm(client, person, fields=['hemoglobin_g_dl'])

    assert response.status_code == 200, response.content
    body = response.json()
    assert 'hemoglobin_g_dl' in body['confirmed']
    assert body['person_id'] == person.person_id
    record = stored(person)
    assert float(record.hemoglobin_g_dl) == 11.5                       # the server's own proposal
    # Tracked as an edit until OMOP holds it (user_edited_fields), and here it does: the
    # write projected the approved fact, so the pending entry is acknowledged, as a PATCH's is.
    assert Measurement.objects.filter(person=person, confidence__isnull=True,
                                      measurement_source_value='718-7').exists()
    after = read(client, person)
    assert float(after['hemoglobin_g_dl']) == 11.5
    assert 'hemoglobin_g_dl' not in after['provisional_fields']        # the mark is gone
    assert marks(person).hemoglobin_g_dl is None                       # and in the pair
    assert float(provisional_row(person).hemoglobin_g_dl) == 11.5


def test_the_result_is_what_the_same_value_through_the_editor_patch_gives(snomed, org):
    via_confirm = PatientRecordFactory(organization=org, disease=None)
    via_patch = PatientRecordFactory(organization=org, disease=None)
    for made in (via_confirm, via_patch):
        propose_hemoglobin(made.person)
    client, _ = as_role('doctor', org)

    assert confirm(client, via_confirm.person, fields=['hemoglobin_g_dl']).status_code == 200
    assert client.patch(url(via_patch.person), {'hemoglobin_g_dl': '11.5'}, format='json').status_code == 200

    first, second = stored(via_confirm.person), stored(via_patch.person)
    assert first.hemoglobin_g_dl == second.hemoglobin_g_dl
    assert first.hemoglobin_level == second.hemoglobin_level          # the alias follows
    assert first.user_edited_fields == second.user_edited_fields
    assert read(client, via_confirm.person)['provisional_fields'] == read(client, via_patch.person)['provisional_fields']

    def facts(made):    # the approved facts the write projected, apart from the proposal's own row
        return sorted(Measurement.objects.filter(person=made.person, confidence__isnull=True)
                      .values_list('measurement_source_value', 'value_as_number', 'measurement_type_concept_id'))
    assert facts(via_confirm) == facts(via_patch)
    assert Measurement.objects.filter(person=via_confirm.person, confidence__isnull=True).exists()


def test_the_value_is_the_servers_proposal_never_the_clients(person, doctor):
    response = confirm(doctor, person, fields=['hemoglobin_g_dl'], hemoglobin_g_dl=99, value=99)

    assert response.status_code == 200, response.content
    assert float(stored(person).hemoglobin_g_dl) == 11.5


def test_a_field_whose_proposal_changed_since_the_page_read_it_is_not_confirmed(person, doctor):
    response = confirm(doctor, person, fields=['hemoglobin_g_dl'], expected={'hemoglobin_g_dl': '9.0'})

    assert response.status_code == 409
    assert reasons(response) == {'hemoglobin_g_dl': 'changed'}
    assert response.json()['confirmed'] == []
    assert stored(person).hemoglobin_g_dl is None
    assert read(doctor, person)['provisional_fields']['hemoglobin_g_dl'] == 82       # still a proposal


def test_a_matching_expected_value_is_confirmed(person, doctor):
    shown = read(doctor, person)['hemoglobin_g_dl']

    response = confirm(doctor, person, fields=['hemoglobin_g_dl'], expected={'hemoglobin_g_dl': shown})

    assert response.status_code == 200, response.content
    assert response.json()['confirmed'] == ['hemoglobin_g_dl']


# --- what is not confirmed, and why ------------------------------------------------------

def test_named_fields_that_cannot_be_confirmed_say_why(snomed, org):
    made = PatientRecordFactory(organization=org, disease=None, blood_type='A+')
    with suppress_patient_record_refresh():
        _weight(made.person, 70, conf=82)
        _height(made.person, 170, conf=75)
    propose_hemoglobin(made.person)
    propose = made.person
    ProposalRejection.objects.create(person=propose, field_name='sodium_meq_l',
                                     origins=[['measurement', 'NA', 1]])
    client, _ = as_role('doctor', org)
    assert {'bmi', 'weight', 'weight_units'} <= set(read(client, propose)['provisional_fields'])

    response = confirm(client, propose, fields=[
        'bmi', 'weight_units', 'blood_type', 'sodium_meq_l', 'no_such_field', 'prior_procedures', 'save'])

    assert response.status_code == 200, response.content
    assert reasons(response) == {
        'bmi': 'calculated', 'weight_units': 'unit', 'blood_type': 'not_provisional',
        'sodium_meq_l': 'rejected', 'no_such_field': 'not_provisional', 'prior_procedures': 'not_provisional',
        'save': 'not_provisional'}
    assert response.json()['confirmed'] == []
    assert stored(propose).weight is None and stored(propose).user_edited_fields in ([], None)


def test_a_field_the_record_already_holds_is_already_approved(person, doctor):
    PatientRecord.objects.filter(person=person).update(hemoglobin_g_dl=13)

    response = confirm(doctor, person, fields=['hemoglobin_g_dl'])

    assert response.status_code == 200
    assert reasons(response) == {'hemoglobin_g_dl': 'already_approved'}
    assert float(stored(person).hemoglobin_g_dl) == 13                    # nothing overwritten


def test_confirm_all_takes_the_scalars_and_reports_the_rest(snomed, org):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _weight(made.person, 70, conf=82)
        _height(made.person, 170, conf=75)
    propose_hemoglobin(made.person)
    ProposalRejection.objects.create(person=made.person, field_name='sodium_meq_l',
                                     origins=[['measurement', 'NA', 1]])
    client, _ = as_role('doctor', org)
    before = read(client, made.person)['provisional_fields']
    assert {'bmi', 'weight', 'height', 'hemoglobin_g_dl'} <= set(before)

    response = confirm(client, made.person, all=True)

    assert response.status_code == 200, response.content
    body = response.json()
    assert {'weight', 'height', 'hemoglobin_g_dl'} <= set(body['confirmed'])
    skipped = reasons(response)
    assert skipped['bmi'] == 'calculated' and skipped['weight_units'] == 'unit'
    record = stored(made.person)
    assert float(record.weight) == 70 and float(record.height) == 170 and float(record.hemoglobin_g_dl) == 11.5
    assert record.bmi is not None                                          # calculated from the approved inputs
    after = read(client, made.person)['provisional_fields']
    assert not {'weight', 'height', 'hemoglobin_g_dl', 'bmi', 'weight_units'} & set(after)
    assert body['remaining'] == len(after)
    assert not ProposalRejection.objects.filter(field_name='hemoglobin_g_dl').exists()
    assert ProposalRejection.objects.filter(field_name='sodium_meq_l').exists()   # a held rejection is left alone


def test_confirm_all_with_expected_skips_a_proposal_the_page_never_listed(snomed, org):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _weight(made.person, 70, conf=82)
    propose_hemoglobin(made.person)
    client, _ = as_role('doctor', org)
    shown = read(client, made.person)

    response = confirm(client, made.person, all=True, expected={'weight': shown['weight']})

    assert response.status_code == 200, response.content
    assert response.json()['confirmed'] == ['weight']
    assert reasons(response)['hemoglobin_g_dl'] == 'not_seen'
    assert stored(made.person).hemoglobin_g_dl is None


def test_a_list_field_is_skipped_and_left_a_proposal(snomed, org):
    from tests.test_provisional_composite import drug  # noqa: F401
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _add_procedure(made.person)
    refresh_patient_record(made.person)
    client, _ = as_role('doctor', org)
    shown = read(client, made.person)
    listed = [field for field in shown['provisional_fields'] if field in shown.get('provisional_items', {})]
    assert listed, 'the fixture proposes a list'

    response = confirm(client, made.person, all=True)

    assert response.status_code == 200, response.content
    assert all(reasons(response)[field] == 'list_field' for field in listed)
    assert set(listed) <= set(read(client, made.person)['provisional_fields'])


def _add_procedure(person):
    from tests.test_provisional_derivation import _add_procedure as add
    return add(person, 72)


def test_a_disease_is_confirmed_with_its_group(snomed, org):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        cancer(made.person, 82)
    refresh_patient_record(made.person)
    client, _ = as_role('doctor', org)
    shown = read(client, made.person)['provisional_fields']
    assert {'disease', 'diagnosis_date'} <= set(shown)

    response = confirm(client, made.person, fields=['disease'])

    assert response.status_code == 200, response.content
    assert {'disease', 'diagnosis_date'} <= set(response.json()['confirmed'])
    record = stored(made.person)
    assert 'squamous' in record.disease.lower() and record.diagnosis_date is not None
    assert not set(provisional_record.DISEASE_FIELDS) & set(read(client, made.person)['provisional_fields'])


def test_a_dates_precision_is_confirmed_with_its_date_and_not_alone(snomed, org):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        cancer(made.person, 82)
    refresh_patient_record(made.person)
    client, _ = as_role('doctor', org)
    assert 'diagnosis_date_precision' in read(client, made.person)['provisional_fields']

    alone = confirm(client, made.person, fields=['diagnosis_date_precision'])
    assert reasons(alone) == {'diagnosis_date_precision': 'follows_field'}

    together = confirm(client, made.person, fields=['diagnosis_date'])
    assert {'diagnosis_date', 'diagnosis_date_precision'} <= set(together.json()['confirmed'])


def test_a_value_the_records_validation_refuses_is_skipped_not_fatal(person, doctor, monkeypatch):
    from patient_portal.api import serializers
    def refuse(self, value):
        raise serializers.serializers.ValidationError('Hemoglobin is out of range.')

    monkeypatch.setattr(serializers.PatientRecordSerializer, 'validate_hemoglobin_g_dl', refuse, raising=False)
    response = confirm(doctor, person, fields=['hemoglobin_g_dl'])

    assert response.status_code == 200, response.content
    assert reasons(response) == {'hemoglobin_g_dl': 'invalid'}
    assert 'out of range' in response.json()['skipped'][0]['detail']
    assert stored(person).hemoglobin_g_dl is None


# --- who may ---------------------------------------------------------------------------

def test_an_analyst_is_refused(person, org):
    client, _ = as_role('analyst', org)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 404     # no patient access at all
    assert confirm(client, person, all=True).status_code == 404
    assert stored(person).hemoglobin_g_dl is None


def test_a_doctor_at_another_org_is_refused(person, elsewhere):
    client, _ = as_role('doctor', elsewhere, email='confirm-other@example.test')

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 404
    assert stored(person).hemoglobin_g_dl is None
    assert not AuditEvent.objects.filter(detail__has_key='provisional_confirm').exists()


def test_an_org_admin_of_another_org_is_refused(person, elsewhere):
    client, _ = as_role('org_admin', elsewhere, email='confirm-other-admin@example.test')

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 404
    assert stored(person).hemoglobin_g_dl is None


def make_patient(org, person, email='confirm-patient@example.test'):
    identity = account(email)
    grant(identity, org, 'patient')
    PatientUser.objects.create(identity=identity, person=person, is_active=True)
    return signed_in(identity), identity


def test_a_patient_cannot_use_it_on_their_own_record_and_keeps_their_own_path(person, org):
    client, _ = make_patient(org, person)
    assert client.post(CONFIRM_ME).status_code == 409                      # the attestation is blocked by the proposal

    response = confirm(client, person, fields=['hemoglobin_g_dl'])

    assert response.status_code == 403
    assert stored(person).hemoglobin_g_dl is None
    assert 'provisional_confirm' not in read(client, person)               # nothing offered to them either
    # Their own route still works: entering the value is the patient's own act.
    assert client.patch(url(person), {'hemoglobin_g_dl': '11.5'}, format='json').status_code == 200


def test_a_patient_cannot_use_it_on_another_person(snomed, org, person):
    other = PatientRecordFactory(organization=org, disease=None)
    propose_hemoglobin(other.person)
    client, _ = make_patient(org, person)

    assert confirm(client, other.person, fields=['hemoglobin_g_dl']).status_code == 404
    assert stored(other.person).hemoglobin_g_dl is None


def test_a_doctor_who_is_also_the_patient_confirms_their_own_record_as_a_patient(person, org):
    identity = account('confirm-both@example.test')
    grant(identity, org, 'doctor')
    PatientUser.objects.create(identity=identity, person=person, is_active=True)

    assert confirm(signed_in(identity), person, fields=['hemoglobin_g_dl']).status_code == 403


def test_a_machine_credential_is_refused(settings, person):
    secret = 'confirm-provisional-service-secret-00000000'
    settings.SERVICE_AUTH_TOKENS = {'etl': {'token': secret, 'scopes': 'patient/*.read patient/*.write system/etl.write'}}
    settings.SERVICE_AUTH_TOKEN = ''
    machine = APIClient()
    machine.credentials(HTTP_AUTHORIZATION=f'Bearer {secret}')

    assert machine.get(url(person)).status_code == 200, 'the token reads patients'
    assert confirm(machine, person, fields=['hemoglobin_g_dl']).status_code == 403
    assert confirm(machine, person, all=True).status_code == 403
    assert stored(person).hemoglobin_g_dl is None


def test_a_credential_that_is_not_a_person_at_the_page_is_refused(person, org):
    identity = account('confirm-token@example.test', is_staff=True)
    token_client = APIClient()
    token_client.force_authenticate(user=identity)            # an authenticated credential, not a session

    assert confirm(token_client, person, fields=['hemoglobin_g_dl']).status_code == 403
    assert stored(person).hemoglobin_g_dl is None


def test_the_signed_out_are_refused(person):
    assert confirm(APIClient(), person, fields=['hemoglobin_g_dl']).status_code in (401, 403)


def test_an_unknown_person_is_not_found(doctor):
    assert doctor.post('/api/v1/patient-records/987654321/confirm-provisional/',
                       {'all': True}, format='json').status_code == 404


@pytest.mark.parametrize('body', [{}, {'fields': []}, {'fields': 'hemoglobin_g_dl'}, {'fields': [1]},
                                  {'all': 'yes'}, {'all': True, 'fields': ['bmi']},
                                  {'fields': ['bmi'], 'expected': ['x']}])
def test_a_malformed_request_is_a_400_and_writes_nothing(body, person, doctor):
    assert confirm(doctor, person, **body).status_code == 400
    assert stored(person).hemoglobin_g_dl is None


# --- the record read ---------------------------------------------------------------------

def test_the_read_tells_a_professional_which_fields_the_server_will_confirm(snomed, org):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _weight(made.person, 70, conf=82)
        _height(made.person, 170, conf=75)
    propose_hemoglobin(made.person)
    client, _ = as_role('doctor', org)

    block = read(client, made.person)['provisional_confirm']

    assert {'weight', 'height', 'hemoglobin_g_dl'} <= set(block['confirmable'])
    assert block['skipped']['bmi'] == 'calculated' and block['skipped']['weight_units'] == 'unit'
    assert not set(block['confirmable']) & set(block['skipped'])


@pytest.mark.parametrize('role', ['org_admin', 'staff'])
def test_the_read_offers_it_to_org_admins_and_staff_too(role, org, person):
    client, _ = as_role(role, org)

    assert 'hemoglobin_g_dl' in read(client, person)['provisional_confirm']['confirmable']


def test_the_read_offers_nothing_to_a_patient_a_credential_or_when_nothing_is_provisional(person, org, snomed):
    patient_client, _ = make_patient(org, person)
    assert 'provisional_confirm' not in read(patient_client, person)

    token_client = APIClient()
    token_client.force_authenticate(user=account('confirm-read-token@example.test', is_staff=True))
    assert 'provisional_confirm' not in read(token_client, person)

    quiet = PatientRecordFactory(organization=org, disease=None)
    client, _ = as_role('doctor', org)
    assert 'provisional_confirm' not in read(client, quiet.person)


def test_the_legacy_route_has_no_confirm_endpoint(person, org):
    client, _ = as_role('doctor', org)

    response = client.post(f'/api/patient-info/{person.person_id}/confirm-provisional/', {'all': True}, format='json')

    assert response.status_code in (404, 405)      # nothing there answers a POST
    assert stored(person).hemoglobin_g_dl is None


def test_the_legacy_read_is_unchanged(person, org):
    client, _ = as_role('doctor', org)

    shown = client.get(f'/api/patient-info/{person.person_id}/').json()['patient_info']

    assert 'provisional_fields' in shown and 'provisional_confirm' not in shown


# --- the switch, repeats, atomicity ---------------------------------------------------------

def test_with_the_switch_off_nothing_happens(person, doctor, settings):
    settings.PROVISIONAL_DATA_ENABLED = False

    response = confirm(doctor, person, fields=['hemoglobin_g_dl'])
    everything = confirm(doctor, person, all=True)

    assert response.status_code == 200 and reasons(response) == {'hemoglobin_g_dl': 'disabled'}
    assert everything.status_code == 200 and everything.json()['confirmed'] == []
    assert stored(person).hemoglobin_g_dl is None
    assert not RecordRevision.objects.filter(patient_record__person=person).exists()
    assert 'provisional_confirm' not in read(doctor, person)


def test_confirming_again_changes_nothing(person, doctor):
    assert confirm(doctor, person, all=True).status_code == 200
    revisions = RecordRevision.objects.filter(patient_record__person=person).count()
    edited = stored(person).user_edited_fields

    again = confirm(doctor, person, all=True)

    assert again.status_code == 200
    assert again.json()['confirmed'] == [] and again.json()['remaining'] == 0
    named = confirm(doctor, person, fields=['hemoglobin_g_dl'])
    assert reasons(named) == {'hemoglobin_g_dl': 'already_approved'}
    assert RecordRevision.objects.filter(patient_record__person=person).count() == revisions
    assert stored(person).user_edited_fields == edited


def test_a_failure_part_way_rolls_everything_back(snomed, org, monkeypatch):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _weight(made.person, 70, conf=82)
    propose_hemoglobin(made.person)
    client, _ = as_role('doctor', org)
    client.raise_request_exception = False
    from patient_portal.api import views

    def boom(person, record, patch, **kwargs):
        record.hemoglobin_g_dl = 1
        record.save(update_fields=['hemoglobin_g_dl'])
        raise RuntimeError('projection failed')

    monkeypatch.setattr(views, 'apply_patient_record_patch', boom)
    response = confirm(client, made.person, all=True)

    assert response.status_code == 500
    record = stored(made.person)
    assert record.hemoglobin_g_dl is None and record.weight is None            # the partial write rolled back
    assert read(client, made.person)['provisional_fields']['hemoglobin_g_dl'] == 82
    assert not RecordRevision.objects.filter(patient_record__person=made.person).exists()


def test_the_work_beyond_the_editors_own_write_does_not_grow_with_the_number_of_fields(snomed, org):
    """Each approved field costs what the editor's PATCH spends projecting it (one OMOP
    fact each); everything the confirmation adds on top is the same for 2 fields or 5."""
    def build(extra):
        made = PatientRecordFactory(organization=org, disease=None)
        with suppress_patient_record_refresh():
            _weight(made.person, 70, conf=82)
            _height(made.person, 170, conf=75)
            for index in range(extra):
                _add_lab(made.person, index)
        refresh_patient_record(made.person)
        return made.person

    def overhead(extra):
        confirmed_person, patched_person = build(extra), build(extra)
        client, _ = as_role('doctor', org, email=f'confirm-count-{extra}@example.test')
        shown = read(client, confirmed_person)
        with CaptureQueriesContext(connection) as through_confirm:
            response = confirm(client, confirmed_person, all=True)
        assert response.status_code == 200, response.content
        confirmed = response.json()['confirmed']
        with CaptureQueriesContext(connection) as through_patch:
            assert client.patch(url(patched_person), {f: shown[f] for f in confirmed}, format='json').status_code == 200
        return len(confirmed), len(through_confirm) - len(through_patch)

    few, few_overhead = overhead(0)
    many, many_overhead = overhead(4)

    assert many >= few + 3, (few, many)
    assert abs(many_overhead - few_overhead) <= 2, (few_overhead, many_overhead)


LABS = [('2951-2', 'sodium_meq_l', 'Sodium', 140), ('2823-3', 'potassium_meq_l', 'Potassium', 4.1),
        ('2075-0', 'chloride_meq_l', 'Chloride', 101), ('17861-6', 'calcium_mg_dl', 'Calcium', 9.4)]


def _add_lab(person, index):
    from tests.factories import MeasurementFactory
    code, _field, name, value = LABS[index]
    concept = _concept('Measurement', 3100000 + index, code, f'{name} [Moles/volume] in Serum', 'LOINC')
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_date=__import__('datetime').date(2024, 6, 1),
        value_as_number=value, measurement_source_value=code, unit_source_value='mmol/L',
        value_source_value=str(value), confidence=80)


# --- provenance ------------------------------------------------------------------------------

def test_the_revision_history_names_who_confirmed_and_it_is_not_the_patient(snomed, org, person):
    patient_client, patient = make_patient(org, person)
    client, doctor = as_role('doctor', org)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 200

    revisions = list(RecordRevision.objects.filter(patient_record__person=person, field='hemoglobin_g_dl'))
    assert len(revisions) == 1
    assert revisions[0].changed_by == str(doctor.pk) != str(patient.pk)
    assert revisions[0].old_value is None and float(revisions[0].new_value) == 11.5


def test_the_audit_trail_records_the_confirmation_without_values(person, org):
    client, doctor = as_role('doctor', org)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 200

    event = AuditEvent.objects.filter(path__endswith='confirm-provisional/', user_id=str(doctor.pk)).get()
    assert event.method == 'POST' and event.status_code == 200
    assert event.resource_id == str(person.person_id)
    assert event.detail['provisional_confirm']['fields'] == ['hemoglobin_g_dl']
    assert '11.5' not in str(event.detail)


# --- what it must not do -------------------------------------------------------------------

def test_it_never_sets_the_attestation(person, org):
    PatientRecord.objects.filter(person=person).update(validated=False)
    client, _ = as_role('doctor', org)

    assert confirm(client, person, all=True).status_code == 200
    assert confirm(client, person, fields=['validated', 'validated_by']).status_code == 200

    record = stored(person)
    assert not record.validated and not record.validated_by and record.validation_date is None
    person_row = Person.objects.get(pk=person.pk)
    assert not person_row.validated


def test_it_changes_no_mapping_and_no_clinical_row(person, org):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='718-7', status='proposed', domain_id='Measurement',
        omop_table='measurement', source_code_description='Hemoglobin')
    fields_before = list(FieldConceptMapping.objects.values_list('pk', 'status', 'concept_id'))
    mapping_before = list(SourceCodeConceptMapping.objects.values_list('pk', 'status', 'target_concept_id', 'updated_at'))
    proposals = list(Measurement.objects.filter(person=person, confidence__isnull=False)
                     .values_list('pk', 'value_as_number', 'confidence', 'measurement_concept_id'))
    observations = list(Observation.objects.filter(person=person).values_list('pk', flat=True))
    client, _ = as_role('doctor', org)

    assert confirm(client, person, all=True).status_code == 200

    assert list(SourceCodeConceptMapping.objects.values_list('pk', 'status', 'target_concept_id', 'updated_at')) == mapping_before
    assert list(FieldConceptMapping.objects.values_list('pk', 'status', 'concept_id')) == fields_before
    assert SourceCodeConceptMapping.objects.get(pk=mapping.pk).status == 'proposed'
    assert list(Measurement.objects.filter(person=person, confidence__isnull=False)
                .values_list('pk', 'value_as_number', 'confidence', 'measurement_concept_id')) == proposals
    assert list(Observation.objects.filter(person=person).values_list('pk', flat=True)) == observations


def test_the_proposals_row_is_never_exported_only_what_the_confirmation_approved(person, org):
    def exported():
        return [e['resource'] for e in build_fhir_bundle(person)['entry']
                if e['resource']['resourceType'] == 'Observation']
    assert Measurement.objects.filter(person=person, confidence__isnull=False).count() == 1
    assert exported() == []                                  # a shown proposal is not exported
    client, _ = as_role('doctor', org)

    assert confirm(client, person, all=True).status_code == 200

    # The proposal's own row is still a proposal and never leaves. The one fact in the
    # export is the approved one the confirmation wrote, as an edit in the editor writes it.
    assert Measurement.objects.filter(person=person, confidence__isnull=False).count() == 1
    approved = Measurement.objects.filter(person=person, confidence__isnull=True)
    assert approved.count() == 1
    assert len(exported()) == 1


def test_it_touches_only_the_named_person(snomed, org):
    mine = PatientRecordFactory(organization=org, disease=None)
    theirs = PatientRecordFactory(organization=org, disease=None)
    for made in (mine, theirs):
        propose_hemoglobin(made.person)
    client, _ = as_role('doctor', org)

    assert confirm(client, mine.person, all=True).status_code == 200

    assert stored(theirs.person).hemoglobin_g_dl is None
    assert read(client, theirs.person)['provisional_fields']['hemoglobin_g_dl'] == 82
