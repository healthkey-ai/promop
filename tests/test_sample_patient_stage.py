from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import Observation, PatientRecord
from omop_core.services.patient_record_service import (
    FHIR_CONDITION_STAGE_SOURCE_VALUE, SAMPLE_STAGE_SOURCE_VALUE,
    _get_staging_data, refresh_patient_record,
)
from omop_core.services.sample_patient_stage import ensure_sample_patient_stage
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConceptFactory, MeasurementFactory, ObservationFactory,
    OrganizationFactory, PatientRecordFactory,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('slug,disease,allowed', [
    ('synthea-bc', 'Malignant tumor of breast', {'I', 'IIA', 'IIB', 'IIIA', 'IIIB', 'IV'}),
    ('synthea-mm', 'Multiple myeloma', {'ISS I', 'ISS II', 'ISS III'}),
    ('synthea-fl', 'Follicular lymphoma grade 3b', {'I', 'II', 'III', 'IV'}),
    ('synthea-fl', 'Diffuse large B-cell lymphoma', {'I', 'II', 'III', 'IV'}),
])
def test_backfill_is_complete_durable_and_idempotent(slug, disease, allowed):
    record = PatientRecordFactory(organization=OrganizationFactory(slug=slug), disease=disease, stage=None)
    call_command('backfill_sample_patient_stage', confirm=True)
    record.refresh_from_db()
    stage = record.stage
    assert stage in allowed
    fact = Observation.objects.get(person=record.person, observation_source_value=SAMPLE_STAGE_SOURCE_VALUE)
    assert fact.qualifier_source_value == 'synthetic fallback'
    assert fact.observation_concept_id == 0
    assert refresh_patient_record(record.person).stage == stage
    call_command('backfill_sample_patient_stage', confirm=True)
    assert Observation.objects.filter(person=record.person).count() == 1
    record.refresh_from_db()
    assert record.stage == stage


def test_dry_run_and_default_scope():
    sample = PatientRecordFactory(organization=OrganizationFactory(slug='synthea-fl'), stage=None)
    other = PatientRecordFactory(stage=None)
    before = Observation.objects.count()
    call_command('backfill_sample_patient_stage')
    call_command('backfill_sample_patient_stage', confirm=True, dry_run=True)
    sample.refresh_from_db()
    assert sample.stage is None
    assert Observation.objects.count() == before
    call_command('backfill_sample_patient_stage', confirm=True)
    other.refresh_from_db()
    assert other.stage is None
    assert not Observation.objects.filter(person=other.person).exists()


def test_backfill_preserves_projection_and_real_source_wins_later():
    record = PatientRecordFactory(organization=OrganizationFactory(slug='synthea-bc'), stage='IIIB')
    call_command('backfill_sample_patient_stage', confirm=True)
    assert refresh_patient_record(record.person).stage == 'IIIB'
    ObservationFactory(person=record.person, observation_source_value='21908-9', value_as_string='Stage IV')
    assert refresh_patient_record(record.person).stage == 'Stage IV'


def test_existing_omop_stage_does_not_create_synthetic_fact():
    record = PatientRecordFactory(organization=OrganizationFactory(slug='synthea-mm'))
    ObservationFactory(person=record.person, observation_source_value='21908-9-riss', value_as_string='R-ISS III')
    PatientRecord.objects.filter(pk=record.pk).update(stage=None)
    call_command('backfill_sample_patient_stage', confirm=True)
    record.refresh_from_db()
    assert record.stage == 'R-ISS III'
    assert not Observation.objects.filter(observation_source_value=SAMPLE_STAGE_SOURCE_VALUE).exists()


def test_blank_and_boolean_measurements_do_not_mask_stage_observation():
    record = PatientRecordFactory()
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_source_value='21908-9', value_as_string=None, value_as_number=None)
        MeasurementFactory(person=record.person, measurement_source_value='21908-9', value_as_string='True', value_as_number=None)
        ObservationFactory(person=record.person, observation_source_value='21908-9', value_as_string='Stage IIIB')
    assert _get_staging_data(record.person)['stage'] == 'Stage IIIB'


