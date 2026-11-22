"""Facts about a patient for PROlog questionnaires (patient_portal.prolog_facts;
PROlog DEF-14, RUN-23)."""
import copy
import json

import pytest
from rest_framework.test import APIClient

from omop_oncology.models import TherapyLineDetail
from patient_portal import prolog_facts
from patient_portal import survey_record_mapping as srm
from prolog_surveys.definitions.loader import load_definition
from prolog_surveys.models import SurveyResponse
from tests.test_survey_record_mapping import ANSWERS, DEFINITION, MAPPING, patient, submit  # noqa: F401

pytestmark = pytest.mark.django_db

FACT = 'latest_therapy_line_response'

# The treatment's response, and whether it had been assessed: "unsure" stands
# for "not assessed yet" in the example questionnaire.
ASSESSED = {'complete': True, 'unsure': False}


@pytest.fixture
def mapped(settings, tmp_path):
    """The example questionnaire, with its line's response_assessed mapped."""
    mapping = copy.deepcopy(MAPPING)
    mapping['therapy_lines'][0]['details'].append({'field': 'response_assessed', 'answer': 'response', 'values': ASSESSED})
    (tmp_path / 'history.json').write_text(json.dumps(mapping))
    settings.PROLOG_RECORD_MAPPING_DIRS = [str(tmp_path)]
    settings.CELERY_BROKER_URL = ''
    srm.reset_cache()
    yield load_definition(json.loads(json.dumps(DEFINITION)), activate=True).version
    srm.reset_cache()


CHECKIN = {
    'schema_version': 1, 'slug': 'facts-checkin', 'version': '1.0', 'status': 'draft',
    'default_language': 'en', 'languages': ['en'], 'title': {'en': 'Check-in'},
    'participation': {'anonymous': False, 'resume': 'account'},
    'facts': [{'key': FACT, 'values': ['assessed', 'not_assessed']}],
    'sections': [{'key': 's1', 'title': {'en': 'Check-in'}, 'questions': [
        {'key': 'how', 'type': 'single', 'text': {'en': 'How are you?'}, 'required': False,
         'options': [{'key': 'well', 'label': {'en': 'Well'}}, {'key': 'unwell', 'label': {'en': 'Unwell'}}]},
        {'key': 'response_now', 'type': 'single', 'text': {'en': 'What were you told about the response?'},
         'required': False, 'visible_if': [{'fact': FACT, 'op': 'eq', 'value': 'not_assessed'}],
         'options': [{'key': 'complete', 'label': {'en': 'Complete'}}, {'key': 'unsure', 'label': {'en': 'Not sure'}}]},
    ]}],
}


def start_checkin(identity):
    load_definition(json.loads(json.dumps(CHECKIN)), activate=True)
    client = APIClient()
    client.force_authenticate(identity)
    r = client.post('/api/v1/prolog/run/responses/', {'slug': 'facts-checkin', 'language': 'en'}, format='json')
    assert r.status_code == 201, r.content
    return SurveyResponse.objects.get(pk=r.json()['id'])


def test_the_mapping_records_whether_the_response_was_assessed(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient

    submit(mapped, record, {**ANSWERS, 'response': {'option': 'unsure'}}, django_capture_on_commit_callbacks)

    assert TherapyLineDetail.objects.get(episode__person=record.person).response_assessed is False


@pytest.mark.parametrize('answer,fact', [('unsure', 'not_assessed'), ('complete', 'assessed')])
def test_the_latest_lines_response_is_a_fact(mapped, patient, django_capture_on_commit_callbacks, answer, fact):
    record, _ = patient
    submit(mapped, record, {**ANSWERS, 'response': {'option': answer}}, django_capture_on_commit_callbacks)

    assert prolog_facts.facts_for(record.person_id, CHECKIN) == {FACT: fact}


def test_no_line_and_no_declared_fact_give_no_facts(patient):
    record, _ = patient

    assert prolog_facts.facts_for(record.person_id, CHECKIN) == {}
    assert prolog_facts.facts_for(record.person_id, {**CHECKIN, 'facts': [{'key': 'other', 'values': ['x']}]}) == {}


def test_a_check_in_started_after_an_unassessed_response_asks_about_it(
    mapped, patient, django_capture_on_commit_callbacks,
):
    record, identity = patient
    submit(mapped, record, {**ANSWERS, 'response': {'option': 'unsure'}}, django_capture_on_commit_callbacks)

    response = start_checkin(identity)

    assert response.facts == {FACT: 'not_assessed'}
    questions = response.definition['sections'][0]['questions']
    assert questions[1]['visible_if'] == [{'const': True}]


def test_a_check_in_started_without_a_line_does_not(patient):
    _, identity = patient

    response = start_checkin(identity)

    assert response.facts == {}
    assert response.definition['sections'][0]['questions'][1]['visible_if'] == [{'const': False}]
