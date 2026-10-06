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


def test_a_second_cancer_gets_its_own_entry_and_cancers_are_newest_first():
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma', diagnosis_date='2024-03-12')
    condition(record, icd10('D47.2', 'Monoclonal gammopathy'), '2019-06-04')
    condition(record, icd10('C90.00', 'Multiple myeloma'), '2024-03-12')
    condition(record, icd10('C61', 'Malignant neoplasm of prostate'), '2025-11-08', condition_end_date='2026-03-06')

    cancer = signed_in(record).get('/api/v1/phr/diagnoses/').data['cancer']

    assert [(c['name'], c['date'], c.get('status')) for c in cancer] == [
        ('Malignant neoplasm of prostate', '2025-11-08', 'Resolved'),
        ('Multiple myeloma', '2024-03-12', None),
    ]
    assert [t['name'] for t in cancer[1]['transitions']] == ['Monoclonal gammopathy', 'Multiple myeloma']
    assert 'transitions' not in cancer[0]


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


# ---------------------------------------------------------------- labs

from patient_portal.models import LabMarker  # noqa: E402


def loinc(code, name):
    return ConceptFactory(concept_code=code, concept_name=name)


def lab(record, concept, on, value=None, **kwargs):
    with suppress_patient_record_refresh():
        return MeasurementFactory(
            person=record.person, measurement_concept=concept, measurement_date=on,
            value_as_number=value, **kwargs,
        )


def test_labs_lists_the_latest_result_per_test_ranked_by_the_admin_labels():
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma')
    LabMarker.objects.create(loinc_code='48378-4', rank=1, disease_slugs=['myeloma'])
    LabMarker.objects.create(loinc_code='718-7', rank=11, panels=['CBC'])
    LabMarker.objects.create(loinc_code='2160-0', rank=30, panels=['BMP', 'CMP'])
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    ratio = loinc('48378-4', 'Kappa lc.free/Lambda lc.free [Mass Ratio] in Serum')
    creat = loinc('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma')
    zinc = loinc('5763-8', 'Zinc [Mass/volume] in Serum or Plasma')
    weight = loinc('29463-7', 'Body weight')
    visit = visit_at(record, 'MD Anderson Cancer Center')
    lab(record, hgb, '2024-01-01', 13.2, range_low=13.5, range_high=17.5)
    lab(record, hgb, '2024-03-01', 11.0, range_low=13.5, range_high=17.5, unit_source_value='g/dL',
        visit_occurrence=visit)
    lab(record, hgb, '2024-04-01', 1.0, is_erroneous=True)
    lab(record, ratio, '2024-02-01', 2.5, range_low=0.26, range_high=1.65)
    lab(record, creat, '2024-02-01', 0.9, range_low=0.7, range_high=1.3)
    lab(record, zinc, '2023-12-01', value_as_string='pending')
    lab(record, weight, '2024-02-01', 80)

    data = signed_in(record).get('/api/v1/phr/labs/').data

    assert [t['name'] for t in data['tests']] == [
        'Kappa lc.free/Lambda lc.free', 'Hemoglobin', 'Creatinine', 'Zinc',
    ]
    hemoglobin = data['tests'][1]
    assert hemoglobin == {
        'id': str(hgb.concept_id),
        'name': 'Hemoglobin',
        'specimen': 'Blood',
        'rank': 11,
        'panels': ['CBC'],
        'disease_slugs': [],
        'count': 2,
        'recent': [
            {'date': '2024-03-01', 'value': 11, 'flag': 'low'},
            {'date': '2024-01-01', 'value': 13.2, 'flag': 'low'},
        ],
        'latest': {
            'id': hemoglobin['latest']['id'],
            'date': '2024-03-01',
            'value': 11,
            'unit': 'g/dL',
            'range': {'low': 13.5, 'high': 17.5},
            'flag': 'low',
            'source': {'kind': 'record', 'facility': 'MD Anderson Cancer Center', 'date': '2024-03-01'},
        },
    }
    assert data['tests'][0]['latest']['flag'] == 'high'
    assert data['tests'][2]['latest']['flag'] == 'normal'
    assert data['tests'][3]['latest'] == {
        'id': data['tests'][3]['latest']['id'],
        'date': '2023-12-01',
        'value_text': 'pending',
        'source': {'kind': 'record', 'facility': None, 'date': '2023-12-01'},
    }
    assert data['filters'] == {
        'diagnoses': [{'slug': 'myeloma', 'name': 'Multiple myeloma'}],
        'panels': ['CBC', 'BMP', 'CMP'],
    }


def test_lab_history_is_newest_first_and_follows_unmapped_source_concepts():
    record = PatientRecordFactory(disease='')
    ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')
    ldh = loinc('2532-0', 'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma')
    lab(record, ldh, '2024-01-01', 200)
    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=record.person, measurement_concept_id=0, measurement_source_concept=ldh,
            measurement_date='2024-05-01', value_as_number=260, range_high=225,
        )

    client = signed_in(record)
    data = client.get(f'/api/v1/phr/labs/{ldh.concept_id}/').data

    assert data['name'] == 'Lactate dehydrogenase'
    assert [(h['date'], h['value'], h.get('flag')) for h in data['history']] == [
        ('2024-05-01', 260, 'high'),
        ('2024-01-01', 200, None),
    ]
    assert client.get('/api/v1/phr/labs/123456789/').status_code == 404


