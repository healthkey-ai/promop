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


def seen(client, person):
    """What the page shows to confirm: the confirmable fields and the members of their groups."""
    response = client.get(url(person))
    if response.status_code != 200:
        return {}
    info = response.json()['patient_info']
    block = info.get('provisional_confirm') or {}
    names = set(block.get('confirmable', [])) | {m for group in block.get('groups', []) for m in group}
    return {name: info.get(name) for name in names}


def confirm(client, person, **body):
    """POST a confirmation. ``all`` without ``expected`` is sent with what the page would show
    (as the editor does); pass ``bare=True`` to send the body exactly as given."""
    bare = body.pop('bare', False)
    if body.get('all') is True and 'expected' not in body and not bare:
        body['expected'] = seen(client, person)
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


def cancer_record(org):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        cancer(made.person, 82)
    refresh_patient_record(made.person)
    return made.person


def test_a_field_no_tab_shows_is_never_confirmed_by_all_nor_by_name(snomed, org):
    person = cancer_record(org)
    client, _ = as_role('doctor', org)
    shown = read(client, person)
    assert {'diagnosis_date', 'disease_slug', 'disease'} <= set(shown['provisional_fields'])
    assert not {'diagnosis_date', 'disease_slug'} & set(shown['provisional_confirm']['confirmable'])

    everything = confirm(client, person, all=True)
    named = confirm(client, person, fields=['diagnosis_date', 'disease_slug'])

    assert everything.status_code == 200 and named.status_code == 200
    assert everything.json()['confirmed'] == [] and named.json()['confirmed'] == []
    assert reasons(named) == {'diagnosis_date': 'not_shown', 'disease_slug': 'not_shown'}
    assert reasons(everything)['diagnosis_date'] == 'not_shown'
    record = stored(person)
    assert not record.disease_slug and record.diagnosis_date is None
    assert not record.user_edited_fields                          # nothing pinned with nothing to acknowledge it


def test_a_field_with_no_projection_is_not_confirmed_because_it_would_stay_pinned(snomed, org):
    """The disease is on the General tab but no OMOP fact acknowledges a typed disease: a confirmed
    one would stay in ``user_edited_fields``, and a newer approved fact would never replace it."""
    person = cancer_record(org)
    client, _ = as_role('doctor', org)
    assert 'disease' in read(client, person)['provisional_fields']

    response = confirm(client, person, fields=['disease'])

    assert response.status_code == 200, response.content
    assert reasons(response)['disease'] == 'no_projection'
    assert response.json()['confirmed'] == []
    assert stored(person).disease in (None, '') and not stored(person).user_edited_fields
    assert 'disease' in read(client, person)['provisional_fields']      # still a proposal


def test_a_value_nothing_acknowledged_after_the_write_is_taken_back(snomed, org, monkeypatch):
    """The safety net: a field the descriptor promised a way to be acknowledged, and none came,
    is rolled back to the savepoint and reported, never left pinned."""
    person = cancer_record(org)
    client, _ = as_role('doctor', org)
    monkeypatch.setattr(provisional_confirm, '_has_way_to_be_acknowledged', lambda entry: True)
    assert 'disease' in read(client, person)['provisional_confirm']['confirmable']

    response = confirm(client, person, fields=['disease'])

    assert response.status_code == 200, response.content
    assert reasons(response)['disease'] == 'no_projection' and response.json()['confirmed'] == []
    record = stored(person)
    assert record.disease in (None, '') and not record.user_edited_fields
    assert not RecordRevision.objects.filter(patient_record__person=person).exists()
    assert 'disease' in read(client, person)['provisional_fields']


def test_a_marked_field_of_another_diseases_section_is_not_shown():
    mine, _ = provisional_confirm.classify({'binet_stage': 80}, 'chronic lymphocytic leukemia', descriptor={
        'binet_stage': {'kind': 'direct', 'writable': True, 'target': 'patient_record',
                        'projection': {'omop_table': 'observation', 'concept_id': 1, 'source_value': 'x'}}})
    other, skipped = provisional_confirm.classify({'binet_stage': 80}, 'multiple myeloma')
    assert mine == ['binet_stage']
    assert other == [] and skipped == {'binet_stage': 'not_shown'}


def test_the_dropped_group_members_are_reported_as_dropped_not_as_unwritten():
    marks_after = {'weight': 80}
    skipped = [{'field': 'disease_slugs', 'reason': 'list_field'}, {'field': 'weight', 'reason': 'not_shown'},
               {'field': 'bmi', 'reason': 'calculated'}]
    provisional_confirm.mark_dropped(skipped, marks_after, confirmed=['disease'])
    assert [item['reason'] for item in skipped] == ['dropped_with_group', 'not_shown', 'calculated']


