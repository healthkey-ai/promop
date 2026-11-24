"""Created fields (``CustomPatientField``) take part in provisional data (#2857).

A created field has no column: its value is a key of ``PatientRecord.custom_fields``,
derived from OMOP rows through its approved mapping. A proposal-based row (confidence
not null) for it is shown to the patient marked with its confidence, like a built-in
field, with the same rules: a proposal only fills a field the approved record leaves
empty and nobody typed, the newest answers, and the approved record never holds it.

``patient_record_confidence.custom_fields`` is ``{field_name: percent}``;
``patient_record_provisional.custom_fields`` is the picture of the values; the record
read reports a mark as ``provisional_fields["custom_fields.<name>"]``.
"""
import json
import pathlib

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID, resolve_person_source_codes
from omop_core.models import (
    CustomPatientField, DiseaseCategoryChoice, DiseaseCategoryMapping, FieldConceptMapping,
    FieldDiseaseMembership, GroupAccess, Observation,
    Organization, Person, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
    ProposalRejection, SourceCodeConceptMapping,
)
from omop_core.services import custom_field_values, provisional_record
from omop_core.services.fhir_export import build_fhir_bundle
from omop_core.services.omop_projection import PATIENT_SELF_REPORT_TYPE
from omop_core.services.patient_record_service import _build_snapshot, refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory, ConditionOccurrenceFactory, MeasurementFactory, ObservationFactory,
    PatientRecordFactory, PersonFactory,
)

pytestmark = pytest.mark.django_db

EHR = 32817
ME = '/api/v1/patient-records/me/'
CONFIRM = '/api/patient-info/me/confirm/'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    settings.CELERY_BROKER_URL = ''


@pytest.fixture
def world():
    """Concept 0 and the type concepts a write needs, plus a person with a record."""
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=EHR, concept_code='32817', concept_name='EHR')
    ConceptFactory(concept_id=PATIENT_SELF_REPORT_TYPE, concept_code='patient-report-type')
    org = Organization.objects.create(name='Custom Prov Org', slug='custom-prov-org')
    person = PersonFactory()
    record = PatientRecordFactory(person=person, organization=org, disease='')
    return person, record


def field(name='frailty_note', field_type='text', table='Observation', *, mode='editable',
          status='approved', tab='general', concept=None):
    concept = concept or ConceptFactory(concept_name=f'Concept for {name}')
    mapping = FieldConceptMapping.objects.create(
        field_name=name, concept=concept, vocabulary_id=concept.vocabulary_id,
        concept_code=concept.concept_code, omop_table=table, source_value=f'custom:{name}',
        status=status,
        value_kind={'number': 'number', 'boolean': 'boolean', 'date': 'date'}.get(field_type, 'string'),
    )
    return CustomPatientField.objects.create(
        field_name=name, display_name=name.replace('_', ' ').title(), tab=tab,
        field_type=field_type, mode=mode, mapping=mapping)


def observed(person, created, text, confidence=82, when='2024-03-01', source='LOCAL-OBS'):
    """An observation answering the field at its concept, a proposal when it has a confidence."""
    return ObservationFactory(
        person=person, observation_concept=created.mapping.concept, observation_date=when,
        observation_source_value=source, value_as_string=text, confidence=confidence)


def derive(person):
    refresh_patient_record(person)
    return PatientRecord.objects.get(person=person)


def picture(person):
    return PatientRecordProvisional.objects.get(person=person)


def marks(person):
    return PatientRecordConfidence.objects.get(person=person)


def signed_in(person, org, role='patient'):
    identity = Identity.objects.create_user(email=f'cf{person.person_id}@custom-prov.test', password='pw')
    GroupAccess.objects.create(identity=identity, org=org, role=role)
    PatientUser.objects.create(identity=identity, person=person, is_active=True)
    client = APIClient()
    client.force_login(identity)
    return client


# --- the confidence table holds a name -> percent object ------------------------------

def test_the_confidence_column_is_a_json_object_and_the_picture_a_clone_of_the_record_column():
    from django.db import models
    assert isinstance(PatientRecordConfidence._meta.get_field('custom_fields'), models.JSONField)
    assert PatientRecordConfidence._meta.get_field('custom_fields').null
    assert isinstance(PatientRecordProvisional._meta.get_field('custom_fields'), models.JSONField)