def test_another_patients_lab_history_is_not_found():
    mine, theirs = PatientRecordFactory(disease=''), PatientRecordFactory(disease='')
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(theirs, hgb, '2024-01-01', 12)

    assert signed_in(mine).get(f'/api/v1/phr/labs/{hgb.concept_id}/').status_code == 404


# ---------------------------------------------------------------- medications, procedures, genetics

from datetime import timedelta  # noqa: E402
from django.utils import timezone  # noqa: E402

from omop_core.models import PatientDocument, ProcedureOccurrence, Provider  # noqa: E402
from tests.factories import DrugExposureFactory  # noqa: E402


def drug(record, concept, start, end=None, **kwargs):
    with suppress_patient_record_refresh():
        return DrugExposureFactory(
            person=record.person, drug_concept=concept, drug_exposure_start_date=start,
            drug_exposure_end_date=end, **kwargs,
        )


def test_medications_group_exposures_per_drug_current_first_with_dose_history():
    record = PatientRecordFactory(disease='')
    future = (timezone.localdate() + timedelta(days=30)).isoformat()
    lenalidomide = ConceptFactory(concept_name='lenalidomide 10 MG Oral Capsule', concept_code='RX-LEN')
    dex = ConceptFactory(concept_name='dexamethasone 4 MG Oral Tablet', concept_code='RX-DEX')
    visit = visit_at(record, 'MD Anderson pharmacy')
    drug(record, lenalidomide, '2023-01-01', '2023-06-30', sig='15 mg daily, days 1–21')
    drug(record, lenalidomide, '2024-01-01', future, sig='10 mg daily, days 1–21', visit_occurrence=visit)
    drug(record, dex, '2022-01-01', '2022-12-31', quantity=40, dose_unit_source_value='mg')
    drug(record, dex, '2021-01-01', '2021-02-01', is_erroneous=True)
    ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')
    with suppress_patient_record_refresh():
        DrugExposureFactory(person=record.person, drug_concept_id=0, drug_source_value='Vitamin D3 / 2000 IU',
                            drug_exposure_start_date='2020-05-01', drug_exposure_end_date=None)

    client = signed_in(record)
    meds = client.get('/api/v1/phr/medications/').data['medications']

    assert [(m['name'], m['status']) for m in meds] == [
        ('lenalidomide 10 MG Oral Capsule', 'current'),
        ('Vitamin D3 / 2000 IU', 'current'),
        ('dexamethasone 4 MG Oral Tablet', 'past'),
    ]
    assert meds[0]['dose'] == '10 mg daily, days 1–21'
    assert meds[0]['started'] == '2023-01-01' and 'ended' not in meds[0]
    assert meds[2]['dose'] == '40 mg' and meds[2]['ended'] == '2022-12-31'
    assert meds[1]['id'] == 'src-vitamin-d3-2000-iu'

    detail = client.get(f"/api/v1/phr/medications/{meds[0]['id']}/").data
    assert [(h['date'], h['dose'], h['source']['facility']) for h in detail['history']] == [
        ('2024-01-01', '10 mg daily, days 1–21', 'MD Anderson pharmacy'),
        ('2023-01-01', '15 mg daily, days 1–21', None),
    ]
    assert client.get(f"/api/v1/phr/medications/{meds[1]['id']}/").status_code == 200
    assert client.get('/api/v1/phr/medications/424242/').status_code == 404


def test_procedures_are_newest_first_with_who_performed_them():
    record = PatientRecordFactory(disease='')
    Provider.objects.create(provider_id=55, provider_name='Dr. Rivera')
    biopsy = ConceptFactory(concept_name='Bone marrow biopsy', concept_code='PROC-BMB')
    asct = ConceptFactory(concept_name='Autologous stem cell transplant', concept_code='PROC-ASCT')
    kind = ConceptFactory(concept_name='Procedure type', concept_code='TYPE-PROC')
    for pk, concept, day, extra in (
        (1, biopsy, '2021-01-15', {'provider_id': 55}),
        (2, asct, '2022-03-01', {'procedure_end_date': '2022-03-05'}),
        (3, biopsy, '2020-01-01', {'is_erroneous': True}),
    ):
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=pk, person=record.person, procedure_concept=concept,
            procedure_date=day, procedure_type_concept=kind, **extra,
        )

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert items == [
        {'id': 2, 'name': 'Autologous stem cell transplant', 'date': '2022-03-01', 'end_date': '2022-03-05',
         'source': {'kind': 'record', 'facility': None, 'date': '2022-03-01'}},
        {'id': 1, 'name': 'Bone marrow biopsy', 'date': '2021-01-15', 'performed_by': 'Dr. Rivera',
         'source': {'kind': 'record', 'facility': None, 'date': '2021-01-15'}},
    ]


