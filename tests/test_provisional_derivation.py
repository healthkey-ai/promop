"""patient_record derives from real rows only (provisional data, phase 1a, #2529).

A clinical row with a ``confidence`` number is a proposed mapping's row. It
must never become a value in ``patient_record``: that table holds approved
data only (docs/provisional_data_plan.md, invariant 3).

Each test builds the same row twice, once real and once provisional. The real
one must change the record, which proves the row is something the derivation
reads; the provisional one must leave the record exactly as it was.
"""
from datetime import date

import pytest

from omop_core.models import FieldConceptMapping
from omop_core.services.patient_record_service import (
    _build_snapshot, refresh_patient_record,
)
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConceptFactory, ConditionOccurrenceFactory, DomainFactory,
    DrugExposureFactory, MeasurementFactory, ObservationFactory,
    PatientRecordFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

EHR_TYPE = 32817
FEVER_CODE = '386661006'


@pytest.fixture()
def record(settings):
    settings.CELERY_BROKER_URL = ''
    return PatientRecordFactory(disease='chronic lymphocytic leukemia')


@pytest.fixture()
def snomed():
    VocabularyFactory(vocabulary_id='SNOMED')
    VocabularyFactory(vocabulary_id='Type Concept')
    VocabularyFactory(vocabulary_id='LOINC')


def _concept(domain, concept_id, code, name, vocabulary='SNOMED'):
    from omop_core.models import Concept, Domain
    if not Domain.objects.filter(domain_id=domain).exists():
        DomainFactory(domain_id=domain, domain_name=domain)
    existing = Concept.objects.filter(concept_id=concept_id).first()
    return existing or ConceptFactory(
        concept_id=concept_id, concept_code=code, vocabulary_id=vocabulary,
        concept_name=name, domain_id=domain, standard_concept='S',
    )


def _add_observation(person, confidence):
    concept = _concept('Observation', 4158517, FEVER_CODE, 'Fever')
    type_concept = _concept('Observation', EHR_TYPE, '32817', 'EHR', 'Type Concept')
    FieldConceptMapping.objects.get_or_create(
        field_name='b_symptom_fever',
        defaults=dict(
            concept=concept, vocabulary_id='SNOMED', concept_code=FEVER_CODE,
            omop_table='observation', source_value=FEVER_CODE, value_kind='boolean',
            type_concept_id=type_concept.concept_id, status='approved',
        ),
    )
    ObservationFactory(
        person=person, observation_concept=concept, observation_date=date(2024, 6, 1),
        value_as_number=1.0, observation_source_value=FEVER_CODE, confidence=confidence,
    )
    return 'b_symptom_fever'


def _add_measurement(person, confidence):
    concept = _concept('Measurement', 3000963, '718-7', 'Hemoglobin [Mass/volume] in Blood',
                       'LOINC')
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_date=date(2024, 6, 1),
        value_as_number=11.5, measurement_source_value='718-7',
        unit_source_value='g/dL', value_source_value='11.5', confidence=confidence,
    )
    return 'hemoglobin_g_dl'


def _add_condition(person, confidence):
    concept = _concept('Condition', 4001, '5601009', 'Lymphadenopathy')
    ConditionOccurrenceFactory(
        person=person, condition_concept=concept, condition_source_value='5601009',
        condition_start_date=date(2024, 6, 1), confidence=confidence,
    )
    return 'lymphadenopathy'


def _add_drug_exposure(person, confidence):
    concept = _concept('Drug', 1500001, 'RX-1', 'Acalabrutinib', 'SNOMED')
    DrugExposureFactory(
        person=person, drug_concept=concept, drug_source_value='RX-1',
        drug_exposure_start_date=date(2024, 6, 1), drug_exposure_end_date=date(2024, 7, 1),
        confidence=confidence,
    )
    return 'concomitant_medications'


def _add_procedure(person, confidence):
    from omop_core.models import ProcedureOccurrence
    from omop_core.services.pk import next_pk
    concept = _concept('Procedure', 4002, '399208008', 'Plain chest X-ray')
    type_concept = _concept('Type Concept', 44818, '44818', 'EHR', 'Type Concept')
    ProcedureOccurrence.objects.create(
        procedure_occurrence_id=next_pk(ProcedureOccurrence, 'procedure_occurrence_id'),
        person=person, procedure_concept=concept, procedure_date=date(2024, 6, 1),
        procedure_type_concept=type_concept, procedure_source_value='399208008',
        confidence=confidence,
    )
    return 'prior_procedures'