@pytest.mark.parametrize('table, field_type, make, expected', [
    ('Observation', 'text',
     lambda p, c: ObservationFactory(person=p, observation_concept=c, observation_date='2024-03-01',
                                     observation_source_value='LOCAL-X', value_as_string='stable',
                                     confidence=82), 'stable'),
    ('Measurement', 'number',
     lambda p, c: MeasurementFactory(person=p, measurement_concept=c, measurement_date='2024-03-01',
                                     measurement_source_value='LOCAL-X', value_as_number=4.5,
                                     confidence=82), 4.5),
    ('Condition', 'boolean',
     lambda p, c: ConditionOccurrenceFactory(person=p, condition_concept=c,
                                             condition_source_value='LOCAL-X', confidence=82), True),
])
def test_a_proposal_row_surfaces_as_a_provisional_value_with_its_percent(
        world, table, field_type, make, expected):
    person, _ = world
    created = field('frailty_note', field_type, table)
    with suppress_patient_record_refresh():
        make(person, created.mapping.concept)

    stored = derive(person)

    assert stored.custom_fields == {}                        # patient_record stays approved-only
    assert picture(person).custom_fields == {'frailty_note': expected}
    assert marks(person).custom_fields == {'frailty_note': 82}


def test_a_row_under_the_fields_own_source_key_is_read_too(world):
    person, _ = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'entered text', 77, source='custom:frailty_note')

    derive(person)

    assert picture(person).custom_fields == {'frailty_note': 'entered text'}
    assert marks(person).custom_fields == {'frailty_note': 77}


def test_the_picture_keeps_the_approved_values_and_adds_the_proposed_ones(world):
    person, record = world
    approved_field, proposed_field = field('approved_note'), field('proposed_note')
    with suppress_patient_record_refresh():
        observed(person, approved_field, 'real value', None)
        observed(person, proposed_field, 'guess', 80)

    stored = derive(person)

    assert stored.custom_fields == {'approved_note': 'real value'}
    assert picture(person).custom_fields == {'approved_note': 'real value', 'proposed_note': 'guess'}
    assert marks(person).custom_fields == {'proposed_note': 80}   # only the proposal is marked


def test_a_person_with_no_proposal_has_no_custom_confidence(world):
    person, _ = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'approved', None)
        observed(person, field('other_note'), 'x', 90)

    derive(person)

    assert marks(person).custom_fields == {'other_note': 90}
    with suppress_patient_record_refresh():
        Observation.objects.filter(person=person, confidence__isnull=False).delete()
    derive(person)
    assert marks(person).custom_fields is None


# --- the rules: real beats provisional, the newest answers, a typed value wins --------

def test_an_approved_row_wins_over_a_newer_proposal(world):
    person, _ = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'approved', None, when='2024-01-01')
        observed(person, created, 'guess', 90, when='2024-06-01')

    stored = derive(person)

    assert stored.custom_fields == {'frailty_note': 'approved'}
    assert picture(person).custom_fields == {'frailty_note': 'approved'}
    assert marks(person).custom_fields is None


def test_the_newest_proposal_answers_with_its_own_percent(world):
    person, _ = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'older', 95, when='2024-01-01')
        observed(person, created, 'newest', 71, when='2024-06-01')

    derive(person)

    assert picture(person).custom_fields == {'frailty_note': 'newest'}
    assert marks(person).custom_fields == {'frailty_note': 71}


def test_a_pending_user_edit_wins_and_a_pending_clear_stays_clear(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 90)
    PatientRecord.objects.filter(pk=record.pk).update(
        custom_fields={'frailty_note': 'mine'}, user_edited_fields=['custom_fields.frailty_note'])

    derive(person)
    assert picture(person).custom_fields == {'frailty_note': 'mine'}
    assert marks(person).custom_fields is None

    # An explicit clear is an edit too: the proposal must not refill it.
    PatientRecord.objects.filter(pk=record.pk).update(
        custom_fields={}, user_edited_fields=['custom_fields.frailty_note'])
    derive(person)
    assert (picture(person).custom_fields or {}).get('frailty_note') is None
    assert marks(person).custom_fields is None


def test_a_proposal_below_the_floor_is_never_written_so_never_shown(world, settings):
    person, _ = world
    created = field(table='Measurement', field_type='number')
    SourceCodeConceptMapping.objects.create(
        source_code='GLU-1', source_vocabulary_id='Local', source_code_description='GLU-1',
        domain_id=created.mapping.concept.domain_id, omop_table='measurement',
        target_concept=created.mapping.concept, status='proposed', suggestion_confidence=0.69)
    MeasurementFactory(person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
                       measurement_source_value='GLU-1', value_as_number=5, measurement_date='2024-03-01')

    resolve_person_source_codes(person, derive=False)
    derive(person)
    assert not PatientRecordConfidence.objects.filter(person=person).exclude(custom_fields=None).exists()

    # The same code proposed at the floor is written provisional, and then shown.
    SourceCodeConceptMapping.objects.update(suggestion_confidence=0.82)
    resolve_person_source_codes(person, derive=False)
    derive(person)
    assert marks(person).custom_fields == {'frailty_note': 82}
    assert picture(person).custom_fields == {'frailty_note': 5}