def test_genetics_groups_findings_into_tests_and_attaches_matching_documents(monkeypatch):
    record = PatientRecordFactory(disease='')
    m1 = lab(record, loinc('81252-9', 'Gene variant'), '2023-04-02')
    m2 = lab(record, loinc('81252-9', 'Gene variant'), '2023-04-02')
    m3 = lab(record, loinc('81252-9', 'Gene variant'), '2021-06-01')
    variants = [
        {'id': m1.pk, 'gene': 'tp53', 'variant': 'del(17p)', 'test_date': '2023-04-02',
         'assay_method': 'FISH', 'allelic_frequency': 38, 'interpretation': 'pathogenic'},
        {'id': m2.pk, 'gene': 'ccnd1', 'genomic_feature': 't(11;14)', 'test_date': '2023-04-02',
         'assay_method': 'FISH'},
        {'id': m3.pk, 'gene': 'kras', 'variant': 'G12D', 'test_date': '2021-06-01', 'assay_method': 'NGS',
         'origin': 'somatic'},
    ]
    monkeypatch.setattr('omop_core.services.genomics.list_variants', lambda person: variants)
    PatientDocument.objects.create(person=record.person, doc_type='FISH', title='FISH panel report',
                                   file_url='https://example.test/fish.pdf', effective_date='2023-04-02')
    PatientDocument.objects.create(person=record.person, doc_type='GEP', title='GEP70', effective_date='2022-02-02')
    PatientDocument.objects.create(person=record.person, doc_type='IMAGING', effective_date='2022-02-02')

    tests = signed_in(record).get('/api/v1/phr/genetics/').data['tests']

    assert [(t['type'], t['date'], [f['name'] for f in t['findings']]) for t in tests] == [
        ('FISH', '2023-04-02', ['TP53', 't(11;14)']),
        ('Gene expression profiling', '2022-02-02', []),
        ('NGS', '2021-06-01', ['KRAS']),
    ]
    assert tests[0]['document'] == {'title': 'FISH panel report', 'url': 'https://example.test/fish.pdf'}
    assert tests[1]['document'] == {'title': 'GEP70'}
    assert tests[0]['findings'][0] == {
        'id': m1.pk, 'name': 'TP53', 'variant': 'del(17p)', 'interpretation': 'pathogenic', 'percent': 38,
        'source': {'kind': 'record', 'facility': None, 'date': '2023-04-02'},
    }


def test_unlabelled_findings_join_the_only_genetic_report_from_that_day(monkeypatch):
    record = PatientRecordFactory(disease='')
    m1 = lab(record, loinc('81252-9', 'Gene variant'), '2026-08-20')
    m2 = lab(record, loinc('81252-9', 'Gene variant'), '2026-06-04')
    monkeypatch.setattr('omop_core.services.genomics.list_variants', lambda person: [
        {'id': m1.pk, 'gene': 'fgfr3', 'variant': 't(4;14)', 'test_date': '2026-08-20'},
        {'id': m2.pk, 'gene': 'kras', 'variant': 'Q61H', 'test_date': '2026-06-04'},
    ])
    PatientDocument.objects.create(person=record.person, doc_type='FISH', title='FISH panel', effective_date='2026-08-20')
    PatientDocument.objects.create(person=record.person, doc_type='NGS', title='NGS panel', effective_date='2026-06-04')
    PatientDocument.objects.create(person=record.person, doc_type='CYTOGENETICS', title='Karyotype', effective_date='2026-06-04')

    tests = signed_in(record).get('/api/v1/phr/genetics/').data['tests']

    assert [(t['type'], t['date'], [f['name'] for f in t['findings']]) for t in tests] == [
        ('FISH', '2026-08-20', ['FGFR3']),
        ('Genetic test', '2026-06-04', ['KRAS']),
        # Two reports that day, so KRAS stays unattached; newer upload first.
        ('Cytogenetics', '2026-06-04', []),
        ('NGS', '2026-06-04', []),
    ]


def test_lab_names_split_into_analyte_and_specimen_in_linear_time():
    import time

    from patient_portal.api.phr.labs import split_name

    assert split_name('Hemoglobin [Mass/volume] in Blood') == ('Hemoglobin', 'Blood')
    assert split_name('Creatinine [Mass/volume] in Serum or Plasma by Enzymatic method') == (
        'Creatinine', 'Serum or Plasma')
    assert split_name('Kappa lc.free/Lambda lc.free [Mass Ratio] in Serum') == ('Kappa lc.free/Lambda lc.free', 'Serum')
    assert split_name('Hemoglobin A1c/Hemoglobin.total in Blood') == ('Hemoglobin A1c/Hemoglobin.total', 'Blood')
    assert split_name('Body weight') == ('Body weight', None)
    assert split_name('Bilirubin [Mass/volume]') == ('Bilirubin [Mass/volume]', None)
    assert split_name('') == ('', None)

    # Inputs that made the old regex backtrack polynomially (CodeQL py/polynomial-redos).
    started = time.perf_counter()
    for crafted in ('a ' + ' [' * 20_000, 'a in ' + 'a in a' * 20_000, 'a in a by ' + 'a by ' * 20_000):
        split_name(crafted)
    assert time.perf_counter() - started < 0.5