@pytest.fixture()
def paired(monkeypatch):
    """Two labs that are one answer, as a date and its precision are: a group of two."""
    monkeypatch.setitem(provisional_record.REJECTION_FOLLOWERS, 'hemoglobin_g_dl', ('sodium_meq_l',))


@pytest.fixture()
def pair_person(snomed, org, paired):
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _add_lab(made.person, 0)
    propose_hemoglobin(made.person)
    return made.person


def test_a_group_is_decided_whole_and_the_read_names_it(pair_person, org):
    client, _ = as_role('doctor', org)
    block = read(client, pair_person)['provisional_confirm']
    assert block['groups'] == [['hemoglobin_g_dl', 'sodium_meq_l']]

    response = confirm(client, pair_person, fields=['hemoglobin_g_dl'], expected=seen(client, pair_person))

    assert response.status_code == 200, response.content
    assert response.json()['confirmed'] == ['hemoglobin_g_dl', 'sodium_meq_l']
    assert reasons(response) == {} or 'sodium_meq_l' not in reasons(response)


def test_a_member_that_changed_skips_the_whole_group_and_writes_nothing(pair_person, org):
    client, _ = as_role('doctor', org)
    expected = {**seen(client, pair_person), 'sodium_meq_l': '1.0'}

    response = confirm(client, pair_person, fields=['hemoglobin_g_dl'], expected=expected)

    assert response.status_code == 409
    assert reasons(response) == {'hemoglobin_g_dl': 'changed', 'sodium_meq_l': 'changed'}
    record = stored(pair_person)
    assert record.hemoglobin_g_dl is None and record.sodium_meq_l is None
    assert not RecordRevision.objects.filter(patient_record__person=pair_person).exists()


def test_a_member_the_page_never_listed_skips_the_whole_group(pair_person, org):
    client, _ = as_role('doctor', org)
    only_the_leader = {'hemoglobin_g_dl': seen(client, pair_person)['hemoglobin_g_dl']}

    for body in ({'fields': ['hemoglobin_g_dl']}, {'all': True, 'bare': True}):
        response = confirm(client, pair_person, expected=only_the_leader, **body)
        assert response.status_code == 200, response.content
        assert {f: r for f, r in reasons(response).items() if f in ('hemoglobin_g_dl', 'sodium_meq_l')} == {
            'hemoglobin_g_dl': 'not_seen', 'sodium_meq_l': 'not_seen'}
        assert response.json()['confirmed'] == []
    assert stored(pair_person).hemoglobin_g_dl is None and stored(pair_person).sodium_meq_l is None


def test_named_alone_without_expected_a_group_is_confirmed_whole_from_the_servers_members(pair_person, org):
    client, _ = as_role('doctor', org)

    response = confirm(client, pair_person, fields=['hemoglobin_g_dl'])

    assert response.json()['confirmed'] == ['hemoglobin_g_dl', 'sodium_meq_l']
    assert float(stored(pair_person).sodium_meq_l) == 140                   # the server's own, not the client's


def test_no_field_is_reported_twice(pair_person, org):
    client, _ = as_role('doctor', org)
    expected = {**seen(client, pair_person), 'sodium_meq_l': '1.0'}

    response = confirm(client, pair_person, all=True, expected=expected)

    listed = [item['field'] for item in response.json()['skipped']]
    assert len(listed) == len(set(listed))


def test_all_without_expected_is_refused(person, doctor):
    response = confirm(doctor, person, all=True, bare=True)

    assert response.status_code == 400
    assert 'expected' in response.json()
    assert stored(person).hemoglobin_g_dl is None


def test_a_dates_precision_is_confirmed_with_its_date_and_not_alone(snomed, org, monkeypatch):
    """A precision is marked only with its date; named alone it is skipped, with the date it goes together."""
    marks = {'sct_date': 80, 'sct_date_precision': 80}
    confirmable, skipped = ['sct_date'], {'sct_date_precision': 'follows_field'}
    assert provisional_confirm.groups(marks, confirmable, skipped) == [['sct_date', 'sct_date_precision']]


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