def test_with_the_switch_off_nothing_is_stored_or_shown(world, settings):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 90)
    settings.PROVISIONAL_DATA_ENABLED = False

    stored = derive(person)

    assert stored.custom_fields == {}
    assert not PatientRecordProvisional.objects.exists() and not PatientRecordConfidence.objects.exists()
    shown, shown_marks = provisional_record.overlay(stored)
    assert shown is stored and shown_marks == {}


def test_a_computed_field_is_not_proposed(world):
    person, _ = world
    created = field(mode='computed')
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 90)

    stored = derive(person)

    assert stored.custom_fields == {}
    assert not PatientRecordConfidence.objects.filter(person=person).exclude(custom_fields=None).exists()


def test_a_field_without_an_approved_mapping_is_ignored(world):
    person, _ = world
    created = field()
    FieldConceptMapping.objects.filter(pk=created.mapping_id).update(status='proposed')
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 90)

    derive(person)

    assert not PatientRecordConfidence.objects.filter(person=person).exclude(custom_fields=None).exists()


def test_a_proposal_for_an_unrelated_concept_is_not_a_custom_value(world):
    person, _ = world
    field()
    with suppress_patient_record_refresh():
        ObservationFactory(person=person, observation_concept=ConceptFactory(), observation_date='2024-03-01',
                           observation_source_value='LOCAL-Z', value_as_string='x', confidence=90)

    derive(person)

    assert not PatientRecordConfidence.objects.filter(person=person).exclude(custom_fields=None).exists()


def test_the_cost_does_not_grow_with_the_number_of_created_fields(world):
    person, record = world

    def add(count, start):
        with suppress_patient_record_refresh():
            for index in range(start, start + count):
                observed(person, field(f'cost_{index}'), f'v{index}', 80)

    def measure(expected):
        snapshot = _build_snapshot(person)
        with CaptureQueriesContext(connection) as captured:
            values, found = provisional_record.custom_proposals(
                person, PatientRecord.objects.get(person=person), snapshot)
        assert len(values['custom_fields']) == expected and set(found['custom_fields'].values()) == {80}
        return len(captured)

    add(3, 0)
    small = measure(3)
    add(12, 3)
    assert measure(15) == small


# --- the record read -----------------------------------------------------------------

def test_the_record_read_lays_a_proposal_over_an_empty_key_and_marks_it(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82)
        observed(person, field('approved_note'), 'real', None)
    derive(person)
    client = signed_in(person, record.organization)

    body = client.get(ME).json()['patient_info']

    assert body['custom_fields'] == {'frailty_note': 'stable', 'approved_note': 'real'}
    assert body['provisional_fields'] == {'custom_fields.frailty_note': 82}
    assert 'provisional_items' not in body            # a created field is a scalar, not a list
    # The overlay is a view, never a write.
    assert PatientRecord.objects.get(person=person).custom_fields == {'approved_note': 'real'}


def test_a_stale_mark_never_overrides_an_approved_or_pending_value(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)
    derive(person)

    # The record gained its own value after the pair was written.
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={'frailty_note': 'approved'})
    shown, shown_marks = provisional_record.overlay(PatientRecord.objects.get(pk=record.pk))
    assert shown.custom_fields == {'frailty_note': 'approved'} and shown_marks == {}

    # Or it holds a pending clear.
    PatientRecord.objects.filter(pk=record.pk).update(
        custom_fields={}, user_edited_fields=['custom_fields.frailty_note'])
    shown, shown_marks = provisional_record.overlay(PatientRecord.objects.get(pk=record.pk))
    assert shown.custom_fields == {} and shown_marks == {}


def test_the_patient_list_carries_no_created_field_mark(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)
    derive(person)
    staff = APIClient()
    staff.force_authenticate(Identity.objects.create_user(email='staff@custom-prov.test', is_staff=True))

    rows = staff.get('/api/v1/patient-records/').json()
    rows = rows['results'] if isinstance(rows, dict) else rows

    assert [r['person_id'] for r in rows] == [person.person_id]
    assert 'custom_fields.frailty_note' not in (rows[0].get('provisional_fields') or {})