ADDERS = {
    'observation': _add_observation,
    'measurement': _add_measurement,
    'condition_occurrence': _add_condition,
    'drug_exposure': _add_drug_exposure,
    'procedure_occurrence': _add_procedure,
}


@pytest.mark.parametrize('table', sorted(ADDERS))
def test_a_real_row_reaches_the_record(snomed, record, table):
    """Control: the row below is one the derivation reads, so the next test means something."""
    baseline = refresh_patient_record(record.person)
    with suppress_patient_record_refresh():
        field = ADDERS[table](record.person, None)

    refreshed = refresh_patient_record(record.person)

    assert getattr(refreshed, field) != getattr(baseline, field), table


@pytest.mark.parametrize('table', sorted(ADDERS))
def test_a_provisional_row_leaves_the_record_unchanged(snomed, record, table):
    baseline = refresh_patient_record(record.person)
    with suppress_patient_record_refresh():
        field = ADDERS[table](record.person, 60)

    refreshed = refresh_patient_record(record.person)

    assert getattr(refreshed, field) == getattr(baseline, field), table


@pytest.mark.parametrize('table', sorted(ADDERS))
def test_a_provisional_row_is_kept_apart_in_the_snapshot(snomed, record, table):
    with suppress_patient_record_refresh():
        ADDERS[table](record.person, 60)
        ADDERS[table](record.person, None)

    snapshot = _build_snapshot(record.person)

    real = {
        'observation': snapshot.observations,
        'measurement': snapshot.measurements,
        'condition_occurrence': snapshot.conditions,
        'drug_exposure': snapshot.drug_exposures,
        'procedure_occurrence': snapshot.procedures,
    }[table]
    provisional = {
        'observation': snapshot.provisional_observations,
        'measurement': snapshot.provisional_measurements,
        'condition_occurrence': snapshot.provisional_conditions,
        'drug_exposure': snapshot.provisional_drug_exposures,
        'procedure_occurrence': snapshot.provisional_procedures,
    }[table]
    assert [row.confidence for row in real] == [None]
    assert [row.confidence for row in provisional] == [60]


def test_a_provisional_row_does_not_override_a_real_one(snomed, record):
    """Both rows name the same fact; only the real one is read."""
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 60)
    provisional_only = refresh_patient_record(record.person).hemoglobin_g_dl

    with suppress_patient_record_refresh():
        concept = _concept('Measurement', 3000963, '718-7', 'Hemoglobin [Mass/volume] in Blood',
                           'LOINC')
        MeasurementFactory(
            person=record.person, measurement_concept=concept,
            measurement_date=date(2024, 5, 1), value_as_number=9.0,
            measurement_source_value='718-7', unit_source_value='g/dL',
            value_source_value='9.0',
        )
    refreshed = refresh_patient_record(record.person)

    assert provisional_only is None
    assert float(refreshed.hemoglobin_g_dl) == 9.0


def test_a_provisional_clear_marker_does_not_hide_a_real_value(snomed, record):
    """The split happens before cleared history is applied, so a clear that is itself
    provisional cannot suppress the approved result it would otherwise erase."""
    from omop_core.services.omop_projection import CLEAR_VALUE
    concept = _concept('Measurement', 3000963, '718-7', 'Hemoglobin [Mass/volume] in Blood', 'LOINC')
    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=record.person, measurement_concept=concept,
            measurement_date=date(2024, 5, 1), value_as_number=9.0,
            measurement_source_value='718-7', unit_source_value='g/dL',
            value_source_value='9.0')
        MeasurementFactory(
            person=record.person, measurement_concept=concept,
            measurement_date=date(2024, 6, 1), value_as_number=None,
            measurement_source_value='718-7', unit_source_value='g/dL',
            value_source_value=CLEAR_VALUE, confidence=60)

    assert float(refresh_patient_record(record.person).hemoglobin_g_dl) == 9.0

    # Control: the same clear marker, real, does erase it.
    from omop_core.models import Measurement
    Measurement.objects.filter(confidence=60).update(confidence=None)
    assert refresh_patient_record(record.person).hemoglobin_g_dl is None