def test_staff_who_is_this_records_patient_is_the_patient_here_too(person, org):
    identity = account('confirm-staff-patient@example.test', is_staff=True)
    PatientUser.objects.create(identity=identity, person=person, is_active=True)
    client = signed_in(identity)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 403
    assert 'provisional_confirm' not in read(client, person)
    assert stored(person).hemoglobin_g_dl is None
    # Staff who are not this patient still may.
    assert confirm(as_role('staff', org, email='confirm-other-staff@example.test')[0], person,
                   fields=['hemoglobin_g_dl']).status_code == 200


def test_who_may_confirm_is_who_may_write_apart_from_the_patient_themselves(person, org, elsewhere):
    """``may_confirm`` is ``can_write_patient`` less the account's own and represented records."""
    from omop_core.authorization import can_write_patient
    actors = []
    for role, home in (('doctor', org), ('org_admin', org), ('analyst', org), ('doctor', elsewhere),
                       ('org_admin', elsewhere), ('patient', org)):
        identity = account(f'confirm-eq-{role}-{home.slug}@example.test')
        grant(identity, home, role)
        actors.append(identity)
    own = account('confirm-eq-own@example.test')
    grant(own, org, 'doctor')
    PatientUser.objects.create(identity=own, person=person, is_active=True)
    actors += [own, account('confirm-eq-staff@example.test', is_staff=True), account('confirm-eq-none@example.test')]

    for actor in actors:
        expected = can_write_patient(actor, person.person_id) and actor is not own
        assert provisional_confirm.may_confirm(actor, person.person_id) is expected, actor.email


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

    # The dating of each fact reads the rows again per provisional code (bounded by
    # MAX_ORIGIN_GROUPS), so the overhead follows the codes, a few queries each; it never
    # follows the number of fields written beyond that.
    assert many >= few + 3, (few, many)
    assert many_overhead - few_overhead <= 3 * (many - few), (few_overhead, many_overhead)


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


def test_the_audit_trail_records_the_confirmation_as_an_update_with_no_field_names(person, org):
    client, doctor = as_role('doctor', org)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 200
    assert confirm(client, person, all=True).status_code == 200

    events = list(AuditEvent.objects.filter(path__endswith='confirm-provisional/', user_id=str(doctor.pk))
                  .order_by('pk'))
    assert [e.event_type for e in events] == ['record_update', 'record_update']
    assert all(e.method == 'POST' and e.status_code == 200 for e in events)
    assert all(e.resource_id == str(person.person_id) for e in events)
    assert events[0].detail == {'provisional_confirm': {'mode': 'fields'}}       # the revisions name the fields
    assert '11.5' not in str(events[0].detail) and 'hemoglobin' not in str(events[0].detail)


def test_the_patients_own_confirmation_is_classified_as_before():
    from django.test import RequestFactory
    from patient_portal.api.middleware import _classify_event_type
    factory = RequestFactory()
    assert _classify_event_type(factory.post('/api/patient-info/me/confirm/')) == 'record_create'
    assert _classify_event_type(factory.post('/api/v1/patient-records/7/confirm-provisional/')) == 'record_update'
    assert _classify_event_type(factory.post('/api/v1/patient-records/7/refresh/')) == 'record_create'


# --- the approved fact keeps the reading's own date -----------------------------------------------

def approved_hemoglobin(person):
    return Measurement.objects.filter(person=person, confidence__isnull=True, measurement_source_value='718-7')


def test_the_approved_fact_is_dated_when_the_reading_was_taken_not_today(person, org):
    from datetime import date
    proposal = Measurement.objects.get(person=person, confidence__isnull=False)
    assert proposal.measurement_date == date(2024, 6, 1)
    client, _ = as_role('doctor', org)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 200

    fact = approved_hemoglobin(person).get()
    assert fact.measurement_date == date(2024, 6, 1)
    assert float(fact.value_as_number) == 11.5 and fact.unit_source_value == 'g/dL'
    assert fact.measurement_concept_id == proposal.measurement_concept_id
    # The proposal's own row stays as it was: its mapping's approval is what promotes it.
    proposal.refresh_from_db()
    assert proposal.confidence == 82 and proposal.measurement_date == date(2024, 6, 1)


def test_a_typed_value_is_still_todays_fact(snomed, org):
    from django.utils import timezone
    made = PatientRecordFactory(organization=org, disease=None)
    propose_hemoglobin(made.person)
    client, _ = as_role('doctor', org)

    assert client.patch(url(made.person), {'hemoglobin_g_dl': '11.5'}, format='json').status_code == 200

    assert approved_hemoglobin(made.person).get().measurement_date == timezone.localdate()