# --- never exported ------------------------------------------------------------------

def test_no_export_or_approved_only_read_carries_a_proposed_custom_value(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'proposed-secret', 82)
    derive(person)
    staff = APIClient()
    staff.force_authenticate(Identity.objects.create_user(email='staff2@custom-prov.test', is_staff=True))

    # The FHIR export reads approved rows only, and there is no way to include the others.
    bundle = build_fhir_bundle(person)
    assert 'proposed-secret' not in str(bundle)
    assert [e['resource']['resourceType'] for e in bundle['entry']] == ['Patient']
    assert 'proposed-secret' not in staff.get(
        f'/api/v1/patient-records/{person.person_id}/export-fhir/').content.decode()
    # The CSV export is the approved record's serializer output.
    csv_export = staff.get(f'/api/v1/patient-records/export-csv/?person_ids={person.person_id}')
    assert csv_export.status_code == 200
    assert 'proposed-secret' not in csv_export.content.decode()
    # The PHR record export, signed in as the patient.
    patient = signed_in(person, record.organization)
    exported = patient.get('/api/v1/phr/record/export/?format=fhir')
    assert exported.status_code == 200
    assert 'proposed-secret' not in exported.content.decode()
    # patient_record itself, and a reader that asks for the approved-only record.
    assert 'proposed-secret' not in str(PatientRecord.objects.get(person=person).custom_fields)


def test_the_phr_primary_cancer_carries_the_mark_of_the_spread_it_shows(world):
    person, record = world
    PatientRecord.objects.filter(pk=record.pk).update(
        disease='Follicular lymphoma', disease_slug='follicular-lymphoma')
    PatientRecordProvisional.objects.create(
        person=person, disease='Follicular lymphoma', disease_slug='follicular-lymphoma',
        custom_fields={'hematologic_spread': 'Bone marrow'})
    PatientRecordConfidence.objects.create(person=person, custom_fields={'hematologic_spread': 76})
    client = signed_in(person, record.organization)

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer'][0]

    assert cancer['spread'] == 'Bone marrow' and cancer['confidence'] == 76


# --- writes: a typed value approves it ------------------------------------------------

def test_entering_a_value_approves_it_and_drops_only_that_mark(world):
    person, record = world
    first, second = field('first_note'), field('second_note')
    with suppress_patient_record_refresh():
        observed(person, first, 'guess one', 82)
        observed(person, second, 'guess two', 91)
    derive(person)
    client = signed_in(person, record.organization)
    assert client.get(ME).json()['patient_info']['provisional_fields'] == {
        'custom_fields.first_note': 82, 'custom_fields.second_note': 91}

    response = client.patch(ME, {'custom_fields': {'first_note': 'guess one'}}, format='json')

    assert response.status_code == 200, response.content
    stored = PatientRecord.objects.get(person=person)
    assert stored.custom_fields == {'first_note': 'guess one'}
    assert marks(person).custom_fields == {'second_note': 91}
    assert picture(person).custom_fields == {'first_note': 'guess one', 'second_note': 'guess two'}
    read = client.get(ME).json()['patient_info']
    assert read['provisional_fields'] == {'custom_fields.second_note': 91}
    assert read['custom_fields'] == {'first_note': 'guess one', 'second_note': 'guess two'}


def test_a_different_value_also_approves_and_the_last_mark_clears_to_null(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)
    derive(person)
    client = signed_in(person, record.organization)

    client.patch(ME, {'custom_fields': {'frailty_note': 'my own answer'}}, format='json')

    assert PatientRecord.objects.get(person=person).custom_fields == {'frailty_note': 'my own answer'}
    assert marks(person).custom_fields is None
    assert picture(person).custom_fields == {'frailty_note': 'my own answer'}
    assert client.get(ME).json()['patient_info']['provisional_fields'] == {}


def test_an_edit_of_another_field_leaves_a_created_proposal_in_place(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)
    derive(person)
    client = signed_in(person, record.organization)

    response = client.patch(ME, {'city': 'Springfield'}, format='json')

    assert response.status_code == 200, response.content
    assert marks(person).custom_fields == {'frailty_note': 82}
    assert picture(person).custom_fields == {'frailty_note': 'guess'}


# --- rejecting a created proposal (#2857) -----------------------------------------------

MARK = 'custom_fields.frailty_note'


def reject(client, blank=None, declared=(MARK,), name='frailty_note'):
    return client.patch(
        ME, {'custom_fields': {name: blank}, 'rejected_proposals': list(declared)}, format='json')


