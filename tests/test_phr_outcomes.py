"""/api/v1/phr/outcomes/ — My outcomes: the patient's PROlog outcome surveys."""
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.utils import timezone

from prolog_surveys.definitions.loader import load_definition
from prolog_surveys.models import ResponseStatus, SurveyResponse
from tests.factories import PatientRecordFactory
from tests.test_phr_read_model import signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''
    settings.PHR_OUTCOME_SURVEYS = ['quality-of-life', 'side-effects']


def survey(slug, title, *, activate=True, minutes=10):
    return load_definition({
        'schema_version': 1, 'slug': slug, 'version': '1.0', 'status': 'draft',
        'default_language': 'en', 'languages': ['en'], 'title': {'en': title}, 'estimated_minutes': minutes,
        'sections': [{'key': 's1', 'title': {'en': title}, 'questions': [
            {'key': 'q1', 'type': 'single', 'text': {'en': 'Were you tired?'},
             'options': [{'key': 'no', 'label': {'en': 'No'}}, {'key': 'yes', 'label': {'en': 'Yes'}}]}]}],
    }, activate=activate).version


def answered(version, person, days_ago):
    submitted = timezone.now() - timedelta(days=days_ago)
    return SurveyResponse.objects.create(
        survey_version=version, participant_id=person.person_id, language='en',
        status=ResponseStatus.SUBMITTED, submitted_at=submitted,
    )


def test_outcomes_list_each_survey_due_a_year_after_it_was_last_completed():
    record = PatientRecordFactory(disease='')
    other = PatientRecordFactory(disease='')
    quality = survey('quality-of-life', 'Quality of life survey')
    effects = survey('side-effects', 'Side effects survey', minutes=20)
    answered(quality, record.person, days_ago=800)
    latest = answered(quality, record.person, days_ago=400)
    answered(effects, other.person, days_ago=10)  # someone else's
    draft = SurveyResponse.objects.create(survey_version=effects, participant_id=record.person.person_id, language='en')
    client = signed_in(record)

    data = client.get('/api/v1/phr/outcomes/').data

    quality_row, effects_row = data['surveys']
    last = timezone.localdate(latest.submitted_at)
    assert quality_row == {
        'slug': 'quality-of-life',
        'title': 'Quality of life survey',
        'estimated_minutes': 10,
        'status': 'overdue',
        'last_completed': last.isoformat(),
        'next_due': last.replace(year=last.year + 1).isoformat(),
        'completed': 2,
        'latest_response': str(latest.id),
        'draft_response': None,
    }
    assert effects_row['status'] == 'due_now'
    assert effects_row['completed'] == 0 and effects_row['last_completed'] is None
    assert effects_row['draft_response'] == str(draft.id)
    assert client.get('/api/v1/phr/status/').data['sections']['outcomes'] == 'ready'


def test_a_survey_completed_this_year_is_upcoming():
    record = PatientRecordFactory(disease='')
    effects = survey('side-effects', 'Side effects survey')
    answered(effects, record.person, days_ago=30)

    row = signed_in(record).get('/api/v1/phr/outcomes/').data['surveys'][0]

    assert row['status'] == 'upcoming'
    assert row['next_due'] > timezone.localdate().isoformat()


def test_only_active_outcome_surveys_show():
    record = PatientRecordFactory(disease='')
    survey('quality-of-life', 'Quality of life survey', activate=False)  # a draft
    survey('symptom-check', 'Something else')  # not an outcome survey
    client = signed_in(record)

    assert client.get('/api/v1/phr/outcomes/').data == {'surveys': []}
    assert client.get('/api/v1/phr/status/').data['sections']['outcomes'] == 'empty'


def test_a_year_after_29_february_is_28_february():
    from patient_portal.api.phr.outcomes import _a_year_after

    assert _a_year_after(datetime(2024, 2, 29, tzinfo=dt_timezone.utc).date()).isoformat() == '2025-02-28'


def test_outcomes_need_a_signed_in_patient():
    from rest_framework.test import APIClient

    assert APIClient().get('/api/v1/phr/outcomes/').status_code in (401, 403)
