"""D04: per-cancer OMOP facts, authoring, projection and refresh isolation."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Measurement, Observation, PatientRecord
from omop_core.services.cancer_details import CONDITION_FIELD, cancer_details
from omop_core.services.omop_projection import CLEAR_VALUE, without_cleared_history
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PatientRecordFactory
from tests.test_phr_read_model import condition, icd10, signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def cancers(settings):
    settings.CELERY_BROKER_URL = ''
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma', stage='Legacy II')
    mine = condition(record, icd10('C90.0', 'Multiple myeloma'), '2020-01-01')
    other = condition(record, icd10('C50.9', 'Breast cancer'), '2022-01-01', condition_end_date='2023-01-01')
    event = ConceptFactory(vocabulary__vocabulary_id='CDM', domain__domain_id='Metadata',
                           concept_code='CDM183', concept_name=CONDITION_FIELD)
    stage = ConceptFactory(vocabulary__vocabulary_id='LOINC', domain__domain_id='Measurement', concept_code='21908-9')
    FieldConceptMapping.objects.update_or_create(field_name='stage', defaults={
        'concept': stage, 'vocabulary_id': 'LOINC', 'concept_code': '21908-9',
        'omop_table': 'measurement', 'value_kind': 'string', 'source_value': '21908-9', 'status': 'approved'})
    return record, mine, other, event, stage, signed_in(record)


def fact(cancers, dx, field, value, on='2024-01-01', model=Measurement, **changes):
    record, _, _, event, stage, client = cancers
    codes = {'stage': '21908-9', 'histologic_type': '59847-4', 'estrogen_receptor_status': '16112-5',
             'mrd_status': '105941-9', 'cytogenetic_markers': '107675007'}
    code = codes[field]
    concept = stage if field == 'stage' else ConceptFactory(
        vocabulary__vocabulary_id='SNOMED' if field == 'cytogenetic_markers' else 'LOINC', concept_code=code,
        concept_name='Multiple myeloma minimal residual disease analysis' if field == 'mrd_status' else field,
        domain__domain_id=model.__name__)
    table = model._meta.db_table
    factory = MeasurementFactory if model is Measurement else ObservationFactory
    with suppress_patient_record_refresh():
        return factory(person=record.person, value_as_number=None, value_as_string=value, **{
            f'{table}_concept': concept, f'{table}_date': on, f'{table}_source_value': code,
            f'{table}_event_id': dx.pk if dx else None,
            'meas_event_field_concept' if model is Measurement else 'obs_event_field_concept': event if dx else None,
            **changes})


def cards(client):
    response = client.get('/api/v1/phr/diagnoses/')
    assert response.status_code == 200
    return {row['name']: row for row in response.data['cancer']}


def test_each_cancer_gets_its_stage_subtype_and_markers_with_sources(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, mine, 'stage', 'R-ISS II')
    fact(cancers, other, 'stage', 'IIIA')
    fact(cancers, mine, 'histologic_type', 'IgG kappa')
    fact(cancers, other, 'histologic_type', 'Ductal carcinoma')
    fact(cancers, mine, 'mrd_status', 'Negative')
    fact(cancers, mine, 'cytogenetic_markers', 'del17p', model=Observation)
    fact(cancers, other, 'estrogen_receptor_status', 'Positive')
    data = cards(client)
    mm, bc = data['Multiple myeloma'], data['Breast cancer']
    assert mm['stage']['value'] == 'R-ISS II'
    assert bc['stage']['value'] == 'IIIA'
    assert mm['subtype'] == 'IgG kappa'
    assert bc['subtype'] == 'Ductal carcinoma'
    assert {b['label']: b['value'] for b in mm['biomarkers']} == {'MRD': 'Negative', 'Cytogenetic markers': 'del17p'}
    assert {b['label']: b['value'] for b in bc['biomarkers']} == {'Estrogen receptor (ER)': 'Positive'}
    assert bc['stage']['source']['date'] == '2024-01-01'
    assert bc['detail_sources']['subtype']['kind'] == 'record'


def test_refresh_keeps_active_primary_and_does_not_steal_secondary_details(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, mine, 'stage', 'II', on='2022-01-01')
    fact(cancers, mine, 'histologic_type', 'IgG kappa', on='2022-01-01')
    fact(cancers, mine, 'mrd_status', 'Negative', on='2022-01-01')
    fact(cancers, other, 'stage', 'IV', on='2025-01-01')
    fact(cancers, other, 'histologic_type', 'Ductal carcinoma', on='2025-01-01')
    fact(cancers, other, 'estrogen_receptor_status', 'Positive', on='2025-01-01')
    for _ in range(2):
        refresh_patient_record(record.person)
        record.refresh_from_db()
        assert record.stage == 'II'
        assert record.histologic_type == 'IgG kappa'
        assert record.mrd_status == 'Negative'
        assert not record.estrogen_receptor_status
        assert record.disease_slug in ('myeloma', 'multiple-myeloma')
        data = cards(client)
        assert data['Breast cancer']['stage']['value'] == 'IV'
        assert next(c for c in data.values() if c['id'] == 'primary')['stage']['value'] == 'II'


def test_legacy_unlinked_facts_remain_primary_only_and_never_create_secondary_values(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, None, 'stage', 'Legacy II')
    data = cards(client)
    assert data['Multiple myeloma']['stage']['value'] == 'Legacy II'
    assert 'stage' not in data['Breast cancer']


def test_clearing_one_cancer_does_not_clear_the_other(cancers):
    record, mine, other, _, _, client = cancers
    a = fact(cancers, mine, 'stage', 'II', on='2024-01-01')
    b = fact(cancers, other, 'stage', 'III', on='2024-01-01')
    clear = fact(cancers, other, 'stage', '', on='2025-01-01', value_source_value=CLEAR_VALUE)
    assert without_cleared_history([clear, a, b], 'measurement') == [a]
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.stage == 'II'
    assert next(c for c in cards(client).values() if c['id'] == 'primary')['stage']['value'] == 'II'
    assert 'stage' not in cards(client)['Breast cancer']
    fact(cancers, mine, 'stage', '', on='2026-01-01', value_source_value=CLEAR_VALUE)
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.stage is None
    assert 'stage' not in next(c for c in cards(client).values() if c['id'] == 'primary')


def measurement_body(cancers, dx, text='II'):
    record, _, _, _, stage, _ = cancers
    return {'person': record.person_id, 'measurement_concept': stage.pk, 'measurement_type_concept': 32817,
        'measurement_date': '2025-01-01', 'measurement_source_value': '21908-9',
        'value_as_string': text, 'condition_occurrence': dx.pk}


def test_v1_authors_two_same_day_results_for_different_cancers_and_retries_idempotently(cancers):
    record, mine, other, _, _, client = cancers
    body = [measurement_body(cancers, mine), measurement_body(cancers, other)]
    response = client.post('/api/v1/measurements/?skip_refresh=true', body, format='json')
    assert response.status_code == 201, response.data
    assert len(set(response.data['ids'])) == 2
    again = client.post('/api/v1/measurements/?skip_refresh=true', body, format='json')
    assert again.status_code in (200, 201), again.data
    assert again.data['ids'] == response.data['ids']
    assert Measurement.objects.filter(person=record.person).count() == 2
    single = client.post('/api/v1/measurements/?skip_refresh=true', measurement_body(cancers, other), format='json')
    assert single.status_code in (200, 201), single.data
    row = Measurement.objects.get(pk=response.data['ids'][1])
    assert row.measurement_event_id == other.pk
    read = client.get(f'/api/v1/measurements/{row.pk}/')
    assert read.data['condition_occurrence'] == other.pk


def test_v1_rejects_foreign_erroneous_or_missing_condition_and_person_moves(cancers):
    record, mine, other, _, _, client = cancers
    foreign = PatientRecordFactory()
    foreign_dx = condition(foreign, icd10('C61', 'Prostate cancer'), '2020-01-01')
    bad = measurement_body(cancers, foreign_dx)
    assert client.post('/api/v1/measurements/', bad, format='json').status_code == 400
    other.is_erroneous = True
    with suppress_patient_record_refresh():
        other.save(update_fields=['is_erroneous'])
    assert client.post('/api/v1/measurements/', measurement_body(cancers, other), format='json').status_code == 400
    assert client.post('/api/v1/measurements/', {**bad, 'condition_occurrence': 999999}, format='json').status_code == 400
    row = fact(cancers, mine, 'stage', 'II')
    assert client.patch(f'/api/v1/measurements/{row.pk}/', {'person': foreign.person_id}, format='json').status_code in (400, 403)
    row.refresh_from_db()
    assert row.person_id == record.person_id
    assert APIClient().get(f'/api/v1/measurements/{row.pk}/').status_code in (401, 403)


def test_other_patients_erroneous_facts_and_wrong_event_table_never_supply_details(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, other, 'stage', 'Valid')
    fact(cancers, other, 'stage', 'Error', on='2026-01-01', is_erroneous=True)
    wrong = ConceptFactory(vocabulary__vocabulary_id='CDM', domain__domain_id='Metadata', concept_name='procedure_occurrence.procedure_occurrence_id')
    fact(cancers, other, 'stage', 'Wrong table', on='2026-02-01', meas_event_field_concept=wrong)
    foreign = fact(cancers, other, 'stage', 'Foreign', on='2026-03-01')
    Measurement.objects.filter(pk=foreign.pk).update(person=PatientRecordFactory().person)
    assert cards(client)['Breast cancer']['stage']['value'] == 'Valid'


def test_older_condition_event_in_same_cancer_keeps_its_details(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, other, 'stage', 'III')
    condition(record, other.condition_concept, '2024-01-01', condition_end_date='2025-01-01')
    assert cards(client)['Breast cancer']['stage']['value'] == 'III'


def test_shared_diagnoses_filter_cancer_specific_details(cancers, settings):
    from tests.test_phr_sharing import grant, holder
    settings.PHR_SHARE_URL = 'https://one.example/r'
    record, mine, other, _, _, client = cancers
    fact(cancers, mine, 'stage', 'Private II')
    fact(cancers, other, 'stage', 'Shared III')
    share = grant(client, selection={'diagnoses': {'items': [f'condition-{other.pk}']}})
    assert share.status_code == 201
    data = holder(share.data).get('/api/v1/phr/shared/diagnoses/').data
    assert len(data['cancer']) == 1
    assert data['cancer'][0]['stage']['value'] == 'Shared III'
    assert data['cancer'][0]['name'] == 'Breast cancer'


def test_flat_primary_edit_does_not_overwrite_secondary_same_day_fact(cancers):
    from django.utils import timezone
    from omop_core.services.omop_projection import project_single_value
    record, mine, other, _, stage, client = cancers
    secondary = fact(cancers, other, 'stage', 'IV', on=timezone.localdate())
    projection = {'omop_table': 'measurement', 'concept_id': stage.pk, 'source_value': '21908-9', 'value_kind': 'string'}
    assert project_single_value(record.person, 'stage', 'II', projection)
    secondary.refresh_from_db()
    assert secondary.value_as_string == 'IV'
    primary = Measurement.objects.get(person=record.person, measurement_event_id=mine.pk)
    assert primary.value_as_string == 'II'
    assert primary.measurement_datetime
    assert cards(client)['Breast cancer']['stage']['value'] == 'IV'
    assert project_single_value(record.person, 'stage', None, projection)
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.stage is None
    assert cards(client)['Breast cancer']['stage']['value'] == 'IV'


def test_native_observation_link_preserves_patch_and_rejects_null_unlink(cancers):
    record, mine, other, event, _, client = cancers
    concept = ConceptFactory(vocabulary__vocabulary_id='SNOMED', concept_code='310871000000100', domain__domain_id='Observation')
    body = {'person': record.person_id, 'condition_occurrence': other.pk,
        'observation_concept': concept.pk, 'observation_type_concept': 32817,
        'observation_date': '2025-01-01', 'observation_source_value': '310871000000100', 'value_as_string': 'Positive'}
    response = client.post('/api/v1/observations/?skip_refresh=true', body, format='json')
    assert response.status_code == 201, response.data
    row = Observation.objects.get(person=record.person, observation_event_id=other.pk)
    assert row.obs_event_field_concept_id == event.pk
    response = client.patch(f'/api/v1/observations/{row.pk}/?skip_refresh=true', {'value_as_string': 'Negative'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['condition_occurrence'] == other.pk
    assert client.patch(f'/api/v1/observations/{row.pk}/', {'condition_occurrence': None}, format='json').status_code == 400
    assert {b['label']: b['value'] for b in cards(client)['Breast cancer']['biomarkers']} == {'Hormone receptor (HR)': 'Negative'}


def test_cytogenetic_markers_are_a_collection_with_individual_clears(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, mine, 'cytogenetic_markers', 'del17p', model=Observation, observation_source_value='cytogenetic:del17p')
    fact(cancers, mine, 'cytogenetic_markers', '1q_gain', model=Observation, observation_source_value='cytogenetic:1q_gain')
    fact(cancers, other, 'cytogenetic_markers', 'del13q', model=Observation, observation_source_value='cytogenetic:del13q')
    fact(cancers, mine, 'cytogenetic_markers', '', model=Observation, on='2025-01-01',
        observation_source_value='cytogenetic:del17p', value_source_value=CLEAR_VALUE)
    assert {b['label']: b['value'] for b in cards(client)['Multiple myeloma']['biomarkers']} == {'Cytogenetic markers': '1q_gain'}
    assert {b['label']: b['value'] for b in cards(client)['Breast cancer']['biomarkers']} == {'Cytogenetic markers': 'del13q'}


def test_patient_wide_labs_remain_visible_when_associated_with_secondary_cancer(cancers):
    record, mine, other, _, _, client = cancers
    hemoglobin = ConceptFactory(concept_code='718-7', domain__domain_id='Measurement')
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_concept=hemoglobin, measurement_source_value='718-7',
            measurement_event_id=other.pk, meas_event_field_concept=cancers[3], value_as_number=10.5, unit_source_value='g/dL')
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.hemoglobin_g_dl == 10.5


def test_snomed_diagnosis_uses_the_same_family_as_the_primary_slug(cancers):
    record, mine, other, _, _, client = cancers
    snomed = ConceptFactory(vocabulary__vocabulary_id='SNOMED', domain__domain_id='Condition',
        concept_code='109989006', concept_name='Multiple myeloma')
    type(mine).objects.filter(pk=mine.pk).update(condition_concept=snomed, condition_source_value='109989006')
    mine.refresh_from_db()
    fact(cancers, mine, 'stage', 'R-ISS II')
    data = cards(client)
    assert len(data) == 2
    assert data['Multiple myeloma']['id'] == 'primary'
    assert data['Multiple myeloma']['stage']['value'] == 'R-ISS II'


def test_batch_projection_query_count_does_not_grow_per_cancer(cancers, django_assert_num_queries):
    record, mine, other, _, _, client = cancers
    groups = {mine.pk: 'myeloma', other.pk: 'breast'}
    fact(cancers, mine, 'stage', 'II')
    for index in range(12):
        extra = condition(record, icd10(f'C{index+60}', f'Cancer {index}'), '2020-01-01')
        groups[extra.pk] = f'cancer-{index}'
        fact(cancers, extra, 'stage', str(index))
    with django_assert_num_queries(4):
        result = cancer_details(record.person_id, groups)
    assert len(result) == 13


def test_explicit_unmapped_fhir_stage_is_linked_to_its_cancer(cancers):
    record, mine, other, _, _, client = cancers
    row = fact(cancers, other, 'stage', 'IIIB', model=Observation,
        observation_source_value='FHIR-condition-stage')
    Observation.objects.filter(pk=row.pk).update(observation_concept_id=0)
    assert cards(client)['Breast cancer']['stage']['value'] == 'IIIB'
    assert cards(client)['Multiple myeloma']['stage']['value'] == 'Legacy II'


def test_fhir_upsert_does_not_collapse_different_cancers(cancers):
    from patient_portal.api.fhir.sync import FhirSyncView
    record, mine, other, event, stage, client = cancers
    a = fact(cancers, mine, 'stage', 'II', on='2025-01-01')
    b = fact(cancers, other, 'stage', 'IV', on='2025-01-01')
    unlinked = Measurement(person=record.person, measurement_concept=stage, measurement_type_concept_id=32817,
        measurement_date=a.measurement_date, measurement_source_value='21908-9', value_as_string='Legacy')
    ids = FhirSyncView()._upsert_clinical(Measurement, 'measurement_id', 'measurement_concept_id',
        'measurement_date', 'measurement_source_value', record.person, [unlinked], 'test', record.organization)
    assert len(ids) == 1
    a.refresh_from_db()
    b.refresh_from_db()
    assert (a.measurement_event_id, b.measurement_event_id) == (mine.pk, other.pk)
    assert Measurement.objects.get(pk=ids[0]).measurement_event_id is None


def test_curated_shared_concept_keeps_distinct_source_keys(cancers):
    record, mine, other, _, stage, client = cancers
    for field, source in (('stage', 'curated-stage'), ('histologic_type', 'curated-subtype')):
        FieldConceptMapping.objects.update_or_create(field_name=field, defaults={'concept': stage,
            'source_value': source, 'vocabulary_id': 'LOINC', 'concept_code': '21908-9', 'status': 'approved'})
    fact(cancers, other, 'stage', 'III', measurement_source_value='curated-stage')
    fact(cancers, other, 'stage', 'Ductal', measurement_source_value='curated-subtype')
    card = cards(client)['Breast cancer']
    assert card['stage']['value'] == 'III'
    assert card['subtype'] == 'Ductal'


def test_cytogenetic_collection_retains_contributor_dates(cancers):
    record, mine, other, _, _, client = cancers
    fact(cancers, other, 'cytogenetic_markers', 'del17p', model=Observation,
        on='2023-01-01', observation_source_value='cytogenetic:del17p')
    fact(cancers, other, 'cytogenetic_markers', '1q_gain', model=Observation,
        on='2024-01-01', observation_source_value='cytogenetic:1q_gain')
    origin = cards(client)['Breast cancer']['biomarkers'][0]['source']
    assert origin['date'] == '2024-01-01'
    assert {part['date'] for part in origin['contributors']} == {'2023-01-01', '2024-01-01'}


def test_linked_fact_can_be_retired_after_its_diagnosis_but_not_reactivated(cancers):
    record, mine, other, _, _, client = cancers
    row = fact(cancers, other, 'stage', 'II')
    type(other).objects.filter(pk=other.pk).update(is_erroneous=True)
    response = client.patch(f'/api/v1/measurements/{row.pk}/?skip_refresh=true', {'is_erroneous': True}, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db()
    assert row.is_erroneous
    response = client.patch(f'/api/v1/measurements/{row.pk}/?include_erroneous=true', {'is_erroneous': False}, format='json')
    assert response.status_code == 400
