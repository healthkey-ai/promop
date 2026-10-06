"""/api/v1/phr/ — the patient's own record, shaped per section for display."""
import pytest
from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APIClient

from omop_core.models import CareSite, ConditionOccurrence, ProvenanceRecord, VisitOccurrence
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory,
    ConditionOccurrenceFactory,
    MeasurementFactory,
    PatientRecordFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


def signed_in(record):
    identity = Identity.objects.create_user(email=f'p{record.person_id}@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)
    return client


def icd10(code, name):
    return ConceptFactory(
        concept_code=code,
        concept_name=name,
        vocabulary=VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD10CM'),
    )


def condition(record, concept, start, **kwargs):
    with suppress_patient_record_refresh():
        return ConditionOccurrenceFactory(
            person=record.person, condition_concept=concept, condition_start_date=start, **kwargs
        )


def visit_at(record, site_name):
    site = CareSite.objects.create(care_site_id=7001, care_site_name=site_name)
    concept = ConceptFactory(concept_name='Outpatient', concept_code='OP')
    return VisitOccurrence.objects.create(
        visit_occurrence_id=8001,
        person=record.person,
        visit_concept=concept,
        visit_type_concept=concept,
        visit_start_date='2023-01-01',
        visit_end_date='2023-01-01',
        care_site_id=site.care_site_id,
    )


# ---------------------------------------------------------------- access


def test_requires_authentication():
    assert APIClient().get('/api/v1/phr/about/').status_code in (401, 403)


def test_identity_without_a_patient_link_gets_404_and_nothing_is_created():
    identity = Identity.objects.create_user(email='clinician@example.test')
    client = APIClient()
    client.force_authenticate(identity)

    response = client.get('/api/v1/phr/diagnoses/')

    assert response.status_code == 404
    assert not PatientUser.objects.filter(identity=identity).exists()


def test_a_patient_sees_only_their_own_conditions():
    mine, theirs = PatientRecordFactory(disease=''), PatientRecordFactory(disease='')
    condition(theirs, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')

    response = signed_in(mine).get('/api/v1/phr/diagnoses/')

    assert response.status_code == 200
    assert response.data == {'cancer': [], 'other': []}


# ---------------------------------------------------------------- about


def test_about_omits_empty_fields_and_marks_patient_edits():
    record = PatientRecordFactory(
        gender='F',
        date_of_birth='1961-04-02',
        race='',
        ethnicity=None,
        city='Boston',
        country='USA',
        facility_name='Dana-Farber',
        user_edited_fields=['city'],
    )

    fields = {f['key']: f for f in signed_in(record).get('/api/v1/phr/about/').data['fields']}

    assert set(fields) == {'date_of_birth', 'gender', 'address'}
    assert fields['gender']['value'] == 'Female'
    assert fields['date_of_birth']['source'] == {'kind': 'record', 'facility': 'Dana-Farber', 'date': None}
    assert fields['address'] == {
        'key': 'address',
        'value': 'Boston, USA',
        'source': {'kind': 'patient', 'facility': None, 'date': None},
    }


# ---------------------------------------------------------------- diagnoses


def test_primary_cancer_uses_the_disease_specific_stage_and_hides_what_is_missing():
    record = PatientRecordFactory(
        disease='Chronic lymphocytic leukemia',
        disease_slug='chronic-lymphocytic-leukemia',
        binet_stage='B',
        stage='should not be shown for CLL',
        cytogenetic_markers='del(17p)',
        her2_status='',
        diagnosis_date='2021-03-04',
        condition_clinical_status='active',
        facility_name='MD Anderson',
    )

    cancer = signed_in(record).get('/api/v1/phr/diagnoses/').data['cancer']

    assert cancer == [{
        'id': 'primary',
        'name': 'Chronic lymphocytic leukemia',
        'stage': {'label': 'Binet stage', 'value': 'B'},
        'biomarkers': [{'label': 'Cytogenetic markers', 'value': 'del(17p)'}],
        'date': '2021-03-04',
        'status': 'Active',
        'source': {'kind': 'record', 'facility': 'MD Anderson', 'date': '2021-03-04'},
    }]


def test_spread_is_shown_for_solid_tumours_only():
    breast = PatientRecordFactory(disease='Breast cancer', disease_slug='breast-cancer', metastatic_status=True)
    myeloma = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma', metastatic_status=True)

    assert signed_in(breast).get('/api/v1/phr/diagnoses/').data['cancer'][0]['spread'] == 'Metastatic'
    assert 'spread' not in signed_in(myeloma).get('/api/v1/phr/diagnoses/').data['cancer'][0]


def test_cancer_conditions_become_dated_transitions_and_other_conditions_are_listed_newest_first():
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma')
    condition(record, icd10('D47.2', 'Monoclonal gammopathy'), '2018-05-01')
    condition(record, icd10('C90.00', 'Multiple myeloma'), '2021-02-01')
    condition(record, icd10('C90.00', 'Multiple myeloma'), '2022-02-01')
    diabetes = icd10('E11.9', 'Type 2 diabetes')
    condition(record, diabetes, '2015-01-01')
    condition(record, diabetes, '2019-01-01')
    condition(record, icd10('I10', 'Hypertension'), '2020-01-01', condition_end_date='2021-01-01')
    condition(record, icd10('J45', 'Asthma'), '2023-01-01', is_erroneous=True)

    data = signed_in(record).get('/api/v1/phr/diagnoses/').data

    assert data['cancer'][0]['transitions'] == [
        {'name': 'Monoclonal gammopathy', 'date': '2018-05-01'},
        {'name': 'Multiple myeloma', 'date': '2021-02-01'},
    ]
    assert [(c['name'], c['date'], c.get('status')) for c in data['other']] == [
        ('Hypertension', '2020-01-01', 'Resolved'),
        ('Type 2 diabetes', '2015-01-01', None),
    ]


def test_without_a_derived_primary_each_cancer_condition_is_listed():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('C50.911', 'Malignant neoplasm of breast'), '2020-02-02')

    cancer = signed_in(record).get('/api/v1/phr/diagnoses/').data['cancer']

    assert [(c['name'], c['date']) for c in cancer] == [('Malignant neoplasm of breast', '2020-02-02')]


def test_condition_sources_show_facility_for_records_and_none_for_patient_reports():
    record = PatientRecordFactory(disease='')
    visit = visit_at(record, 'Mayo Clinic')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2019-01-01', visit_occurrence=visit)
    self_report = ConceptFactory(concept_id=32865, concept_name='Patient self-report', concept_code='OMOP4976879')
    condition(record, icd10('M81.0', 'Osteoporosis'), '2020-01-01', condition_type_concept=self_report)
    gout = condition(record, icd10('M10.9', 'Gout'), '2021-01-01', visit_occurrence=visit)
    ProvenanceRecord.objects.create(
        content_type=ContentType.objects.get_for_model(ConditionOccurrence),
        object_id=gout.pk,
        source='PATIENT_SELF',
    )

    other = {c['name']: c['source'] for c in signed_in(record).get('/api/v1/phr/diagnoses/').data['other']}

    assert other == {
        'Gout': {'kind': 'patient', 'facility': None, 'date': '2021-01-01'},
        'Osteoporosis': {'kind': 'patient', 'facility': None, 'date': '2020-01-01'},
        'Type 2 diabetes': {'kind': 'record', 'facility': 'Mayo Clinic', 'date': '2019-01-01'},
    }


# ---------------------------------------------------------------- status


def test_status_reports_each_section_ready_or_empty():
    record = PatientRecordFactory(disease='Multiple myeloma', gender='M')
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, value_as_number=12)

    sections = signed_in(record).get('/api/v1/phr/status/').data['sections']

    assert sections == {
        'whats_new': 'ready',
        'about': 'ready',
        'diagnoses': 'ready',
        'therapy': 'empty',
        'outcomes': 'empty',
        'labs': 'ready',
        'medications': 'empty',
        'procedures': 'empty',
        'genetics': 'empty',
        'imaging': 'empty',
    }