def test_the_proposals_time_of_day_comes_with_its_date(person, org):
    from datetime import datetime, timezone as utc
    taken = datetime(2024, 6, 1, 8, 30, tzinfo=utc.utc)
    Measurement.objects.filter(person=person, confidence__isnull=False).update(measurement_datetime=taken)
    client, _ = as_role('doctor', org)

    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 200

    assert approved_hemoglobin(person).get().measurement_datetime == taken


def test_a_newer_approved_fact_replaces_a_confirmed_value_and_an_older_one_does_not(person, org):
    from datetime import date
    from tests.factories import MeasurementFactory
    client, _ = as_role('doctor', org)
    assert confirm(client, person, fields=['hemoglobin_g_dl']).status_code == 200
    concept = Measurement.objects.get(person=person, confidence__isnull=False).measurement_concept
    assert not stored(person).user_edited_fields            # acknowledged by the projected fact, not pinned

    def real(value, when):
        MeasurementFactory(person=person, measurement_concept=concept, measurement_date=when, value_as_number=value,
                           measurement_source_value='718-7', unit_source_value='g/dL', value_source_value=str(value))

    with suppress_patient_record_refresh():
        real(8.0, date(2023, 1, 1))                           # before the reading: does not distort latest-wins
    refresh_patient_record(person)
    assert float(stored(person).hemoglobin_g_dl) == 11.5

    with suppress_patient_record_refresh():
        real(9.0, date(2025, 1, 1))                           # a newer approved fact
    refresh_patient_record(person)
    assert float(stored(person).hemoglobin_g_dl) == 9.0


@pytest.mark.parametrize('table, value, kind', [('observation', 4.5, 'number'), ('condition', True, 'boolean')])
def test_the_projection_dates_an_observation_or_a_condition_as_it_is_told(snomed, table, value, kind):
    from datetime import date
    from django.utils import timezone
    from omop_core.models import ConditionOccurrence
    from omop_core.services.omop_projection import project_single_value
    person = PatientRecordFactory().person
    domain = 'Observation' if table == 'observation' else 'Condition'
    concept = _concept(domain, 9100001, 'DATED-1', 'Dated concept')
    projection = {'omop_table': table, 'concept_id': concept.concept_id, 'source_value': 'DATED-1',
                  'type_concept_id': 32817, 'value_kind': kind}

    assert project_single_value(person, 'dated_field', value, projection, as_of=date(2021, 3, 4))

    model = Observation if table == 'observation' else ConditionOccurrence
    date_column = 'observation_date' if table == 'observation' else 'condition_start_date'
    assert getattr(model.objects.get(person=person), date_column) == date(2021, 3, 4)
    # Without as_of it is today's fact, as before.
    other = PatientRecordFactory().person
    assert project_single_value(other, 'dated_field', value, projection)
    assert getattr(model.objects.get(person=other), date_column) == timezone.localdate()


def test_a_proposal_of_today_is_not_overwritten_by_the_fact_that_approves_it(snomed, org):
    """Dated at the reading's day, the new fact could once have matched the proposal's own row."""
    from django.utils import timezone
    made = PatientRecordFactory(organization=org, disease=None)
    with suppress_patient_record_refresh():
        _add_measurement(made.person, 82)
    Measurement.objects.filter(person=made.person).update(measurement_date=timezone.localdate())
    refresh_patient_record(made.person)
    client, _ = as_role('doctor', org)

    assert confirm(client, made.person, fields=['hemoglobin_g_dl']).status_code == 200

    assert Measurement.objects.filter(person=made.person, confidence=82).count() == 1
    assert approved_hemoglobin(made.person).count() == 1


# --- what the read costs -------------------------------------------------------------------

READ_EXTRA_BOUND = 9


def test_the_read_costs_a_bounded_number_of_extra_queries(person, org, monkeypatch):
    from patient_portal.api import views
    spent = {}
    for name in ('doctor', 'staff'):
        who, _ = as_role(name, org, email=f'confirm-cost-{name}@example.test')
        who.get(url(person))                                  # warm per-process caches
        with CaptureQueriesContext(connection) as with_block:
            assert 'provisional_confirm' in who.get(url(person)).json()['patient_info']
        with monkeypatch.context() as patch:
            patch.setattr(views.PatientRecordV1ViewSet, '_provisional_confirm_block',
                          lambda self, request, patient_info, shown, marks: {})
            with CaptureQueriesContext(connection) as without_block:
                assert 'provisional_confirm' not in who.get(url(person)).json()['patient_info']
        spent[name] = len(with_block) - len(without_block)
    assert max(spent.values()) <= READ_EXTRA_BOUND, spent


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