def test_coded_stage_value_and_riss_preference():
    record = PatientRecordFactory()
    with suppress_patient_record_refresh():
        ObservationFactory(person=record.person, observation_source_value='21908-9', value_as_string='ISS II')
        ObservationFactory(person=record.person, observation_source_value='21908-9-riss',
                           value_as_concept=ConceptFactory(concept_name='R-ISS III'))
    assert _get_staging_data(record.person)['stage'] == 'R-ISS III'


def test_erroneous_source_is_excluded():
    record = PatientRecordFactory(stage=None)
    ObservationFactory(person=record.person, observation_source_value='21908-9', value_as_string='Stage IV', is_erroneous=True)
    assert 'stage' not in _get_staging_data(record.person)


def test_boolean_legacy_recist_falls_back_to_condition_stage():
    record = PatientRecordFactory()
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_source_value='21908-9', value_as_string='False', value_as_number=None)
        ObservationFactory(person=record.person, observation_source_value=FHIR_CONDITION_STAGE_SOURCE_VALUE, value_as_string='IIA')
    assert _get_staging_data(record.person)['stage'] == 'IIA'


def test_explicit_org_selection_limit_and_unknown_disease():
    org = OrganizationFactory(slug='custom-demo')
    first = PatientRecordFactory(organization=org, disease='Follicular Lymphoma', stage=None)
    second = PatientRecordFactory(organization=org, disease='Unknown', stage=None)
    call_command('backfill_sample_patient_stage', org_slugs='CUSTOM-DEMO', limit=1, confirm=True)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.stage
    assert second.stage is None
    with pytest.raises(CommandError, match='no supported disease'):
        call_command('backfill_sample_patient_stage', org_slugs='custom-demo', confirm=True)


def test_fl_enrichment_alone_populates_stage():
    record = PatientRecordFactory(organization=OrganizationFactory(slug='synthea-fl'), stage=None)
    out = StringIO()
    call_command('enrich_synthea_fl_omop_data', confirm=True, stdout=out)
    record.refresh_from_db()
    assert record.stage
    assert 'stage' in out.getvalue()


def test_deterministic_preview_matches_apply():
    record = PatientRecordFactory(stage=None, diagnosis_date=date(2020, 1, 1))
    preview, _ = ensure_sample_patient_stage(record, disease='FL', dry_run=True)
    applied, _ = ensure_sample_patient_stage(record, disease='FL')
    assert preview == applied
    assert Observation.objects.get(person=record.person).observation_date == date(2020, 1, 1)


@pytest.mark.parametrize('disease', ['breast-cancer', 'mm', 'fl'])
def test_generators_include_stage_without_boolean_stage_codes(tmp_path, disease, monkeypatch):
    import json
    # Stage generation is independent of the installed HemOnc release.
    monkeypatch.setattr('omop_core.management.commands._fl_generator.load_hemonc_regimens_for_disease',
                        lambda _: [{'concept_id': 35804570, 'concept_name': 'BR',
                                    'drugs': ['bendamustine', 'rituximab']}])
    bundle_path = tmp_path / 'bundle.json'
    call_command('generate_fhir_bundle', disease=disease, count=3, seed=42, output=str(bundle_path))
    resources = [e['resource'] for e in json.loads(bundle_path.read_text())['entry']]
    patients = [r for r in resources if r['resourceType'] == 'Patient']
    for patient in patients:
        conditions = [r for r in resources if r['resourceType'] == 'Condition'
                      and r.get('subject', {}).get('reference') == f"Patient/{patient['id']}"]
        assert any(c.get('stage') for c in conditions)
    stage_obs = [r for r in resources if r['resourceType'] == 'Observation'
                 and any(c.get('code') == '21908-9' for c in r.get('code', {}).get('coding', []))]
    assert all('valueBoolean' not in r for r in stage_obs)
    if disease == 'fl':
        assert len(stage_obs) == 3
        for obs in stage_obs:
            condition = next(r for r in resources if r['resourceType'] == 'Condition'
                             and r.get('subject') == obs['subject'] and r.get('stage'))
            assert condition['stage'][0]['summary']['text'].endswith(obs['valueString'])
