"""The PHR APIs and the patient list show proposals when provisional data is on (#2660).

A patient reads their own record through ``/api/v1/phr/``. With the site switch
on, labs, diagnoses and procedures from proposed mappings are listed with their
``confidence``, and record fields a proposal fills carry one too. Switch off,
nothing provisional is read or shown. A share recipient, an export, medications,
therapy and What's new never include a proposal.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.services import provisional_record
from omop_core.models import (
    DrugExposure, GroupAccess, PatientRecordConfidence, PatientRecordProvisional, ProcedureOccurrence,
)
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, LabMarker
from tests.factories import (
    ConceptFactory, DrugExposureFactory, OrganizationFactory, PatientRecordFactory,
)
from tests.test_phr_read_model import condition, icd10, lab, loinc, signed_in
from tests.test_phr_sharing import EVERYTHING, grant, holder

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''
    settings.PHR_SHARE_URL = 'https://one.example/r'
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'


@pytest.fixture
def patient():
    record = PatientRecordFactory(disease='', gender='M')
    return record, signed_in(record)


def pair(record, **proposals):
    """A stored provisional pair holding *proposals* (field -> (value, confidence))."""
    PatientRecordProvisional.objects.create(
        person=record.person, **{name: value for name, (value, _) in proposals.items()})
    PatientRecordConfidence.objects.create(
        person=record.person, **{name: mark for name, (_, mark) in proposals.items()})


# --- labs ---------------------------------------------------------------------------------

def test_a_proposed_lab_is_listed_with_its_confidence_and_a_real_result_wins_the_day(patient):
    record, client = patient
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-03-01', 11.0)
    lab(record, hgb, '2024-04-01', 9.5, confidence=82)
    lab(record, hgb, '2024-04-01', 10.0)                 # a real result on the same day
    wbc = loinc('6690-2', 'Leukocytes [#/volume] in Blood')
    lab(record, wbc, '2024-04-02', 5.0, confidence=75)   # only a proposal

    tests = {t['id']: t for t in client.get('/api/v1/phr/labs/').json()['tests']}

    hemoglobin = tests[str(hgb.concept_id)]
    assert hemoglobin['latest']['value'] == 10 and 'confidence' not in hemoglobin['latest']
    assert [r.get('confidence') for r in hemoglobin['recent']] == [None, 82, None]
    assert hemoglobin['count'] == 3
    assert tests[str(wbc.concept_id)]['latest']['confidence'] == 75


def test_a_lab_history_marks_the_proposed_results(patient):
    record, client = patient
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-03-01', 11.0)
    lab(record, hgb, '2024-04-01', 9.5, confidence=82)

    history = client.get(f'/api/v1/phr/labs/{hgb.concept_id}/').json()['history']

    assert [r.get('confidence') for r in history] == [82, None]


def test_with_the_switch_off_no_proposal_is_read_or_shown(patient, settings):
    record, client = patient
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-03-01', 11.0)
    lab(record, hgb, '2024-04-01', 9.5, confidence=82)
    only = loinc('6690-2', 'Leukocytes [#/volume] in Blood')
    lab(record, only, '2024-04-02', 5.0, confidence=75)
    settings.PROVISIONAL_DATA_ENABLED = False

    body = client.get('/api/v1/phr/labs/').json()

    assert [t['id'] for t in body['tests']] == [str(hgb.concept_id)]
    assert body['tests'][0]['count'] == 1 and 'confidence' not in body['tests'][0]['latest']
    assert client.get(f'/api/v1/phr/labs/{only.concept_id}/').status_code == 404
    assert client.get('/api/v1/phr/status/').json()['sections']['labs'] == 'ready'


def test_a_section_with_only_proposals_is_ready_only_while_the_switch_is_on(patient, settings):
    record, client = patient
    lab(record, loinc('6690-2', 'Leukocytes [#/volume] in Blood'), '2024-04-02', 5.0, confidence=75)

    assert client.get('/api/v1/phr/status/').json()['sections']['labs'] == 'ready'
    settings.PROVISIONAL_DATA_ENABLED = False
    assert client.get('/api/v1/phr/status/').json()['sections']['labs'] == 'empty'


def test_a_newer_proposed_lab_never_becomes_the_latest_over_a_real_one(patient):
    record, client = patient
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-03-01', 11.0)
    lab(record, hgb, '2024-04-01', 5.0, confidence=82)

    test = client.get('/api/v1/phr/labs/').json()['tests'][0]

    assert test['latest']['value'] == 11 and 'confidence' not in test['latest']
    assert test['latest']['date'] == '2024-03-01'
    assert [r.get('confidence') for r in test['recent']] == [82, None]     # listed alongside
    assert test['count'] == 2


# --- diagnoses and procedures --------------------------------------------------------------

def test_a_proposed_condition_is_listed_with_its_confidence_unless_a_real_one_answers_it(patient):
    record, client = patient
    diabetes, gout = icd10('E11.9', 'Type 2 diabetes'), icd10('M10.9', 'Gout')
    condition(record, diabetes, '2020-01-01')
    condition(record, diabetes, '2021-01-01', confidence=70)   # already answered by a real row
    condition(record, gout, '2022-01-01', confidence=88)

    body = client.get('/api/v1/phr/diagnoses/').json()

    shown = {c['name']: c for c in body['other']}
    assert set(shown) == {'Type 2 diabetes', 'Gout'}
    assert 'confidence' not in shown['Type 2 diabetes'] and shown['Gout']['confidence'] == 88


def test_a_proposed_condition_never_renames_a_real_cancer_entry(patient):
    record, client = patient
    record.disease, record.disease_slug = 'Multiple myeloma', 'myeloma'
    record.save()
    condition(record, icd10('C90.00', 'Multiple myeloma not having achieved remission'), '2020-01-01')
    condition(record, icd10('C90.01', 'Multiple myeloma in remission'), '2024-01-01', confidence=60)

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer']

    assert len(cancer) == 1 and cancer[0]['id'] == 'primary'
    assert 'transitions' not in cancer[0] and 'confidence' not in cancer[0]
    assert cancer[0]['proposed'] == [
        {'name': 'Multiple myeloma in remission', 'date': '2024-01-01', 'confidence': 60}]


def test_a_cancer_only_a_proposal_answers_is_headlined_by_it_with_its_confidence(patient):
    record, client = patient
    condition(record, icd10('C90.01', 'Multiple myeloma in remission'), '2024-01-01', confidence=60)

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer']

    assert cancer[0]['name'] == 'Multiple myeloma in remission' and cancer[0]['confidence'] == 60
    assert not cancer[0].get('proposed')


def test_proposed_conditions_are_absent_with_the_switch_off(patient, settings):
    record, client = patient
    condition(record, icd10('M10.9', 'Gout'), '2022-01-01', confidence=88)
    settings.PROVISIONAL_DATA_ENABLED = False

    body = client.get('/api/v1/phr/diagnoses/').json()

    assert body['other'] == [] and body['cancer'] == []


def test_the_primary_cancer_carries_the_confidence_of_the_fields_a_proposal_fills(patient):
    record, client = patient
    record.disease = 'Breast cancer'
    record.disease_slug = 'breast-cancer'
    record.save()
    pair(record, her2_status=('Positive', 91), stage=('II', 74))

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer'][0]

    assert cancer['id'] == 'primary' and cancer['confidence'] == 74       # the lowest shown
    assert cancer['stage']['confidence'] == 74 and cancer['stage']['value'] == 'II'
    her2 = next(b for b in cancer['biomarkers'] if b['label'] == 'HER2')
    assert her2['confidence'] == 91
    from omop_core.models import PatientRecord
    assert PatientRecord.objects.get(person=record.person).stage in (None, '')   # nothing was written


def test_the_share_options_never_offer_a_proposed_cancer(patient):
    record, client = patient
    pair(record, disease=('Breast cancer', 80))

    assert client.get('/api/v1/phr/diagnoses/').json()['cancer'][0]['name'] == 'Breast cancer'
    sections = {s['id'] for s in client.get('/api/v1/phr/shares/options/').json()['sections']}
    assert 'diagnoses' not in sections


def test_the_overlay_never_touches_an_identity_or_redacted_field(patient, monkeypatch):
    from patient_portal.api.serializers import PatientRecordSerializer as S
    assert set(S.WITHHELD_IDENTIFIER_FIELDS) <= provisional_record.NEVER_OVERLAID
    assert {'email', 'phone_number', 'suppress_demographics_for_others', 'death_date', 'patient_age',
            'consent_capability', 'date_of_birth'} <= provisional_record.NEVER_OVERLAID

    record, _ = patient
    pair(record, stage=('II', 74))
    shown, marks = provisional_record.overlay(record)
    assert shown.stage == 'II' and marks == {'stage': 74}
    monkeypatch.setattr(provisional_record, 'NEVER_OVERLAID', frozenset({'stage'}))
    shown, marks = provisional_record.overlay(record)
    assert not shown.stage and marks == {}


def test_a_field_the_record_answers_is_never_overlaid(patient):
    record, client = patient
    record.disease, record.disease_slug, record.stage = 'Breast cancer', 'breast-cancer', 'III'
    record.save()
    pair(record, stage=('I', 60))

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer'][0]

    assert cancer['stage'] == {'label': 'Stage', 'value': 'III'} and 'confidence' not in cancer


def test_the_primary_cancer_has_no_overlay_with_the_switch_off(patient, settings):
    record, client = patient
    record.disease, record.disease_slug = 'Breast cancer', 'breast-cancer'
    record.save()
    pair(record, her2_status=('Positive', 91))
    settings.PROVISIONAL_DATA_ENABLED = False

    cancer = client.get('/api/v1/phr/diagnoses/').json()['cancer'][0]

    assert 'biomarkers' not in cancer and 'confidence' not in cancer


def _procedure(record, pk, name, **extra):
    return ProcedureOccurrence.objects.create(
        procedure_occurrence_id=pk, person=record.person, procedure_date='2024-05-01',
        procedure_concept=ConceptFactory(concept_name=name, concept_code=f'P{pk}'),
        procedure_type_concept=ConceptFactory(concept_name='EHR', concept_code='EHR'), **extra)


def test_a_repeat_procedure_on_another_date_is_not_shadowed_by_the_real_one(patient):
    record, client = patient
    _procedure(record, 9101, 'Bone marrow biopsy')                                      # 2024-05-01
    again = _procedure(record, 9102, 'Bone marrow biopsy', confidence=79)
    ProcedureOccurrence.objects.filter(pk=again.pk).update(procedure_concept=ProcedureOccurrence.objects.get(
        pk=9101).procedure_concept, procedure_date='2024-09-01')
    same_day = _procedure(record, 9103, 'Bone marrow biopsy', confidence=71)
    ProcedureOccurrence.objects.filter(pk=same_day.pk).update(procedure_concept=ProcedureOccurrence.objects.get(
        pk=9101).procedure_concept)

    items = client.get('/api/v1/phr/procedures/').json()['procedures']

    assert sorted((p['date'], p.get('confidence')) for p in items) == [
        ('2024-05-01', None), ('2024-09-01', 79)]          # the same-day proposal is the real one's


def test_a_proposed_procedure_is_listed_with_its_confidence_only_while_on(patient, settings):
    record, client = patient
    _procedure(record, 9101, 'Bone marrow biopsy')
    _procedure(record, 9102, 'Lumbar puncture', confidence=79)

    shown = {p['name']: p for p in client.get('/api/v1/phr/procedures/').json()['procedures']}
    assert 'confidence' not in shown['Bone marrow biopsy'] and shown['Lumbar puncture']['confidence'] == 79

    settings.PROVISIONAL_DATA_ENABLED = False
    assert [p['name'] for p in client.get('/api/v1/phr/procedures/').json()['procedures']] == [
        'Bone marrow biopsy']


# --- sections that stay approved-only -------------------------------------------------------

def test_medications_and_whats_new_never_include_a_proposal(patient):
    record, client = patient
    with suppress_patient_record_refresh():
        DrugExposureFactory(
            person=record.person, drug_concept=ConceptFactory(concept_name='Melphalan', concept_code='M1'),
            drug_exposure_start_date='2999-01-01', confidence=80)
    lab(record, loinc('6690-2', 'Leukocytes [#/volume] in Blood'), '2999-01-01', 5.0, confidence=75)

    assert client.get('/api/v1/phr/medications/').json()['medications'] == []
    assert client.get('/api/v1/phr/status/').json()['sections']['medications'] == 'empty'
    assert DrugExposure.objects.filter(confidence__isnull=False).count() == 1


def test_a_share_recipient_never_sees_a_proposal(patient):
    record, client = patient
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-03-01', 11.0)
    lab(record, hgb, '2024-04-01', 9.5, confidence=82)
    lab(record, loinc('6690-2', 'Leukocytes [#/volume] in Blood'), '2024-04-02', 5.0, confidence=75)
    condition(record, icd10('M10.9', 'Gout'), '2022-01-01', confidence=88)
    _procedure(record, 9102, 'Lumbar puncture', confidence=79)
    LabMarker.objects.create(loinc_code='718-7', rank=11, panels=['CBC'])
    share = holder(grant(client, selection=EVERYTHING).json())

    tests = share.get('/api/v1/phr/shared/labs/').json()['tests']
    assert [t['id'] for t in tests] == [str(hgb.concept_id)] and tests[0]['count'] == 1
    assert share.get('/api/v1/phr/shared/diagnoses/').json()['other'] == []
    assert share.get('/api/v1/phr/shared/procedures/').json()['procedures'] == []
    assert share.get('/api/v1/phr/shared/status/').json()['sections']['procedures'] == 'empty'
    # What can be shared lists no proposal either.
    catalog = {s['id']: s for s in client.get('/api/v1/phr/shares/options/').json()['sections']}
    assert 'diagnoses' not in catalog and 'procedures' not in catalog
    assert [i['label'] for i in catalog['labs']['items']] == ['Blood counts (CBC)']


def test_a_clinician_reads_nothing_through_the_patients_own_endpoints(patient):
    record, _ = patient
    lab(record, loinc('6690-2', 'Leukocytes [#/volume] in Blood'), '2024-04-02', 5.0, confidence=75)
    doctor = Identity.objects.create_user(email='phr-doctor@example.test')
    GroupAccess.objects.create(identity=doctor, org=record.organization, role='doctor')
    other = APIClient()
    other.force_authenticate(doctor)

    assert other.get('/api/v1/phr/labs/').status_code == 404


# --- the patient list ----------------------------------------------------------------------

LIST = '/api/v1/patient-records/'


def _staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='list-staff@example.test', is_staff=True))
    return client


def test_the_list_shows_a_proposal_in_an_empty_column_and_says_which(patient):
    record, _ = patient
    record.disease = 'Breast cancer'
    record.save()
    pair(record, stage=('II', 74), ecog_performance_status=(1, 66))

    row = next(r for r in _staff().get(LIST).json() if r['person_id'] == record.person_id)

    assert row['stage'] == 'II' and row['ecog_performance_status'] == 1
    assert row['provisional_fields'] == {'stage': 74, 'ecog_performance_status': 66}
    assert 'Stage' not in row['data_gaps'] and 'ECOG' not in row['data_gaps']


def test_list_filters_and_sort_are_approved_only_while_the_shown_value_is_overlaid(patient):
    """Documented (docs/provisional_data_plan.md): a filter reads the approved column."""
    record, _ = patient
    pair(record, stage=('II', 74))
    staff = _staff()

    shown = next(r for r in staff.get(LIST).json() if r['person_id'] == record.person_id)
    assert shown['stage'] == 'II'
    assert [r for r in staff.get(LIST, {'stage': 'II'}).json() if r['person_id'] == record.person_id] == []


def test_the_list_is_the_record_with_the_switch_off(patient, settings):
    record, _ = patient
    pair(record, stage=('II', 74))
    settings.PROVISIONAL_DATA_ENABLED = False

    row = next(r for r in _staff().get(LIST).json() if r['person_id'] == record.person_id)

    assert not row['stage'] and 'provisional_fields' not in row and 'Stage' in row['data_gaps']


def test_the_list_costs_the_same_queries_for_one_patient_or_many():
    PatientRecordFactory(disease='Myeloma')
    staff = _staff()
    with CaptureQueriesContext(connection) as one:
        staff.get(LIST)
    for _ in range(5):
        extra = PatientRecordFactory(disease='Myeloma')
        pair(extra, stage=('II', 70))
    with CaptureQueriesContext(connection) as many:
        assert len(staff.get(LIST).json()) == 6

    assert len(many) <= len(one) + 2


def test_a_reader_who_cannot_list_the_patient_gets_no_proposal_for_them(patient):
    record, _ = patient
    pair(record, stage=('II', 74))
    elsewhere = Identity.objects.create_user(email='list-elsewhere@example.test')
    GroupAccess.objects.create(identity=elsewhere, org=OrganizationFactory(), role='doctor')
    other = APIClient()
    other.force_authenticate(elsewhere)
    analyst = Identity.objects.create_user(email='list-analyst@example.test')
    GroupAccess.objects.create(identity=analyst, org=record.organization, role='analyst')
    mapper = APIClient()
    mapper.force_authenticate(analyst)

    assert other.get(LIST).json() == []
    assert mapper.get(LIST).json() == []                 # an analyst reads no patient
