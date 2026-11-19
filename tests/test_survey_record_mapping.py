"""Questionnaire answers into the patient's own record (patient_portal.survey_record_mapping)."""
import datetime as dt
import json

import pytest
from django.utils import timezone

from omop_core.models import PatientRecord, RecordRevision
from omop_oncology.models import Episode, TherapyLineAssertion
from patient_portal import survey_record_mapping as srm
from patient_portal.models import Identity, PatientUser
from prolog_surveys.definitions.loader import load_definition
from prolog_surveys.models import ResponseStatus, SurveyResponse
from tests.factories import ConceptFactory, PatientRecordFactory, VocabularyFactory
from tests.test_episode_service import _seed_episode_writer_concepts

pytestmark = pytest.mark.django_db

SLUG = 'history-example'


def _option(key):
    return {'key': key, 'label': {'en': key}}


DEFINITION = {
    'schema_version': 1, 'slug': SLUG, 'version': '1.0', 'status': 'draft',
    'default_language': 'en', 'languages': ['en'], 'title': {'en': 'History'},
    'sections': [{'key': 's1', 'title': {'en': 'History'}, 'questions': [
        {'key': 'diagnosed', 'type': 'date', 'text': {'en': 'When were you diagnosed?'}, 'required': False,
         'config': {'precision': ['month', 'year']}, 'options': [_option('not_sure')]},
        {'key': 'stage', 'type': 'single', 'text': {'en': 'Stage?'}, 'required': False,
         'options': [_option('stage_3'), _option('not_told')]},
        {'key': 'activity', 'type': 'single', 'text': {'en': 'Activity?'}, 'required': False,
         'options': [_option('level_0'), _option('level_1')]},
        {'key': 'treatment', 'type': 'single', 'text': {'en': 'Treatment?'}, 'required': False,
         'options': [_option('chemo'), _option('not_sure')]},
        {'key': 'treatment_names', 'type': 'text', 'text': {'en': 'Names?'}, 'required': False},
        {'key': 'started', 'type': 'date', 'text': {'en': 'Started?'}, 'required': False,
         'config': {'precision': ['month', 'year']}},
        {'key': 'ended', 'type': 'date', 'text': {'en': 'Ended?'}, 'required': False,
         'config': {'precision': ['month', 'year']}, 'options': [_option('ongoing')]},
        {'key': 'response', 'type': 'single', 'text': {'en': 'Response?'}, 'required': False,
         'options': [_option('complete'), _option('unsure')]},
    ]}],
}

MAPPING = {
    'mapping_version': 1, 'survey': SLUG, 'versions': ['1.0'],
    'record': [
        {'field': 'diagnosis_date', 'answer': 'diagnosed', 'precision_field': 'diagnosis_date_precision'},
        {'field': 'stage', 'answer': 'stage', 'values': {'stage_3': 'III'}},
        {'field': 'ecog_performance_status', 'answer': 'activity', 'values': {'level_0': 0, 'level_1': 1}},
        {'field': 'ecog_assessment_date', 'submitted_on': True,
         'when': [{'question': 'activity', 'op': 'answered'}]},
    ],
    'therapy_lines': [
        {'line': 1, 'regimen': {'answer': 'treatment', 'labels': {'chemo': 'Chemotherapy', 'not_sure': 'Not sure'}},
         'names': 'treatment_names', 'start_date': 'started', 'end_date': 'ended',
         'outcome': {'answer': 'response', 'values': {'complete': 'CR'}}},
    ],
}


@pytest.fixture
def mapped(settings, tmp_path):
    """The example survey, loaded and active, with its mapping in place."""
    (tmp_path / 'history.json').write_text(json.dumps(MAPPING))
    settings.PROLOG_RECORD_MAPPING_DIRS = [str(tmp_path)]
    settings.CELERY_BROKER_URL = ''
    srm.reset_cache()
    yield load_definition(json.loads(json.dumps(DEFINITION)), activate=True).version
    srm.reset_cache()


