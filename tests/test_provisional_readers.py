"""Readers that bypass patient_record leave provisional rows out (#2530).

A clinical row with a ``confidence`` came from a proposed, not yet approved,
mapping. ``patient_record`` already ignores it (phase 1a); these readers query
the OMOP tables directly, so each has to say so itself. Every test builds a
real row and a provisional twin: the real one proves the reader reads the
table, the provisional one must not appear.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from omop_core.models import (
    FieldConceptMapping, ProcedureOccurrence, PatientRecord,
)
from omop_core.services import provenance_service as prov
from omop_core.services.fhir_export import build_fhir_bundle
from omop_core.services.patient_list_context import annotate_context
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConceptFactory, ConditionOccurrenceFactory, DrugExposureFactory,
    MeasurementFactory, ObservationFactory, PatientRecordFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture()
def person():
    return PatientRecordFactory().person


def entry(table, **kw):
    return SimpleNamespace(
        omop_table=table, concept_codes=kw.get('concept_codes'), source_values=kw.get('source_values'),
        concept_name_patterns=kw.get('concept_name_patterns'), selection_rule=kw.get('selection_rule', 'latest'),
    )


def ids(rows):
    return {row['id'] for row in rows}


def test_provenance_measurement_lookup_skips_provisional(person):
    with suppress_patient_record_refresh():
        real = MeasurementFactory(person=person, measurement_source_value='HGB', value_as_number=12,
                                  measurement_date=date(2024, 1, 1))
        guess = MeasurementFactory(person=person, measurement_source_value='HGB', value_as_number=99,
                                   measurement_date=date(2024, 6, 1), confidence=80)
    rows = prov._lookup_measurement(person, entry('Measurement', source_values=['HGB']))
    assert ids(rows) == {real.pk}
    # The latest row is the "selected" one, so a provisional newest row must not take it.
    assert rows[0]['selected'] and rows[0]['id'] != guess.pk


def test_provenance_observation_lookup_skips_provisional(person):
    with suppress_patient_record_refresh():
        real = ObservationFactory(person=person, observation_source_value='FEVER', value_as_number=1)
        ObservationFactory(person=person, observation_source_value='FEVER', value_as_number=0,
                           observation_date=date(2025, 1, 1), confidence=75)
    rows = prov._lookup_observation(person, entry('Observation', source_values=['FEVER']))
    assert ids(rows) == {real.pk}


def test_provenance_condition_drug_and_procedure_lookups_skip_provisional(person):
    with suppress_patient_record_refresh():
        cond = ConditionOccurrenceFactory(person=person)
        ConditionOccurrenceFactory(person=person, condition_start_date='2025-01-01', confidence=90)
        drug = DrugExposureFactory(person=person)
        DrugExposureFactory(person=person, confidence=90)
        type_concept = ConceptFactory(concept_name='Proc type', concept_code='TYPE-PROC')
        proc = ProcedureOccurrence.objects.create(
            procedure_occurrence_id=1, person=person, procedure_concept=ConceptFactory(),
            procedure_date=date(2024, 1, 1), procedure_type_concept=type_concept)
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=2, person=person, procedure_concept=ConceptFactory(),
            procedure_date=date(2025, 1, 1), procedure_type_concept=type_concept, confidence=90)
    assert ids(prov._lookup_condition(person, entry('ConditionOccurrence'))) == {cond.pk}
    assert ids(prov._lookup_drug_exposure(person, entry('DrugExposure'))) == {drug.pk}
    assert ids(prov._lookup_procedure(person, entry('ProcedureOccurrence'))) == {proc.pk}


def test_provenance_sct_lookup_skips_provisional(person):
    from omop_core.services.clinical_input_contract import RECIPES
    canonical = RECIPES['sct_date'][0]
    with suppress_patient_record_refresh():
        real = ObservationFactory(person=person, observation_source_value=canonical,
                                  observation_date=date(2023, 1, 1), value_as_string='2023-01-01')
        ObservationFactory(person=person, observation_source_value=canonical,
                           observation_date=date(2024, 1, 1), value_as_string='2024-01-01', confidence=85)
    assert ids(prov._lookup_sct(person)) == {real.pk}


def _tables(bundle):
    return [e['resource']['resourceType'] for e in bundle['entry']]


def test_fhir_export_leaves_provisional_rows_out_unless_asked(person):
    with suppress_patient_record_refresh():
        MeasurementFactory(person=person, value_as_number=1)
        MeasurementFactory(person=person, value_as_number=2, confidence=80)
        ObservationFactory(person=person, value_as_number=1)
        ObservationFactory(person=person, value_as_number=2, confidence=80)
        ConditionOccurrenceFactory(person=person)
        ConditionOccurrenceFactory(person=person, confidence=80)
        DrugExposureFactory(person=person)
        DrugExposureFactory(person=person, confidence=80)
    default = _tables(build_fhir_bundle(person))
    everything = _tables(build_fhir_bundle(person, include_provisional=True))
    # Patient + four real rows; the opt-in adds the four provisional ones.
    assert len(default) == 5
    assert len(everything) == 9


def test_patient_list_freshness_ignores_provisional_results():
    record = PatientRecordFactory()
    today = timezone.localdate()
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_date=today - timedelta(days=100), value_as_number=1)
        MeasurementFactory(person=record.person, measurement_date=today, value_as_number=1, confidence=80)
        ObservationFactory(person=record.person, observation_date=today, value_as_number=1, confidence=80)
    row = annotate_context(PatientRecord.objects.filter(pk=record.pk)).get()
    assert row.latest_result_date == today - timedelta(days=100)
    # A patient whose only result is provisional has none.
    only_guess = PatientRecordFactory()
    with suppress_patient_record_refresh():
        MeasurementFactory(person=only_guess.person, measurement_date=today, value_as_number=1, confidence=80)
    row = annotate_context(PatientRecord.objects.filter(pk=only_guess.pk)).get()
    assert row.latest_result_date is None
