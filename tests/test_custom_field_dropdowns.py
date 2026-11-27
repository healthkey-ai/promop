"""Created fields can be dropdowns: a curated list of answers, saved as the answer's value.

A dropdown (``CustomPatientField.field_type == 'choice'``) keeps its answers as
ordinary ``FieldChoice`` rows under the field's name, so the answer editor, the
``value_as_concept`` decisions and the derive-back a built-in coded field has all
apply to it. The value is the answer's stored value; an answer with a concept
projects as ``value_as_concept_id`` beside the text, one without as text only; a
fact imported at the field's concept reads back as the answer it stands for.
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import (
    CustomPatientField, FieldChoice, FieldConceptMapping, FieldDiseaseMembership,
    FieldValueConceptMapping, GroupAccess, Observation, PatientRecord,
)
from omop_core.services import custom_field_values, field_choices
from omop_core.services.omop_projection import PATIENT_SELF_REPORT_TYPE
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, DomainFactory, MeasurementFactory, ObservationFactory, OrganizationFactory,
    PatientRecordFactory, VocabularyFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('disease_extensions')]

EHR = 32817
CUSTOM = '/api/v1/custom-patient-fields/'
CHOICES = '/api/v1/field-choices/'
WRITABLE = '/api/v1/patient-records/writable-fields/'


@pytest.fixture(autouse=True)
def types(settings):
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=EHR, concept_code='ehr-type')
    ConceptFactory(concept_id=PATIENT_SELF_REPORT_TYPE, concept_code='patient-report-type')


@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='curator@example.org', is_staff=True))
    return client


def as_role(role):
    user = Identity.objects.create_user(email=f'{role}@example.org')
    GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def concepts():
    """The question concept and three answer concepts (Gleason grade groups)."""
    observation = DomainFactory(domain_id='Observation', domain_name='Observation')
    snomed = VocabularyFactory(vocabulary_id='SNOMED')
    return {
        'question': ConceptFactory(concept_code='Q-GRADE', concept_name='Gleason grade group',
                                   vocabulary=snomed, domain=observation),
        'one': ConceptFactory(concept_code='A-1', concept_name='Grade group 1',
                              vocabulary=snomed, domain=observation),
        'two': ConceptFactory(concept_code='A-2', concept_name='Grade group 2',
                              vocabulary=snomed, domain=observation),
    }


def payload(concepts, **changes):
    body = {
        'confirm_patient_record': True, 'field_name': 'gleason_grade_group',
        'display_name': 'Gleason grade group', 'field_type': 'choice', 'tab': 'disease',
        'concept': concepts['question'].concept_id, 'omop_table': 'observation',
        'answers': [
            {'label': 'Grade group 1', 'value': '1', 'concept': concepts['one'].concept_id},
            {'label': 'Grade group 2', 'value': '2', 'concept': concepts['two'].concept_id},
            {'label': 'Grade group 3', 'value': '3'},
        ],
    }
    body.update(changes)
    return body


def create(client, concepts, expect=201, **changes):
    response = client.post(CUSTOM, payload(concepts, **changes), format='json')
    assert response.status_code == expect, response.data
    return response


@pytest.fixture
def grade(staff, concepts):
    create(staff, concepts)
    return CustomPatientField.objects.get(field_name='gleason_grade_group')


@pytest.fixture
def record():
    return PatientRecordFactory()


def patch(client, record, values, expect=200):
    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'custom_fields': values}, format='json')
    assert response.status_code == expect, response.data
    return response


def facts(record, field):
    return Observation.objects.filter(
        person=record.person, observation_source_value=f'custom:{field.field_name}')


# ----------------------------------------------------------------- creating one


def test_creating_a_dropdown_makes_the_field_its_mapping_and_its_answers(staff, concepts):
    response = create(staff, concepts)

    assert response.data['field_type'] == 'choice'
    assert response.data['options'] == [
        {'value': '1', 'code': 'A-1', 'label': 'Grade group 1'},
        {'value': '2', 'code': 'A-2', 'label': 'Grade group 2'},
        {'value': '3', 'code': None, 'label': 'Grade group 3'},
    ]
    field = CustomPatientField.objects.get(field_name='gleason_grade_group')
    assert field.mapping.value_kind == 'concept' and field.mapping.status == 'approved'
    answers = list(FieldChoice.objects.filter(field_name='gleason_grade_group'))
    assert [(a.display, a.stored_value, a.sort_order) for a in answers] == [
        ('Grade group 1', '1', 0), ('Grade group 2', '2', 1), ('Grade group 3', '3', 2)]
    decided = {m.choice.display: m.target_concept_id
               for m in FieldValueConceptMapping.objects.filter(status='approved')}
    assert decided == {'Grade group 1': concepts['one'].pk, 'Grade group 2': concepts['two'].pk}


def test_an_answer_with_no_value_stores_its_label(staff, concepts):
    create(staff, concepts, field_name='disease_state', answers=[
        {'label': 'Localized'}, {'label': 'Metastatic'}])
    options = staff.get(CUSTOM).data[0]['options']
    # No label key where it is what is stored, as for a built-in select.
    assert options == [{'value': 'Localized', 'code': None}, {'value': 'Metastatic', 'code': None}]


@pytest.mark.parametrize('change, key', [
    ({'answers': []}, 'answers'),
    ({'answers': [{'label': 'A'}, {'label': ' a '}]}, 'answers'),
    ({'answers': [{'label': 'A', 'value': 'x'}, {'label': 'B', 'value': 'x'}]}, 'answers'),
    ({'answers': [{'label': 'A'}, {'label': 'B', 'value': 'A'}]}, 'answers'),
    ({'answers': [{'label': '   '}]}, 'answers'),
    ({'mode': 'computed', 'formula': '1 + 1'}, 'mode'),
    ({'omop_table': 'condition_occurrence'}, 'omop_table'),
])
def test_a_dropdown_is_refused_what_cannot_be_held(staff, concepts, change, key):
    response = create(staff, concepts, expect=400, **change)
    assert key in response.data
    assert not CustomPatientField.objects.exists() and not FieldChoice.objects.exists()
    assert not FieldConceptMapping.objects.exists()


def test_an_answer_concept_must_be_a_usable_standard_concept(staff, concepts):
    retired = ConceptFactory(concept_code='A-OLD', invalid_reason='D', standard_concept=None)
    response = create(staff, concepts, expect=400, answers=[
        {'label': 'Old', 'concept': retired.concept_id}])
    assert 'concept' in response.data['answers'][0]
    assert not CustomPatientField.objects.exists()


def test_two_answers_may_not_share_a_concept(staff, concepts):
    response = create(staff, concepts, expect=400, answers=[
        {'label': 'One', 'concept': concepts['one'].concept_id},
        {'label': 'Uno', 'concept': concepts['one'].concept_id}])
    assert 'concept' in response.data['answers'][1]


def test_only_a_dropdown_has_answers(staff, concepts):
    response = create(staff, concepts, expect=400, field_type='text')
    assert 'answers' in response.data


@pytest.mark.parametrize('role', ['analyst', 'doctor'])
def test_only_staff_and_org_admins_can_create_one(concepts, role):
    response = as_role(role).post(CUSTOM, payload(concepts), format='json')
    assert response.status_code == 403
    assert not CustomPatientField.objects.exists() and not FieldChoice.objects.exists()


def test_an_org_admin_can_create_one(concepts):
    client = as_role('org_admin')
    create(client, concepts)
    assert CustomPatientField.objects.filter(field_type='choice').count() == 1


def test_a_dropdown_takes_a_scope_like_any_created_field(staff, concepts):
    response = create(staff, concepts, diseases=['MM'])
    assert response.data['scope'] == 'disease' and response.data['diseases'] == ['MM']
    assert list(FieldDiseaseMembership.objects.filter(field_name='gleason_grade_group')
                .values_list('field_name', 'disease')) == [('gleason_grade_group', 'MM')]


def test_the_list_reads_options_without_a_query_per_field(staff, concepts, django_assert_max_num_queries):
    create(staff, concepts)
    create(staff, concepts, field_name='disease_state', answers=[{'label': 'Localized'}])
    create(staff, concepts, field_name='note', field_type='text', answers=[])
    with django_assert_max_num_queries(14):
        listed = {f['field_name']: f for f in staff.get(CUSTOM).data}
    assert listed['note']['options'] is None
    assert [o['value'] for o in listed['disease_state']['options']] == ['Localized']


def test_the_writable_descriptor_offers_the_options_and_a_recipe_with_the_concepts(staff, concepts, grade):
    entry = staff.get(WRITABLE).data['gleason_grade_group']
    assert entry['writable'] is True and entry['multiple'] is False
    assert entry['options'][0] == {'value': '1', 'code': 'A-1', 'label': 'Grade group 1'}
    assert entry['value_kind'] == 'concept'
    assert entry['projection']['value_concept_map'] == {
        'Grade group 1': concepts['one'].pk, '1': concepts['one'].pk,
        'Grade group 2': concepts['two'].pk, '2': concepts['two'].pk}


# ---------------------------------------------------------- the answers editor


def test_the_answer_editor_adds_an_answer_to_a_created_dropdown(staff, grade):
    response = staff.post(CHOICES, {'field_name': 'gleason_grade_group', 'display': 'Grade group 4',
                                    'value': '4', 'sort_order': 3}, format='json')
    assert response.status_code == 201, response.data
    assert [o['value'] for o in staff.get(CUSTOM).data[0]['options']] == ['1', '2', '3', '4']


def test_a_field_that_is_not_a_dropdown_takes_no_answers(staff, concepts):
    create(staff, concepts, field_name='psa_note', field_type='text', answers=[])
    response = staff.post(CHOICES, {'field_name': 'psa_note', 'display': 'x'}, format='json')
    assert response.status_code == 400 and 'field_name' in response.data


def test_the_field_mappings_page_lists_the_answers_with_the_field(staff, grade):
    listed = {d['field_name']: d for d in staff.get('/api/v1/field-mappings/').data}
    answers = listed['gleason_grade_group']['choices']
    assert [a['display'] for a in answers] == ['Grade group 1', 'Grade group 2', 'Grade group 3']
    assert listed['gleason_grade_group']['field_type'] == 'choice'


# ------------------------------------------------------------- saving a value


def test_a_dropdown_value_lands_on_the_record_is_pending_then_projected(staff, record, grade, concepts):
    response = patch(staff, record, {'gleason_grade_group': '2'})

    assert response.data['custom_fields'] == {'gleason_grade_group': '2'}
    stored = PatientRecord.objects.get(pk=record.pk)
    assert stored.custom_fields == {'gleason_grade_group': '2'}
    row = facts(record, grade).get()
    assert row.value_as_string == '2'
    assert row.value_as_concept_id == concepts['two'].pk
    assert row.observation_concept_id == grade.mapping.concept_id
    assert row.observation_date == timezone.localdate()
    # OMOP represents it, so nothing is left pending.
    assert custom_field_values.pending_names(stored.user_edited_fields) == set()


def test_an_answer_without_a_concept_is_written_as_text_only(staff, record, grade):
    patch(staff, record, {'gleason_grade_group': '3'})
    row = facts(record, grade).get()
    assert row.value_as_string == '3' and row.value_as_concept_id is None


def test_a_number_sent_for_a_stored_value_is_its_text(staff, record, grade, concepts):
    patch(staff, record, {'gleason_grade_group': 2})
    assert PatientRecord.objects.get(pk=record.pk).custom_fields == {'gleason_grade_group': '2'}
    assert facts(record, grade).get().value_as_concept_id == concepts['two'].pk


@pytest.mark.parametrize('value', ['Grade group 1', '7', 'grade group 2', True, ['1']])
def test_a_value_that_is_not_a_stored_value_is_refused(staff, record, grade, value):
    response = patch(staff, record, {'gleason_grade_group': value}, expect=400)
    assert 'gleason_grade_group' in response.data['custom_fields']
    assert PatientRecord.objects.get(pk=record.pk).custom_fields in (None, {})
    assert not facts(record, grade).exists()


def test_the_refusal_lists_the_answers(staff, record, grade):
    response = patch(staff, record, {'gleason_grade_group': 'nope'}, expect=400)
    message = response.data['custom_fields']['gleason_grade_group'][0]
    assert '1, 2, 3' in message


def test_clearing_a_dropdown_leaves_no_key(staff, record, grade):
    patch(staff, record, {'gleason_grade_group': '1'})
    patch(staff, record, {'gleason_grade_group': None})
    assert 'gleason_grade_group' not in (PatientRecord.objects.get(pk=record.pk).custom_fields or {})


def test_a_renamed_answer_keeps_saving_its_frozen_stored_value(staff, record, grade, concepts):
    choice = FieldChoice.objects.get(field_name='gleason_grade_group', display='Grade group 3')
    choice.value = ''
    choice.save()
    response = staff.patch(f'{CHOICES}{choice.pk}/', {'display': 'Grade group three'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['stored_value'] == 'Grade group 3'

    options = staff.get(CUSTOM).data[0]['options']
    assert options[2] == {'value': 'Grade group 3', 'code': None, 'label': 'Grade group three'}
    patch(staff, record, {'gleason_grade_group': 'Grade group 3'})
    assert PatientRecord.objects.get(pk=record.pk).custom_fields == {'gleason_grade_group': 'Grade group 3'}
    # The other answers kept the value they were created with.
    patch(staff, record, {'gleason_grade_group': '2'})


def test_a_retired_answer_is_no_longer_accepted_but_a_stored_one_stays(staff, record, grade):
    patch(staff, record, {'gleason_grade_group': '1'})
    choice = FieldChoice.objects.get(field_name='gleason_grade_group', display='Grade group 1')
    field_choices.set_retired(choice, True)

    assert [o['value'] for o in staff.get(CUSTOM).data[0]['options']] == ['2', '3']
    patch(staff, record, {'gleason_grade_group': '1'})   # unchanged, so no edit and no refusal
    refresh_patient_record(record.person)
    assert PatientRecord.objects.get(pk=record.pk).custom_fields == {'gleason_grade_group': '1'}
    other = PatientRecordFactory()
    patch(staff, other, {'gleason_grade_group': '1'}, expect=400)


def test_a_dropdown_is_read_only_for_a_caller_who_cannot_write_it(grade, record):
    entry = as_role('analyst').get(WRITABLE).data['gleason_grade_group']
    # Whatever the caller, the options are the field's own.
    assert [o['value'] for o in entry['options']] == ['1', '2', '3']


# --------------------------------------------------------------- derive-back


def derived(record):
    refresh_patient_record(record.person)
    return PatientRecord.objects.get(pk=record.pk).custom_fields or {}


def imported(record, grade, **values):
    return ObservationFactory(
        person=record.person, observation_concept=grade.mapping.concept,
        observation_source_value='LOCAL-GRADE', observation_date=values.pop('when', '2024-03-01'),
        **values)


def test_a_fact_with_the_answer_concept_and_no_text_reads_back_as_the_answer(record, grade, concepts):
    imported(record, grade, value_as_concept=concepts['two'])
    assert derived(record) == {'gleason_grade_group': '2'}


def test_a_fact_with_text_reads_back_by_value_label_or_spelling_alone(record, grade):
    imported(record, grade, value_as_string='Grade Group  1')
    assert derived(record) == {'gleason_grade_group': '1'}


def test_an_answer_with_no_concept_reads_back_by_its_text(record, grade):
    imported(record, grade, value_as_string='3')
    assert derived(record) == {'gleason_grade_group': '3'}


def test_a_value_that_matches_no_answer_is_not_written(record, grade):
    other = ConceptFactory(concept_code='A-OTHER')
    imported(record, grade, value_as_string='Grade group 9', value_as_concept=other)
    assert derived(record) == {}


def test_an_unmatched_newer_fact_does_not_hide_an_older_answer(record, grade):
    imported(record, grade, value_as_string='2', when='2024-01-01')
    imported(record, grade, value_as_string='unknown', when='2024-06-01')
    assert derived(record) == {'gleason_grade_group': '2'}


def test_the_latest_matching_fact_answers(record, grade):
    imported(record, grade, value_as_string='1', when='2024-01-01')
    imported(record, grade, value_as_string='2', when='2024-06-01')
    assert derived(record) == {'gleason_grade_group': '2'}


def test_a_text_that_two_answers_share_is_not_guessed(record, grade):
    # The same label in two contexts with different values: the editor allows it
    # (a staging system's "I" and another's), and the text alone cannot say which.
    FieldChoice.objects.create(field_name='gleason_grade_group', display='Grade group 3',
                               context_key='other', value='three')
    imported(record, grade, value_as_string='Grade group 3')
    assert derived(record) == {}


def test_a_value_entered_in_the_editor_survives_a_derivation(staff, record, grade, concepts):
    patch(staff, record, {'gleason_grade_group': '2'})
    assert derived(record) == {'gleason_grade_group': '2'}


def test_a_dropdown_in_the_measurement_table_projects_and_reads_back(staff, concepts, record):
    create(staff, concepts, field_name='psa_band', omop_table='measurement', answers=[
        {'label': 'Low', 'value': 'low', 'concept': concepts['one'].concept_id},
        {'label': 'High', 'value': 'high'}])
    field = CustomPatientField.objects.get(field_name='psa_band')
    patch(staff, record, {'psa_band': 'low'})
    from omop_core.models import Measurement
    row = Measurement.objects.get(person=record.person, measurement_source_value='custom:psa_band')
    assert row.value_as_string == 'low' and row.value_as_concept_id == concepts['one'].pk

    MeasurementFactory(person=record.person, measurement_concept=field.mapping.concept,
                       measurement_source_value='LOCAL-PSA', measurement_date='2030-01-01',
                       value_as_string='High')
    assert derived(record) == {'psa_band': 'high'}


def test_an_approved_decision_changed_later_is_what_the_next_save_writes(staff, record, grade, concepts):
    from omop_core.services import field_value_mappings
    choice = FieldChoice.objects.get(field_name='gleason_grade_group', display='Grade group 3')
    proposal = field_value_mappings.propose(choice, concept_id=concepts['two'].pk, user=None)
    field_value_mappings.approve(proposal, user=None)
    patch(staff, record, {'gleason_grade_group': '3'})
    assert facts(record, grade).get().value_as_concept_id == concepts['two'].pk


# ------------------------------------------------------- scope and provisional


def test_a_proposal_fills_a_dropdown_only_with_one_of_its_answers(record, grade, settings, concepts):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    from omop_core.models import PatientRecordConfidence, PatientRecordProvisional
    imported(record, grade, value_as_string='Grade group 2', confidence=82)
    refresh_patient_record(record.person)
    assert PatientRecordProvisional.objects.get(person=record.person).custom_fields == {
        'gleason_grade_group': '2'}
    assert PatientRecordConfidence.objects.get(person=record.person).custom_fields == {
        'gleason_grade_group': 82}
    assert not (PatientRecord.objects.get(pk=record.pk).custom_fields or {})


def test_a_proposal_that_is_not_an_answer_shows_nothing(record, grade, settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    from omop_core.models import PatientRecordProvisional
    imported(record, grade, value_as_string='Grade group 12', confidence=90)
    refresh_patient_record(record.person)
    shown = PatientRecordProvisional.objects.filter(person=record.person).first()
    assert not (shown.custom_fields if shown else None)


def test_an_answer_can_be_confirmed_from_a_proposal_through_the_patch(staff, record, grade, settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    imported(record, grade, value_as_string='1', confidence=82)
    refresh_patient_record(record.person)
    patch(staff, record, {'gleason_grade_group': '1'})
    assert PatientRecord.objects.get(pk=record.pk).custom_fields == {'gleason_grade_group': '1'}


# ------------------------------------- the LOINC Gleason grade group shape (Measurement)


@pytest.fixture
def loinc_gleason():
    """LOINC 94734-1 as a Measurement, its answers LOINC answer concepts in the Meas Value domain."""
    loinc = VocabularyFactory(vocabulary_id='LOINC')
    measurement = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    meas_value = DomainFactory(domain_id='Meas Value', domain_name='Meas Value')
    question = ConceptFactory(concept_id=36660206, concept_code='94734-1', vocabulary=loinc,
                              domain=measurement, concept_name='Gleason grade group')
    answers = {
        n: ConceptFactory(concept_id=cid, concept_code=code, vocabulary=loinc, domain=meas_value,
                          concept_name=f'Grade Group {n}')
        for n, cid, code in [(1, 36662377, 'LA30794-4'), (2, 36662565, 'LA30795-1'),
                             (3, 36662476, 'LA30796-9'), (4, 36662300, 'LA30797-7')]
    }
    return question, answers


@pytest.fixture
def gleason(staff, loinc_gleason):
    question, answers = loinc_gleason
    response = staff.post(CUSTOM, {
        'confirm_patient_record': True, 'field_name': 'gleason_grade_group',
        'display_name': 'Gleason grade group', 'field_type': 'choice', 'tab': 'disease',
        'concept': question.pk, 'omop_table': 'Measurement',
        'answers': [
            *({'label': f'Grade Group {n}', 'value': str(n), 'concept': answers[n].pk}
              for n in (1, 2, 3, 4)),
            # Grade group 5 has no standalone standard concept: an answer may have none.
            {'label': 'Grade Group 5', 'value': '5'},
        ],
    }, format='json')
    assert response.status_code == 201, response.data
    return CustomPatientField.objects.get(field_name='gleason_grade_group')


def test_a_measurement_fact_with_the_answer_concept_and_text_reads_back_as_the_answer(
        record, gleason, loinc_gleason):
    _, answers = loinc_gleason
    MeasurementFactory(person=record.person, measurement_concept=gleason.mapping.concept,
                       measurement_source_value='LOCAL-GLEASON', measurement_date='2024-03-01',
                       value_as_concept=answers[3], value_as_string='Grade Group 3')
    assert derived(record) == {'gleason_grade_group': '3'}


def test_a_measurement_fact_with_only_the_concept_or_only_other_text_reads_by_the_concept(
        record, gleason, loinc_gleason):
    _, answers = loinc_gleason
    MeasurementFactory(person=record.person, measurement_concept=gleason.mapping.concept,
                       measurement_source_value='LOCAL-GLEASON', measurement_date='2024-03-01',
                       value_as_concept=answers[4], value_as_string='GG IV (pathology report)')
    assert derived(record) == {'gleason_grade_group': '4'}


def test_saving_grade_groups_writes_the_measurement_with_and_without_a_concept(
        staff, record, gleason, loinc_gleason):
    from omop_core.models import Measurement
    _, answers = loinc_gleason
    patch(staff, record, {'gleason_grade_group': '2'})
    row = Measurement.objects.get(person=record.person, measurement_source_value='custom:gleason_grade_group')
    assert (row.measurement_concept_id, row.value_as_concept_id, row.value_as_string) == (
        36660206, answers[2].pk, '2')
    # Grade group 5 has no concept: text only, and it reads back.
    patch(staff, record, {'gleason_grade_group': '5'})
    row.refresh_from_db()
    assert (row.value_as_concept_id, row.value_as_string) == (None, '5')
    assert derived(record) == {'gleason_grade_group': '5'}


# ------------------------------------------------------------ review follow-ups


def test_a_value_entered_under_an_answer_that_was_deleted_still_shows(staff, record, grade):
    patch(staff, record, {'gleason_grade_group': '2'})
    FieldChoice.objects.get(field_name='gleason_grade_group', display='Grade group 2').delete()
    assert derived(record) == {'gleason_grade_group': '2'}
    # An imported fact of an answer that no longer exists is not read.
    other = PatientRecordFactory()
    imported(other, grade, value_as_string='Grade group 2')
    assert derived(other) == {}


def test_a_proposal_of_a_retired_answer_is_not_shown_since_it_could_not_be_confirmed(
        record, grade, settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    from omop_core.models import PatientRecordProvisional
    field_choices.set_retired(
        FieldChoice.objects.get(field_name='gleason_grade_group', display='Grade group 1'), True)
    imported(record, grade, value_as_string='1', confidence=82)
    refresh_patient_record(record.person)
    shown = PatientRecordProvisional.objects.filter(person=record.person).first()
    assert not (shown.custom_fields if shown else None)
    # The same fact, approved, still reads: stored data keeps its meaning.
    other = PatientRecordFactory()
    imported(other, grade, value_as_string='1')
    assert derived(other) == {'gleason_grade_group': '1'}


def test_a_name_with_answers_left_by_a_removed_field_is_refused(staff, concepts):
    FieldChoice.objects.create(field_name='gleason_grade_group', display='Old answer')
    response = create(staff, concepts, expect=400)
    assert 'field_name' in response.data
    assert not CustomPatientField.objects.exists()


def test_the_number_of_answers_is_capped(staff, concepts):
    response = create(staff, concepts, expect=400,
                      answers=[{'label': f'Answer {n}'} for n in range(201)])
    assert 'answers' in response.data
