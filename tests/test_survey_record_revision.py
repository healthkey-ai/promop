"""An update of a revisable questionnaire starts from the patient's last answers
with what their record says differently now (patient_portal.survey_record_revision),
and submitting it takes out the patient's own lines it no longer gives
(survey_record_mapping)."""
import copy
import datetime as dt
import json

import pytest
from django.utils import timezone

from omop_core.models import Observation, PatientRecord
from omop_oncology.models import Episode, TherapyLineAssertion, TherapyLineDetail
from patient_portal import survey_record_mapping as srm
from patient_portal.survey_record_revision import revision_answers
from prolog_surveys.definitions.loader import load_definition
from prolog_surveys.models import ResponseStatus, SurveyResponse
from tests.test_survey_record_mapping import ANSWERS, DEFINITION, mapped, patient, submit  # noqa: F401

pytestmark = pytest.mark.django_db


def _line(record, number=1):
    return Episode.objects.get(person=record.person, episode_number=number)


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


# --- the one-line example ------------------------------------------------------

def test_an_unchanged_record_gives_back_the_last_answers(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)

    assert revision_answers(record.person_id, DEFINITION, dict(ANSWERS)) == ANSWERS


def test_what_the_record_changed_since_replaces_the_last_answer(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    # Since then (a check-in): the treatment ended later than first said.
    line = _line(record)
    Episode.objects.filter(pk=line.pk).update(episode_end_date=dt.date(2020, 3, 1))
    TherapyLineDetail.objects.filter(episode=line).update(end_date_precision='month')

    answers = revision_answers(record.person_id, DEFINITION, dict(ANSWERS))

    assert answers == {**ANSWERS, 'ended': {'date': '2020-03'}}


def test_a_question_left_unanswered_is_filled_from_the_record(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    without_stage = {k: v for k, v in ANSWERS.items() if k != 'stage'}
    submit(mapped, record, without_stage, django_capture_on_commit_callbacks)
    PatientRecord.objects.filter(pk=record.pk).update(stage='III')  # told since

    assert revision_answers(record.person_id, DEFINITION, without_stage)['stage'] == {'option': 'stage_3'}


def test_an_answer_that_writes_nothing_is_kept(mapped, patient, django_capture_on_commit_callbacks):
    """"Not told" writes no stage; a stage in the record may be one from before."""
    record, _ = patient
    told_nothing = {**ANSWERS, 'stage': {'option': 'not_told'}}
    submit(mapped, record, told_nothing, django_capture_on_commit_callbacks)
    PatientRecord.objects.filter(pk=record.pk).update(stage='III')

    assert revision_answers(record.person_id, DEFINITION, told_nothing)['stage'] == {'option': 'not_told'}


def test_an_answer_the_record_now_contradicts_is_asked_again(mapped, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    still = {**ANSWERS, 'ended': {'option': 'ongoing'}}
    submit(mapped, record, still, django_capture_on_commit_callbacks)
    # A check-in said it finished, without saying when.
    TherapyLineDetail.objects.filter(episode=_line(record)).update(ongoing=False)

    answers = revision_answers(record.person_id, DEFINITION, still)

    assert 'ended' not in answers
    assert answers['treatment'] == {'option': 'chemo'}


def test_the_record_off_means_the_last_answers(mapped, patient, django_capture_on_commit_callbacks, monkeypatch):
    record, _ = patient
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    TherapyLineDetail.objects.filter(episode=_line(record)).update(ongoing=True)
    monkeypatch.setattr('patient_portal.consent.record_permitted', lambda person: False)

    assert revision_answers(record.person_id, DEFINITION, dict(ANSWERS)) == ANSWERS


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


# --- a profile shaped like a deployment's: four lines behind gates ---------------

PROFILE = 'profile-example'
FL = 'Follicular Lymphoma'
LABELS = {'chemo': 'Chemotherapy', 'len': 'Lenalidomide', 'not_sure': 'Not sure'}
PLANNED = 'CAR-T therapy'


def _q(key, text, options=None, qtype='single', **extra):
    q = {'key': key, 'type': qtype, 'text': {'en': text}, 'required': False, **extra}
    if options:
        q['options'] = [{'key': o, 'label': {'en': o}} for o in options]
    return q


def _date(key, text, options=(), **extra):
    return _q(key, text, list(options) or None, qtype='date', config={'precision': ['month', 'year']}, **extra)


def _profile_definition():
    sections = [{'key': 'about', 'title': {'en': 'About'}, 'questions': [
        _q('stage', 'Stage?', ['stage_3', 'not_told']),
        _q('plan', 'Planned?', ['yes', 'no']),
        _q('plan_what', 'What?', ['car_t', 'other'], visible_if=[{'question': 'plan', 'op': 'eq', 'value': 'yes'}]),
    ]}]
    for n in range(1, 5):
        t = f't{n}_'
        questions = [
            _q(t + 'treat', 'Treatment?', list(LABELS)),
            _date(t + 'start', 'Started?'),
            _date(t + 'end', 'Ended?', ['ongoing']),
            _q(t + 'resp', 'Response?', ['complete', 'partial', 'stable', 'worked_type_unknown', 'not_assessed'],
               visible_if=[{'question': t + 'end', 'op': 'neq', 'value': 'ongoing'}]),
            _q(t + 'back', 'Came back?', ['yes_once', 'yes_more', 'no']),
            _date(t + 'back_date', 'When?', visible_if=[{'question': t + 'back', 'op': 'in', 'values': ['yes_once', 'yes_more']}]),
            _q(t + 'times', 'How many times?', ['twice', 'three'],
               visible_if=[{'question': t + 'back', 'op': 'eq', 'value': 'yes_more'}]),
        ]
        if n < 4:
            questions.append(_q(f'more{n}', 'Further treatment?', ['yes', 'no']))
        section = {'key': f'line{n}', 'title': {'en': f'Treatment {n}'}, 'questions': questions}
        if n > 1:
            section['visible_if'] = [{'question': f'more{n - 1}', 'op': 'eq', 'value': 'yes'}]
        sections.append(section)
    return {
        'schema_version': 1, 'slug': PROFILE, 'version': '1.0', 'status': 'draft', 'default_language': 'en',
        'languages': ['en'], 'title': {'en': 'Profile'}, 'sections': sections,
        'participation': {'anonymous': False, 'resume': 'account', 'revisable': True},
    }


def _profile_mapping():
    record = [
        {'field': 'stage', 'answer': 'stage', 'values': {'stage_3': 'III'}},
        {'field': 'planned_therapies', 'answer': 'plan_what', 'values': {'car_t': PLANNED},
         'when': [{'question': 'plan', 'op': 'eq', 'value': 'yes'}]},
    ]
    lines = []
    for n in range(1, 5):
        t = f't{n}_'
        record.append({'field': 'relapse_count', 'answer': t + 'times', 'values': {'twice': 2, 'three': 3}})
        lines.append({
            'line': n, 'disease': FL,
            'regimen': {'answer': t + 'treat', 'labels': LABELS},
            'start_date': t + 'start', 'end_date': t + 'end', 'progression_date': t + 'back_date',
            'outcome': {'answer': t + 'resp', 'values': {'complete': 'CR', 'partial': 'PR', 'stable': 'SD'}},
            'details': [
                {'field': 'ongoing', 'answer': t + 'end', 'values': {'ongoing': True}},
                {'field': 'response_assessed', 'answer': t + 'resp', 'values': {
                    'complete': True, 'partial': True, 'stable': True, 'worked_type_unknown': True,
                    'not_assessed': False}},
            ],
        })
    return {'mapping_version': 1, 'survey': PROFILE, 'versions': ['1.0'], 'record': record, 'therapy_lines': lines}


@pytest.fixture
def profile(settings, tmp_path):
    (tmp_path / 'profile.json').write_text(json.dumps(_profile_mapping()))
    settings.PROLOG_RECORD_MAPPING_DIRS = [str(tmp_path)]
    settings.CELERY_BROKER_URL = ''
    srm.reset_cache()
    yield load_definition(_profile_definition(), activate=True).version
    srm.reset_cache()


def _treatment(n, treat, start, end, resp=None, back='no', back_date=None, times=None, more=None):
    t = f't{n}_'
    out = {t + 'treat': {'option': treat}, t + 'start': {'date': start}, t + 'back': {'option': back}}
    out[t + 'end'] = {'option': 'ongoing'} if end == 'ongoing' else {'date': end}
    if resp:
        out[t + 'resp'] = {'option': resp}
    if back_date:
        out[t + 'back_date'] = {'date': back_date}
    if times:
        out[t + 'times'] = {'option': times}
    if more:
        out[f'more{n}'] = {'option': more}
    return out


FOUR_LINES = {
    'stage': {'option': 'stage_3'}, 'plan': {'option': 'no'},
    **_treatment(1, 'chemo', '2019-06', '2019-11', 'complete', back='yes_once', back_date='2020-06', more='yes'),
    **_treatment(2, 'len', '2020-07', '2021-01', 'partial', back='yes_more', back_date='2021-06', times='twice',
                 more='yes'),
    **_treatment(3, 'chemo', '2021-07', '2022-01', 'stable', back='no', more='yes'),
    **_treatment(4, 'len', '2022-02', '2022-08', 'complete', back='no'),
}


def test_a_four_line_profile_comes_back_answer_for_answer(profile, patient, django_capture_on_commit_callbacks):
    """Several rules write relapse_count, lines 3 and 4 have different outcomes,
    and gates guard each line: an unchanged record gives back exactly what the
    patient said, and nothing they did not."""
    record, _ = patient
    submit(profile, record, FOUR_LINES, django_capture_on_commit_callbacks)
    assert sorted(Episode.objects.filter(person=record.person, episode_number__isnull=False)
                  .values_list('episode_number', flat=True)) == [1, 2, 3, 4]

    assert revision_answers(record.person_id, profile.cached_definition, dict(FOUR_LINES)) == FOUR_LINES


def test_each_line_is_read_from_its_own_outcome(profile, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(profile, record, FOUR_LINES, django_capture_on_commit_callbacks)
    # A check-in revises line 4's response only.
    Observation.objects.filter(person=record.person, observation_source_value='LOT-4-outcome',
                               observation_event_id=_line(record, 4).episode_id).update(value_as_string='Partial Response')

    answers = revision_answers(record.person_id, profile.cached_definition, dict(FOUR_LINES))

    assert answers['t4_resp'] == {'option': 'partial'}
    assert answers['t3_resp'] == {'option': 'stable'}


def test_a_count_several_questions_write_is_never_traced_to_one(profile, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    submit(profile, record, FOUR_LINES, django_capture_on_commit_callbacks)
    PatientRecord.objects.filter(pk=record.pk).update(relapse_count=3)

    answers = revision_answers(record.person_id, profile.cached_definition, dict(FOUR_LINES))

    assert answers == FOUR_LINES  # no "how many times" made up, no gate opened


def test_a_rule_whose_condition_the_last_answers_fail_says_nothing(profile, patient, django_capture_on_commit_callbacks):
    """The mapping never clears planned_therapies: an old value stays in the record after "no"."""
    record, _ = patient
    submit(profile, record, FOUR_LINES, django_capture_on_commit_callbacks)
    PatientRecord.objects.filter(pk=record.pk).update(planned_therapies=PLANNED)

    answers = revision_answers(record.person_id, profile.cached_definition, dict(FOUR_LINES))

    assert answers['plan'] == {'option': 'no'} and 'plan_what' not in answers


def test_a_gate_the_patient_answered_is_never_changed(profile, patient, django_capture_on_commit_callbacks):
    """One treatment and "no further treatment"; a check-in adds a second line."""
    from omop_core.services.episode_service import author_therapy_line
    from omop_core.services.omop_projection import patient_self_report_type

    record, _ = patient
    one = {'stage': {'option': 'stage_3'}, 'plan': {'option': 'no'},
           **_treatment(1, 'chemo', '2019-06', '2019-11', 'complete', back='no', more='no')}
    submit(profile, record, one, django_capture_on_commit_callbacks)
    author_therapy_line(record.person, line_number=2, asserted_by=srm.QUESTIONNAIRE,
                        type_concept_id=patient_self_report_type(), source_value='Lenalidomide',
                        start_date=dt.date(2026, 3, 1))

    answers = revision_answers(record.person_id, profile.cached_definition, dict(one))

    assert answers['t1_back'] == {'option': 'no'} and answers['more1'] == {'option': 'no'}


def test_a_response_since_assessed_of_an_unknown_kind_is_asked_again(profile, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    not_yet = {**FOUR_LINES, 't2_resp': {'option': 'not_assessed'}}
    submit(profile, record, not_yet, django_capture_on_commit_callbacks)
    # A check-in: it worked, of an unknown kind (no outcome).
    TherapyLineDetail.objects.filter(episode=_line(record, 2)).update(response_assessed=True)

    answers = revision_answers(record.person_id, profile.cached_definition, dict(not_yet))

    assert 't2_resp' not in answers


def test_an_update_never_retracts_a_line_another_questionnaire_added(profile, patient, django_capture_on_commit_callbacks):
    """The update started with one line; a check-in then added line 2."""
    from omop_core.services.episode_service import author_therapy_line
    from omop_core.services.omop_projection import patient_self_report_type

    record, _ = patient
    one = {'stage': {'option': 'stage_3'}, 'plan': {'option': 'no'},
           **_treatment(1, 'chemo', '2019-06', '2019-11', 'complete', back='no', more='no')}
    source = submit(profile, record, one, django_capture_on_commit_callbacks)
    result = author_therapy_line(record.person, line_number=2, asserted_by=srm.QUESTIONNAIRE,
                                 type_concept_id=patient_self_report_type(), source_value='Lenalidomide',
                                 start_date=dt.date(2026, 3, 1))
    srm._write_detail(result.episode, {}, 'a-check-in:next')

    _submit_update(profile, record, one, source, django_capture_on_commit_callbacks)

    assert TherapyLineAssertion.objects.get(episode=_line(record, 2)).state == TherapyLineAssertion.ASSERTED
    assert TherapyLineAssertion.objects.get(episode=_line(record, 1)).state == TherapyLineAssertion.ASSERTED


def test_an_update_retracts_a_later_line_it_took_out(profile, patient, django_capture_on_commit_callbacks):
    record, _ = patient
    source = submit(profile, record, FOUR_LINES, django_capture_on_commit_callbacks)
    three = {k: v for k, v in FOUR_LINES.items() if not k.startswith('t4_')}
    three['more3'] = {'option': 'no'}

    _submit_update(profile, record, three, source, django_capture_on_commit_callbacks)

    states = dict(TherapyLineAssertion.objects.filter(episode__person=record.person)
                  .values_list('episode__episode_number', 'state'))
    assert states[4] == TherapyLineAssertion.RETRACTED
    assert {states[n] for n in (1, 2, 3)} == {TherapyLineAssertion.ASSERTED}