def make_row(person, created, kind, source='LOCAL-X', confidence=82, when='2024-03-01'):
    concept = created.mapping.concept
    if kind == 'number':
        return MeasurementFactory(person=person, measurement_concept=concept, measurement_date=when,
                                  measurement_source_value=source, value_as_number=4.5, confidence=confidence)
    if kind == 'boolean':
        return ConditionOccurrenceFactory(person=person, condition_concept=concept, condition_start_date=when,
                                          condition_source_value=source, confidence=confidence)
    return observed(person, created, 'stable', confidence, when=when, source=source)


@pytest.mark.parametrize('kind, table, blank', [
    ('text', 'Observation', ''), ('text', 'Observation', None), ('number', 'Measurement', None),
    ('date', 'Observation', None), ('boolean', 'Condition', None),
])
def test_clearing_a_proposed_created_field_rejects_it_and_it_stays_rejected(world, kind, table, blank):
    person, record = world
    created = field('frailty_note', kind, table)
    with suppress_patient_record_refresh():
        make_row(person, created, kind)
    derive(person)
    client = signed_in(person, record.organization)
    assert client.get(ME).json()['patient_info']['provisional_fields'] == {MARK: 82}

    response = reject(client, blank)

    assert response.status_code == 200, response.content
    # Not an edit, not an approval: the record stores nothing for the key.
    stored = PatientRecord.objects.get(person=person)
    assert stored.custom_fields == {} and not custom_field_values.pending_names(stored.user_edited_fields)
    rejection = ProposalRejection.objects.get(person=person, field_name=MARK)
    assert len(rejection.origins) == 1 and rejection.rejected_by
    # The mark and the shown value are gone in the same request.
    assert marks(person).custom_fields is None
    assert (picture(person).custom_fields or {}).get('frailty_note') is None
    read = client.get(ME).json()['patient_info']
    assert read['provisional_fields'] == {} and 'frailty_note' not in read['custom_fields']
    # A new reading of the same code stays rejected through a derivation.
    with suppress_patient_record_refresh():
        make_row(person, created, kind, when='2024-05-01')
    derive(person)
    assert marks(person).custom_fields is None
    assert client.get(ME).json()['patient_info']['provisional_fields'] == {}


def test_a_different_code_answering_the_field_is_a_new_proposal_and_shows_again(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82, source='LOCAL-A')
    derive(person)
    client = signed_in(person, record.organization)
    reject(client, '')

    with suppress_patient_record_refresh():
        observed(person, created, 'other code', 75, when='2024-09-01', source='LOCAL-B')
    derive(person)

    assert marks(person).custom_fields == {'frailty_note': 75}
    assert client.get(ME).json()['patient_info']['provisional_fields'] == {MARK: 75}


def test_typing_a_value_after_a_rejection_approves_it_and_the_rejection_is_ignored(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82)
    derive(person)
    client = signed_in(person, record.organization)
    reject(client, '')

    client.patch(ME, {'custom_fields': {'frailty_note': 'mine'}}, format='json')

    assert PatientRecord.objects.get(person=person).custom_fields == {'frailty_note': 'mine'}
    assert ProposalRejection.objects.filter(person=person, field_name=MARK).exists()   # kept, ignored
    read = client.get(ME).json()['patient_info']
    assert read['custom_fields'] == {'frailty_note': 'mine'} and read['provisional_fields'] == {}


def test_flipping_a_boolean_is_an_edit_and_only_null_rejects(world):
    person, record = world
    created = field('frailty_note', 'boolean', 'Condition')
    with suppress_patient_record_refresh():
        make_row(person, created, 'boolean')
    derive(person)
    client = signed_in(person, record.organization)

    response = reject(client, False)            # a false answer: the patient's own

    assert response.status_code == 200, response.content
    assert PatientRecord.objects.get(person=person).custom_fields == {'frailty_note': False}
    assert not ProposalRejection.objects.exists()
    assert marks(person).custom_fields is None


def test_a_blank_the_client_did_not_declare_is_not_a_rejection(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82)
    derive(person)
    client = signed_in(person, record.organization)

    for declared in ([], ['frailty_note'], ['custom_fields']):
        assert reject(client, None, declared).status_code == 200

    assert not ProposalRejection.objects.exists()
    assert marks(person).custom_fields == {'frailty_note': 82}


