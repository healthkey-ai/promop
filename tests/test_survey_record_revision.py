"""An update of a revisable questionnaire starts from the patient's record as it
stands now (patient_portal.survey_record_revision), and submitting it takes out
the patient's own lines it no longer gives (survey_record_mapping)."""
import copy
import datetime as dt

import pytest
from django.utils import timezone

from omop_oncology.models import Episode, TherapyLineAssertion, TherapyLineDetail
from omop_core.models import PatientRecord
from patient_portal.survey_record_revision import revision_answers
from prolog_surveys.models import ResponseStatus, SurveyResponse
from tests.test_survey_record_mapping import ANSWERS, DEFINITION, mapped, patient, submit  # noqa: F401

pytestmark = pytest.mark.django_db


def _line(record):
    return Episode.objects.get(person=record.person, episode_number=1)


def test_an_update_starts_from_the_record_as_it_stands_now(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    # Since then (a check-in): the treatment ended later than first said.
    line = _line(record)
    Episode.objects.filter(pk=line.pk).update(episode_end_date=dt.date(2020, 3, 1))
    TherapyLineDetail.objects.filter(episode=line).update(end_date_precision='month')

    answers = revision_answers(record.person_id, DEFINITION, dict(ANSWERS))

    assert answers['ended'] == {'date': '2020-03'}
    # Read back from the record: the treatment, its names and dates, its response.
    assert answers['treatment'] == {'option': 'chemo'}
    assert answers['treatment_names'] == {'text': 'R-CHOP'}
    assert answers['started'] == {'date': '2019-06'}
    assert answers['response'] == {'option': 'complete'}
    assert answers['stage'] == {'option': 'stage_3'}
    assert answers['diagnosed'] == {'date': '2019-03'}


def test_a_record_field_set_since_replaces_the_last_answer(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(mapped, record, {**ANSWERS, 'stage': {'option': 'not_told'}}, django_capture_on_commit_callbacks)
    PatientRecord.objects.filter(pk=record.pk).update(stage='III')  # told since

    answers = revision_answers(record.person_id, DEFINITION, {**ANSWERS, 'stage': {'option': 'not_told'}})

    assert answers['stage'] == {'option': 'stage_3'}


def test_an_answer_the_record_now_contradicts_is_asked_again(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    still = {**ANSWERS, 'ended': {'option': 'ongoing'}}
    submit(mapped, record, still, django_capture_on_commit_callbacks)
    assert TherapyLineDetail.objects.get(episode=_line(record)).ongoing is True
    # A check-in said it finished, without saying when.
    TherapyLineDetail.objects.filter(episode=_line(record)).update(ongoing=False)

    answers = revision_answers(record.person_id, DEFINITION, still)

    assert 'ended' not in answers
    assert answers['treatment'] == {'option': 'chemo'}


def test_what_the_record_does_not_hold_keeps_the_last_answer(mapped, patient):
    record, _ = patient  # nothing written: the record is empty
    previous = {'activity': {'option': 'level_1'}, 'trial': {'option': 'yes'}}

    assert revision_answers(record.person_id, DEFINITION, previous) == previous


def test_an_answer_from_the_record_opens_the_way_to_itself(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    # The same questions behind a gate the last answers say no to.
    gated = copy.deepcopy(DEFINITION)
    questions = gated['sections'][0]['questions']
    at = next(i for i, q in enumerate(questions) if q['key'] == 'treatment')
    questions.insert(at, {'key': 'treated', 'type': 'single', 'text': {'en': 'Treated?'}, 'required': False,
                          'options': [{'key': 'yes', 'label': {'en': 'yes'}}, {'key': 'no', 'label': {'en': 'no'}}]})
    questions[at + 1]['visible_if'] = [{'question': 'treated', 'op': 'eq', 'value': 'yes'}]

    answers = revision_answers(record.person_id, gated, {'treated': {'option': 'no'}})

    assert answers['treated'] == {'option': 'yes'}
    assert answers['treatment'] == {'option': 'chemo'}


def test_the_record_off_means_the_last_answers(mapped, patient, django_capture_on_commit_callbacks, monkeypatch):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    monkeypatch.setattr('patient_portal.consent.record_permitted', lambda person: False)

    assert revision_answers(record.person_id, DEFINITION, {'stage': {'option': 'not_told'}}) == {
        'stage': {'option': 'not_told'}
    }


def _submit_update(version, record, answers, source, capture):
    """An update of ``source`` with these answers, submitted as PROlog submits it."""
    response = SurveyResponse.objects.create(
        survey_version=version, participant_id=record.person_id, language='en', revises=source,
    )
    for key, value in answers.items():
        response.answers.create(question_key=key, value=value, option_keys=[value['option']] if 'option' in value else [])
    with capture(execute=True):
        response.status = ResponseStatus.SUBMITTED
        response.submitted_at = timezone.now()
        response.save(update_fields=['status', 'submitted_at', 'updated_at'])
    return response


WITHOUT_THE_TREATMENT = {key: value for key, value in ANSWERS.items()
                         if key not in {'treatment', 'treatment_names', 'started', 'ended', 'response', 'trial'}}


def test_an_update_that_takes_a_treatment_out_retracts_the_patients_line(
    mapped, patient, django_capture_on_commit_callbacks,
):
    record, _ = patient
    source = submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    line = _line(record)

    _submit_update(mapped, record, WITHOUT_THE_TREATMENT, source, django_capture_on_commit_callbacks)

    assert TherapyLineAssertion.objects.get(episode=line).state == TherapyLineAssertion.RETRACTED
    assert Episode.objects.filter(pk=line.pk).exists()  # kept for audit


def test_an_update_never_retracts_a_line_from_the_ehr(mapped, patient, django_capture_on_commit_callbacks):
    from omop_core.services.episode_service import author_therapy_line

    record, _ = patient
    author_therapy_line(record.person, line_number=1, start_date=dt.date(2018, 2, 1), source_value='R-CVP')
    source = submit(mapped, record, WITHOUT_THE_TREATMENT, django_capture_on_commit_callbacks)

    _submit_update(mapped, record, WITHOUT_THE_TREATMENT, source, django_capture_on_commit_callbacks)

    assertion = TherapyLineAssertion.objects.filter(episode=_line(record)).first()
    assert assertion is None or assertion.state != TherapyLineAssertion.RETRACTED


def test_a_submission_that_is_not_an_update_retracts_nothing(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)

    submit(mapped, record, WITHOUT_THE_TREATMENT, django_capture_on_commit_callbacks)

    assert TherapyLineAssertion.objects.get(episode=_line(record)).state == TherapyLineAssertion.ASSERTED


@pytest.fixture
def named_cancer(mapped, settings):
    """The mapping with its line filed under a named cancer, as a deployment's
    mapping names the cancer its questionnaire is about."""
    import json
    from pathlib import Path

    from patient_portal import survey_record_mapping as srm
    from tests.test_survey_record_mapping import MAPPING

    mapping = copy.deepcopy(MAPPING)
    mapping['therapy_lines'][0]['disease'] = 'Follicular Lymphoma'
    (Path(settings.PROLOG_RECORD_MAPPING_DIRS[0]) / 'history.json').write_text(json.dumps(mapping))
    srm.reset_cache()
    yield mapped
    srm.reset_cache()


def test_a_line_filed_under_its_cancer_is_read_back_and_taken_out(
    named_cancer, patient, django_capture_on_commit_callbacks,
):
    record, _ = patient
    source = submit(named_cancer, record, ANSWERS, django_capture_on_commit_callbacks)
    line = _line(record)
    TherapyLineDetail.objects.filter(episode=line).update(ongoing=False)
    Episode.objects.filter(pk=line.pk).update(episode_end_date=dt.date(2020, 3, 1))

    answers = revision_answers(record.person_id, DEFINITION, dict(ANSWERS))
    assert answers['ended'] == {'date': '2020-03'}
    assert answers['treatment'] == {'option': 'chemo'}

    _submit_update(named_cancer, record, WITHOUT_THE_TREATMENT, source, django_capture_on_commit_callbacks)
    assert TherapyLineAssertion.objects.get(episode=line).state == TherapyLineAssertion.RETRACTED
