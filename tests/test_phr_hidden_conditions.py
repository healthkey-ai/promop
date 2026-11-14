"""#2568: a condition the source marks as not an active diagnosis is listed
apart, with the reason, instead of among the active ones."""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.factories import PatientRecordFactory
from tests.test_phr_read_model import condition, icd10, signed_in

pytestmark = pytest.mark.django_db

URL = '/api/v1/phr/diagnoses/'


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


def listed(record):
    data = signed_in(record).get(URL).data
    return data, {c['name']: c['reason'] for c in data['other_hidden']}


@pytest.mark.parametrize('field, value, reason, label', [
    ('condition_verification_status', 'entered-in-error', 'entered_in_error', 'Entered in error'),
    ('condition_verification_status', 'refuted', 'refuted', 'Refuted'),
    ('condition_verification_status', 'provisional', 'not_confirmed', 'Not confirmed'),
    ('condition_verification_status', 'differential', 'not_confirmed', 'Not confirmed'),
    ('condition_verification_status', 'unconfirmed', 'not_confirmed', 'Not confirmed'),
    ('condition_category', 'health-concern', 'care_plan_item', 'Care-plan item'),
    ('condition_category', 'care-plan', 'care_plan_item', 'Care-plan item'),
    ('condition_clinical_status', 'resolved', 'resolved', 'Resolved'),
    ('condition_clinical_status', 'inactive', 'resolved', 'Resolved'),
    ('condition_clinical_status', 'remission', 'resolved', 'Resolved'),
])
def test_what_the_source_says_moves_a_condition_to_the_hidden_list_with_its_reason(field, value, reason, label):
    record = PatientRecordFactory(disease='')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01', **{field: value})

    data, hidden = listed(record)

    assert hidden == {'Type 2 diabetes': reason}
    assert data['other_hidden'][0]['reason_label'] == label
    assert data['other'] == []
    # Same card shape as the active list: id, date, source.
    assert {'id', 'date', 'source'} <= set(data['other_hidden'][0])


def test_a_condition_the_source_said_nothing_about_stays_active():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')
    condition(record, icd10('I10', 'Hypertension'), '2020-02-01',
              condition_category='problem-list-item', condition_verification_status='confirmed',
              condition_clinical_status='active')
    condition(record, icd10('M10.9', 'Gout'), '2020-03-01', condition_category='encounter-diagnosis,health-concern')

    data, hidden = listed(record)

    assert sorted(c['name'] for c in data['other']) == ['Gout', 'Hypertension', 'Type 2 diabetes']
    assert hidden == {}


def test_an_end_date_that_has_passed_resolves_one_still_to_come_does_not():
    record = PatientRecordFactory(disease='')
    today = timezone.localdate()
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01', condition_end_date=today - timedelta(days=1))
    condition(record, icd10('I10', 'Hypertension'), '2020-02-01', condition_end_date=today + timedelta(days=30))

    data, hidden = listed(record)

    assert hidden == {'Type 2 diabetes': 'resolved'}
    assert [c['name'] for c in data['other']] == ['Hypertension']


def test_the_most_definite_reason_wins():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01',
              condition_verification_status='refuted', condition_category='health-concern',
              condition_clinical_status='resolved')
    condition(record, icd10('I10', 'Hypertension'), '2020-02-01',
              condition_category='health-concern', condition_clinical_status='resolved')

    _, hidden = listed(record)

    assert hidden == {'Type 2 diabetes': 'refuted', 'Hypertension': 'care_plan_item'}


def test_a_condition_with_a_live_record_is_not_also_hidden():
    record = PatientRecordFactory(disease='')
    diabetes = icd10('E11.9', 'Type 2 diabetes')
    condition(record, diabetes, '2015-01-01', condition_clinical_status='resolved')
    condition(record, diabetes, '2021-01-01', condition_clinical_status='active')

    data, hidden = listed(record)

    assert [(c['name'], c['date']) for c in data['other']] == [('Type 2 diabetes', '2021-01-01')]
    assert hidden == {}


def test_repeats_in_the_hidden_list_collapse_to_the_earliest():
    record = PatientRecordFactory(disease='')
    concept = icd10('E11.9', 'Type 2 diabetes')
    condition(record, concept, '2015-01-01', condition_category='health-concern')
    condition(record, concept, '2019-01-01', condition_category='health-concern')

    data, _ = listed(record)

    assert [(c['name'], c['date']) for c in data['other_hidden']] == [('Type 2 diabetes', '2015-01-01')]


def test_cancer_and_transplant_status_are_never_hidden_conditions():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('C90.00', 'Multiple myeloma'), '2021-02-01', condition_category='health-concern')
    condition(record, icd10('Z94.81', 'Bone marrow transplant status'), '2016-09-26',
              condition_clinical_status='resolved')

    data, hidden = listed(record)

    assert hidden == {}
    assert [c['name'] for c in data['cancer']] == ['Multiple myeloma']
    assert [c['name'] for c in data['historical_procedures']] == ['Bone marrow transplant status']


def test_a_patient_with_nothing_to_hide_gets_an_empty_list():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')

    assert signed_in(record).get(URL).data['other_hidden'] == []