def test_a_declared_name_that_is_no_proposal_is_ignored(world):
    person, record = world
    created = field()
    other = field('other_note')
    computed = field('derived_note', mode='computed')
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82)
        observed(person, computed, 'guess', 90)
        observed(person, other, 'real', None)
    derive(person)
    client = signed_in(person, record.organization)

    for name in ('other_note', 'derived_note', 'no_such_field'):
        response = reject(client, None, [f'custom_fields.{name}'], name)
        assert response.status_code == 200, response.content
    # The one proposal there is, the declared name of an unapproved field, is ignored.
    FieldConceptMapping.objects.filter(pk=created.mapping_id).update(status='proposed')
    assert reject(client, None).status_code == 200

    assert not ProposalRejection.objects.exists()


def test_a_malformed_rejected_proposals_is_refused(world):
    person, record = world
    client = signed_in(person, record.organization)

    for bad in ('custom_fields.frailty_note', [1], {'a': 1}):
        response = client.patch(ME, {'custom_fields': {}, 'rejected_proposals': bad}, format='json')
        assert response.status_code == 400, bad


def test_a_survey_a_machine_credential_and_a_staff_save_without_the_declaration_do_not_reject(world, settings):
    from patient_portal.api.permissions import SERVICE_TOKEN
    from patient_portal.api.views import apply_patient_record_patch
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82)
    derive(person)
    identity = Identity.objects.create_user(email='survey@custom-prov.test')

    # The survey mapping writes through the same function with no allow_reject.
    apply_patient_record_patch(
        person, PatientRecord.objects.get(person=person),
        {'custom_fields': {'frailty_note': None}, 'rejected_proposals': [MARK]}, actor=identity)
    # An ETL's service token.
    settings.SERVICE_AUTH_SCOPES = 'patient/*.read patient/*.write'
    machine = APIClient()
    machine.force_authenticate(Identity.objects.create_user(email='etl@custom-prov.test', is_staff=True),
                               token=SERVICE_TOKEN)
    response = machine.patch(f'/api/v1/patient-records/{person.person_id}/',
                             {'custom_fields': {'frailty_note': None}, 'rejected_proposals': [MARK]},
                             format='json')
    assert response.status_code == 200, response.content

    assert not ProposalRejection.objects.exists()
    assert marks(person).custom_fields == {'frailty_note': 82}


def test_the_confirmation_is_offered_again_once_the_proposal_is_rejected(world):
    person, record = world
    created = field('frailty_note', tab='general')
    with suppress_patient_record_refresh():
        observed(person, created, 'stable', 82)
    derive(person)
    client = signed_in(person, record.organization)
    assert client.post(CONFIRM).status_code == 409

    reject(client, '')

    assert provisional_record.attestation_blockers(PatientRecord.objects.get(person=person)) == []
    assert client.post(CONFIRM).status_code == 200


def test_a_rejection_of_a_created_field_is_erased_with_the_record_and_fits_the_longest_name(world):
    from patient_portal.erasure import erase_record
    person, record = world
    long_name = 'x' * 100
    ProposalRejection.objects.create(person=person, field_name=f'custom_fields.{long_name}', origins=[])
    assert ProposalRejection.objects.get(person=person).field_name == f'custom_fields.{long_name}'

    erase_record(person)

    assert not ProposalRejection.objects.filter(person=person).exists()


def test_a_rejection_is_not_exported(world):
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'proposed-secret', 82)
    derive(person)
    client = signed_in(person, record.organization)
    reject(client, '')

    assert 'proposed-secret' not in str(build_fhir_bundle(person))
    exported = client.get('/api/v1/phr/record/export/?format=fhir')
    assert exported.status_code == 200 and 'proposed-secret' not in exported.content.decode()


def test_an_echo_of_the_shown_object_is_an_edit_so_a_client_sends_only_what_changed(world):
    # The scalar rule, as for a column: a value sent back is the person's answer. The
    # page keeps the real record as its baseline and sends only the keys it changed.
    person, record = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)
    derive(person)
    client = signed_in(person, record.organization)
    shown = client.get(ME).json()['patient_info']

    client.patch(ME, {'custom_fields': {}}, format='json')
    assert marks(person).custom_fields == {'frailty_note': 82}        # nothing sent, nothing approved

    client.patch(ME, {'custom_fields': shown['custom_fields']}, format='json')
    assert PatientRecord.objects.get(person=person).custom_fields == {'frailty_note': 'guess'}


# --- "Confirm my record" ---------------------------------------------------------------

def store_pair(person, picture_values, mark_values, **picture_columns):
    PatientRecordProvisional.objects.update_or_create(
        person=person, defaults={'custom_fields': picture_values, **picture_columns})
    PatientRecordConfidence.objects.update_or_create(person=person, defaults={'custom_fields': mark_values})