@pytest.fixture
def patient():
    _seed_episode_writer_concepts()
    ConceptFactory(concept_id=32865, concept_name='Patient self-report', concept_code='32865',
                   vocabulary=VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept'))
    record = PatientRecordFactory(disease='Follicular Lymphoma', stage='')
    identity = Identity.objects.create_user(email=f'p{record.person_id}@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    return record, identity


def submit(version, record, answers, django_capture_on_commit_callbacks):
    """A response with these answers, submitted the way PROlog's submit saves it."""
    response = SurveyResponse.objects.create(survey_version=version, participant_id=record.person_id, language='en')
    for key, value in answers.items():
        response.answers.create(question_key=key, value=value, option_keys=[value['option']] if 'option' in value else [])
    with django_capture_on_commit_callbacks(execute=True):
        response.status = ResponseStatus.SUBMITTED
        response.submitted_at = timezone.now()
        response.save(update_fields=['status', 'submitted_at', 'updated_at'])
    return response


ANSWERS = {
    'diagnosed': {'date': '2019-03'},
    'stage': {'option': 'stage_3'},
    'activity': {'option': 'level_1'},
    'treatment': {'option': 'chemo'},
    'treatment_names': {'text': 'R-CHOP'},
    'started': {'date': '2019-06'},
    'ended': {'date': '2019-11'},
    'response': {'option': 'complete'},
}


def test_a_submitted_response_writes_the_record_as_the_patient(mapped, patient, django_capture_on_commit_callbacks):
    record, identity = patient

    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)

    record = PatientRecord.objects.get(pk=record.pk)
    assert record.diagnosis_date == dt.date(2019, 3, 1)
    assert record.diagnosis_date_precision == 'month'
    assert record.stage == 'III'
    assert record.ecog_performance_status == 1
    assert record.ecog_assessment_date == timezone.localdate()
    # The patient's own edit: their revision history names them.
    revisions = RecordRevision.objects.filter(patient_record=record, field='stage')
    assert [r.changed_by for r in revisions] == [str(identity.pk)]


def test_a_treatment_becomes_a_line_the_patient_reported(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient

    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)

    line = Episode.objects.get(person=record.person, episode_number=1)
    assert line.episode_source_value == 'Chemotherapy (R-CHOP)'
    assert line.episode_start_date == dt.date(2019, 6, 1)
    assert line.episode_end_date == dt.date(2019, 11, 1)
    assert line.episode_type_concept_id == 32865
    assert TherapyLineAssertion.objects.get(episode=line).source == 'questionnaire'
    # The record reads the line back, its response included.
    record = PatientRecord.objects.get(pk=record.pk)
    assert record.first_line_outcome == 'Complete Response'
    assert record.first_line_start_date == dt.date(2019, 6, 1)


def test_the_patient_sees_the_line_as_theirs(mapped, patient, django_capture_on_commit_callbacks):
    from rest_framework.test import APIClient

    record, identity = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    client = APIClient()
    client.force_authenticate(identity)

    lines = client.get('/api/v1/phr/therapy/').data['tracks'][0]['lines']

    assert [(line['regimen'], line['source']['kind']) for line in lines] == [('Chemotherapy (R-CHOP)', 'patient')]


def test_only_what_an_answer_gives_is_written(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    PatientRecord.objects.filter(pk=record.pk).update(stage='II')

    submit(mapped, record, {
        'diagnosed': {'option': 'not_sure'},   # an answer instead of a date
        'stage': {'option': 'not_told'},       # an option the mapping does not list
        'activity': {'skipped': True},          # skipped: the `when` does not hold either
        'ended': {'option': 'ongoing'},
    }, django_capture_on_commit_callbacks)

    record = PatientRecord.objects.get(pk=record.pk)
    assert record.stage == 'II'  # never cleared
    assert record.diagnosis_date is None
    assert record.ecog_performance_status is None
    assert record.ecog_assessment_date is None
    assert not Episode.objects.filter(person=record.person, episode_number=1).exists()


def test_applying_again_writes_the_same_record(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    response = submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)

    srm.apply_response(response)

    assert Episode.objects.filter(person=record.person, episode_number=1).count() == 1
    assert PatientRecord.objects.get(pk=record.pk).stage == 'III'


def test_nothing_is_written_without_a_mapping(settings, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    settings.PROLOG_RECORD_MAPPING_DIRS = []
    srm.reset_cache()
    version = load_definition(json.loads(json.dumps(DEFINITION)), activate=True).version

    submit(version, record, ANSWERS, django_capture_on_commit_callbacks)

    assert PatientRecord.objects.get(pk=record.pk).stage == ''
    assert not Episode.objects.filter(person=record.person).exists()


def test_nothing_is_written_for_a_participant_without_an_account(mapped, django_capture_on_commit_callbacks):
    record = PatientRecordFactory(stage='')

    response = submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)

    assert srm.apply_response(response) is None
    assert PatientRecord.objects.get(pk=record.pk).stage == ''


def test_a_mapping_naming_no_record_field_is_refused():
    with pytest.raises(srm.MappingError, match='PatientRecord field'):
        srm.check_mapping({**MAPPING, 'record': [{'field': 'not_a_field', 'value': 1}]})
    with pytest.raises(srm.MappingError, match='exactly one'):
        srm.check_mapping({**MAPPING, 'record': [{'field': 'stage', 'value': 'I', 'submitted_on': True}]})


def test_a_line_label_must_fit_the_line_and_long_names_are_left_out():
    long_label = {**MAPPING['therapy_lines'][0], 'regimen': {'answer': 'treatment', 'labels': {'chemo': 'x' * 51}}}
    with pytest.raises(srm.MappingError, match='labels longer'):
        srm.check_mapping({**MAPPING, 'therapy_lines': [long_label]})

    lines = srm.therapy_lines(MAPPING, {**ANSWERS, 'treatment_names': {'text': 'y' * 45}})

    assert lines[0]['source_value'] == 'Chemotherapy'


def test_a_mapping_is_checked_against_its_survey_version():
    mapping = {**MAPPING, 'record': [
        *MAPPING['record'],
        {'field': 'stage', 'answer': 'stage', 'values': {'stage_9': 'IX'}},
        {'field': 'disease', 'value': 'x', 'when': [{'question': 'missing', 'op': 'answered'}]},
    ]}

    problems = srm.check_against_definition(mapping, DEFINITION)

    assert problems == [
        "record[4]: 'stage' has no option 'stage_9'",
        "record[5].when: no question 'missing'",
    ]
    assert srm.check_against_definition(MAPPING, DEFINITION) == []


@pytest.mark.parametrize('value, expected', [
    ({'date': '2019'}, (dt.date(2019, 1, 1), 'year')),
    ({'date': '2019-06'}, (dt.date(2019, 6, 1), 'month')),
    ({'date': '2019-06-15'}, (dt.date(2019, 6, 15), 'day')),
    ({'option': 'not_sure'}, None),
    ({'date': '2019-13'}, None),
    (None, None),
])
def test_answer_date(value, expected):
    assert srm.answer_date(value) == expected


def test_the_command_checks_mappings_and_backfills_earlier_responses(mapped, patient, settings, tmp_path, capsys):
    from django.core.management import call_command

    record, _ = patient
    # Submitted before the mapping existed: nothing applied it.
    response = SurveyResponse.objects.create(survey_version=mapped, participant_id=record.person_id, language='en',
                                             status=ResponseStatus.SUBMITTED, submitted_at=timezone.now())
    response.answers.create(question_key='stage', value={'option': 'stage_3'}, option_keys=['stage_3'])

    call_command('apply_record_mappings', '--check')
    call_command('apply_record_mappings', '--survey', SLUG)

    assert 'OK: 1 mapped survey version(s)' in capsys.readouterr().out
    assert PatientRecord.objects.get(pk=record.pk).stage == 'III'
