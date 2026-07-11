"""
Tests for benchmark_trial_eligibility management command.

Covers:
  - Runs end-to-end against a small fixture cohort.
  - Uses the organization-slug cohort selection path.
  - Writes an output JSON file when requested.
"""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import Organization, PatientRecord
from tests.factories import (
    ConditionOccurrenceFactory,
    ConceptFactory,
    MeasurementFactory,
    ObservationFactory,
    PatientRecordFactory,
    PersonFactory,
)


pytestmark = pytest.mark.django_db


def _seed_trial_eligibility_patient(org):
    person = PersonFactory(year_of_birth=1960)
    patient = PatientRecordFactory(
        person=person,
        organization=org,
        patient_age=65,
        gender='M',
        disease='multiple myeloma',
        stage='Stage II',
        ecog_performance_status=1,
        karnofsky_performance_score=90,
        hemoglobin_g_dl=12.4,
        platelet_count_thousand_per_ul=180.0,
        anc_thousand_per_ul=2.1,
        wbc_count_thousand_per_ul=6.3,
        serum_creatinine_mg_dl=1.0,
        creatinine_clearance_ml_min=75.0,
        serum_calcium_mg_dl=9.2,
        bilirubin_total_mg_dl=0.6,
        ast_u_l=18,
        alt_u_l=21,
        albumin_g_dl=4.0,
        her2_status='NEGATIVE',
        estrogen_receptor_status='POSITIVE',
        progesterone_receptor_status='POSITIVE',
    )

    ConditionOccurrenceFactory(
        person=person,
        condition_concept=ConceptFactory(
            concept_name='Multiple myeloma',
            concept_code='MM-1',
        ),
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Cancer stage', concept_code='21908-9'),
        measurement_source_value='21908-9',
        value_as_string='Stage II',
    )
    ObservationFactory(
        person=person,
        observation_concept=ConceptFactory(concept_name='ECOG performance status', concept_code='ECOG-1'),
        value_as_number=1,
    )
    ObservationFactory(
        person=person,
        observation_concept=ConceptFactory(concept_name='Karnofsky performance score', concept_code='KPS-1'),
        value_as_number=90,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Hemoglobin', concept_code='718-7'),
        measurement_source_value='718-7',
        value_as_number=12.4,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Platelet count', concept_code='777-3'),
        measurement_source_value='777-3',
        value_as_number=180.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Absolute neutrophil count', concept_code='751-8'),
        measurement_source_value='751-8',
        value_as_number=2.1,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='White blood cell count', concept_code='6690-2'),
        measurement_source_value='6690-2',
        value_as_number=6.3,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Serum creatinine', concept_code='2160-0'),
        measurement_source_value='2160-0',
        value_as_number=1.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Creatinine clearance', concept_code='2164-2'),
        measurement_source_value='2164-2',
        value_as_number=75.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Serum calcium', concept_code='17861-6'),
        measurement_source_value='17861-6',
        value_as_number=9.2,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Total bilirubin', concept_code='1975-2'),
        measurement_source_value='1975-2',
        value_as_number=0.6,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='AST', concept_code='1920-8'),
        measurement_source_value='1920-8',
        value_as_number=18,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='ALT', concept_code='1742-6'),
        measurement_source_value='1742-6',
        value_as_number=21,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Albumin', concept_code='1751-7'),
        measurement_source_value='1751-7',
        value_as_number=4.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='HER2', concept_code='48676-1'),
        measurement_source_value='48676-1',
        value_as_string='Negative',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Estrogen receptor', concept_code='16112-5'),
        measurement_source_value='16112-5',
        value_as_string='Positive',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Progesterone receptor', concept_code='16113-3'),
        measurement_source_value='16113-3',
        value_as_string='Positive',
    )

    return person, patient


class TestBenchmarkTrialEligibility:

    def test_runs_for_org_slug(self, capsys):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        _seed_trial_eligibility_patient(org)

        call_command('benchmark_trial_eligibility', org_slugs='synthea-mm')

        out = capsys.readouterr().out
        assert 'patient_record pull:' in out
        assert 'OMOP pull:' in out
        assert '20' in out

    def test_output_file_written(self, tmp_path):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        _seed_trial_eligibility_patient(org)
        output_path = tmp_path / 'results.json'

        call_command(
            'benchmark_trial_eligibility',
            org_slugs='synthea-mm',
            output=str(output_path),
        )

        assert output_path.exists()

    def test_empty_cohort_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command('benchmark_trial_eligibility', person_ids='999999999')