def test_a_marked_editable_field_on_a_patient_tab_blocks_the_confirmation(world):
    person, record = world
    field('frailty_note', tab='general')
    store_pair(person, {'frailty_note': 'guess'}, {'frailty_note': 82})
    client = signed_in(person, record.organization)

    response = client.post(CONFIRM)

    assert response.status_code == 409
    body = response.json()
    assert body['provisional_fields'] == ['custom_fields.frailty_note'] and body['count'] == 1
    assert not PatientRecord.objects.get(person=person).validated
    assert provisional_record.attestation_blockers(
        PatientRecord.objects.get(person=person)) == ['custom_fields.frailty_note']


def test_entering_the_value_lifts_the_block(world):
    person, record = world
    created = field('frailty_note', tab='labs')
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)
    derive(person)
    client = signed_in(person, record.organization)
    assert client.post(CONFIRM).status_code == 409

    client.patch(ME, {'custom_fields': {'frailty_note': 'guess'}}, format='json')

    assert client.post(CONFIRM).status_code == 200


@pytest.mark.parametrize('why', [
    'unplaced', 'computed', 'unapproved', 'another disease', 'disease tab without a disease',
    'undefined', 'approved',
])
def test_a_field_the_patient_cannot_see_or_edit_never_blocks(world, why):
    person, record = world
    name = 'frailty_note'
    created = field(name, tab='omop' if why == 'unplaced' else 'general',
                    mode='computed' if why == 'computed' else 'editable')
    if why == 'unapproved':
        FieldConceptMapping.objects.filter(pk=created.mapping_id).update(status='proposed')
    elif why == 'another disease':
        PatientRecord.objects.filter(pk=record.pk).update(disease='Multiple myeloma')
        FieldDiseaseMembership.objects.create(field_name=name, disease='BC')
    elif why == 'disease tab without a disease':
        CustomPatientField.objects.filter(pk=created.pk).update(tab='disease')
    elif why == 'undefined':
        created.delete()
    if why == 'approved':
        PatientRecord.objects.filter(pk=record.pk).update(custom_fields={name: 'approved'})
    store_pair(person, {name: 'guess'}, {name: 82})
    client = signed_in(person, record.organization)

    assert provisional_record.attestation_blockers(PatientRecord.objects.get(person=person)) == []
    assert client.post(CONFIRM).status_code == 200


def test_a_disease_tab_field_blocks_only_when_the_record_has_a_disease_tab(world):
    person, record = world
    created = field('frailty_note', tab='disease')
    FieldDiseaseMembership.objects.create(field_name='frailty_note', disease='MM')
    store_pair(person, {'frailty_note': 'guess'}, {'frailty_note': 82})
    client = signed_in(person, record.organization)

    PatientRecord.objects.filter(pk=record.pk).update(disease='Multiple myeloma')
    assert client.post(CONFIRM).status_code == 409
    # A breast record is another extension, and an unclassified one has no disease tab.
    PatientRecord.objects.filter(pk=record.pk).update(disease='Breast cancer')
    assert client.post(CONFIRM).status_code == 200
    PatientRecord.objects.filter(pk=record.pk).update(disease='')
    assert client.post(CONFIRM).status_code == 200
    assert created.pk


def test_shown_to_patient_is_one_query_set_whatever_the_number_of_marks(world):
    person, record = world
    names = [f'shown_{i}' for i in range(12)]
    for name in names:
        field(name)
    with CaptureQueriesContext(connection) as many:
        assert custom_field_values.shown_to_patient(names, '') == sorted(names)
    with CaptureQueriesContext(connection) as one:
        assert custom_field_values.shown_to_patient(names[:1], '') == names[:1]
    assert len(many) == len(one)


# --- the shared list a page reads ------------------------------------------------------

def test_the_stored_tab_list_is_every_placeable_tab_but_the_unplaced_one():
    from omop_core.services import patient_visible_fields as visible
    from omop_core.models import UNPLACED_TAB
    assert UNPLACED_TAB not in visible.custom_field_tabs()
    assert visible.custom_field_tabs() == frozenset(visible.placeable_custom_field_tabs())
    assert {'general', 'disease', 'treatment', 'labs', 'blood', 'genomics', 'behavior',
            'wearable'} == set(visible.custom_field_tabs())


def test_the_fill_report_names_a_created_field_as_the_record_read_does(world):
    from omop_core.services import fill_provisional
    person, _ = world
    created = field()
    with suppress_patient_record_refresh():
        observed(person, created, 'guess', 82)

    derived = fill_provisional.rederive(person)

    assert derived['provisional_fields'] == {'custom_fields.frailty_note': 82}


def test_a_deleted_person_takes_the_custom_confidence_with_it(world):
    person, _ = world
    store_pair(person, {'x': 'y'}, {'x': 80})
    assert Person.objects.filter(pk=person.pk).exists()
    person.delete()
    assert not PatientRecordConfidence.objects.exists()


# --- one list of cases, run against the server rule and the page's ----------------------

CASES_FILE = (pathlib.Path(__file__).resolve().parents[1]
              / 'frontend' / 'src' / 'components' / 'PatientInfo' / 'createdFieldAttestationCases.json')
CASES = json.loads(CASES_FILE.read_text())['cases']


@pytest.mark.parametrize('case', CASES, ids=[c['name'] for c in CASES])
def test_the_shared_attestation_cases_hold_on_the_server(world, case):
    # provisional.test.ts runs the same cases against the page's attestationBlockers,
    # so the page and this endpoint cannot disagree.
    person, record = world
    created = field('frailty_note', tab=case['tab'], mode=case.get('mode', 'editable'))
    if case.get('approved') is False:
        FieldConceptMapping.objects.filter(pk=created.mapping_id).update(status='proposed')
    for code in case['diseases']:
        FieldDiseaseMembership.objects.create(field_name='frailty_note', disease=code)
    if case.get('extra_category'):
        # A Disease Category with no hand-authored section, and the disease text that maps to it.
        DiseaseCategoryChoice.objects.create(code=case['extra_category'], label='Extra category')
        DiseaseCategoryMapping.objects.create(disease_name=case['disease'], category=case['extra_category'])
    for index, other in enumerate(case.get('others', [])):
        extra = field(f'other_{index}', tab=other['tab'])
        if not other['approved']:
            FieldConceptMapping.objects.filter(pk=extra.mapping_id).update(status='proposed')
        for code in other['diseases']:
            FieldDiseaseMembership.objects.create(field_name=extra.field_name, disease=code)
    PatientRecord.objects.filter(pk=record.pk).update(disease=case['disease'])
    if case.get('marked', True):
        store_pair(person, {'frailty_note': 'guess'}, {'frailty_note': 82})
    else:
        store_pair(person, {}, None)
    client = signed_in(person, record.organization)

    blockers = provisional_record.attestation_blockers(PatientRecord.objects.get(person=person))

    assert blockers == (['custom_fields.frailty_note'] if case['blocks'] else [])
    assert client.post(CONFIRM).status_code == (409 if case['blocks'] else 200)


def test_a_solid_tumours_primary_cancer_is_not_marked_for_a_spread_it_does_not_show(world):
    person, record = world
    PatientRecord.objects.filter(pk=record.pk).update(
        disease='Breast cancer', disease_slug='breast-cancer', metastasis_status='Regional')
    PatientRecordProvisional.objects.create(
        person=person, disease='Breast cancer', disease_slug='breast-cancer', metastasis_status='Regional',
        custom_fields={'hematologic_spread': 'Bone marrow'})
    PatientRecordConfidence.objects.create(person=person, custom_fields={'hematologic_spread': 76})
    client = signed_in(person, record.organization)

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer'][0]

    assert cancer['spread'] == 'Regional' and 'confidence' not in cancer


def test_a_disease_tab_count_ignores_a_field_the_page_never_lists(world):
    # custom_patient_field_list shows approved fields only, so an unapproved scoped field
    # must not make the server think a disease tab exists that the page never renders.
    person, record = world
    DiseaseCategoryChoice.objects.create(code='DLBCL', label='DLBCL')
    DiseaseCategoryMapping.objects.create(disease_name='Diffuse large B-cell lymphoma', category='DLBCL')
    PatientRecord.objects.filter(pk=record.pk).update(disease='Diffuse large B-cell lymphoma')
    shown = field('shown_note', tab='disease')
    hidden = field('hidden_note', tab='disease')
    FieldDiseaseMembership.objects.create(field_name='hidden_note', disease='DLBCL')
    FieldConceptMapping.objects.filter(pk=hidden.mapping_id).update(status='proposed')

    assert custom_field_values.shown_to_patient(['shown_note'], 'Diffuse large B-cell lymphoma') == []

    FieldConceptMapping.objects.filter(pk=hidden.mapping_id).update(status='approved')
    assert custom_field_values.shown_to_patient(['shown_note'], 'Diffuse large B-cell lymphoma') == ['shown_note']
    assert shown.pk
