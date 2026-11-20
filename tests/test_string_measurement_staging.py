"""Slice 2a: staging fields written as patient-authored string Measurements.

`_sync_string_measurement` (via `sync_to_omop`) writes a CB staging code into a Measurement's
value_as_string, keyed by the LOINC the derivation reads, scoped to the 'Patient self-report' type so
a same-day IMPORTED fact for the same concept is never clobbered.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.models import Concept, Measurement
from omop_core.services.omop_write_service import sync_to_omop
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

PATIENT_REPORTED_TYPE = 32865
LAB_TYPE = 32856


def _seed():
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=loinc)
    for code, cid in [('21908-9', 990001), ('21905-5', 990002), ('21906-3', 990003), ('21901-4', 990004)]:
        ConceptFactory(concept_id=cid, concept_name=f'Stage {code}', concept_code=code, vocabulary=loinc)
    tc = VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(concept_id=PATIENT_REPORTED_TYPE, concept_name='Patient self-report',
                   concept_code=str(PATIENT_REPORTED_TYPE), vocabulary=tc)


def test_staging_writes_a_patient_reported_string_measurement():
    _seed()
    person = PersonFactory()
    sync_to_omop(SimpleNamespace(person=person, tumor_stage='t1'), {'tumor_stage'}, today=date(2026, 1, 1))

    m = Measurement.objects.get(person=person, measurement_source_value='21905-5')
    assert m.value_as_string == 't1'                       # code stored verbatim
    assert m.value_as_number is None
    assert m.measurement_type_concept_id == PATIENT_REPORTED_TYPE


def test_all_four_staging_fields_map_to_their_loinc():
    _seed()
    person = PersonFactory()
    pi = SimpleNamespace(person=person, stage='III', tumor_stage='t2', nodes_stage='n1',
                         distant_metastasis_stage='m1')
    sync_to_omop(pi, {'stage', 'tumor_stage', 'nodes_stage', 'distant_metastasis_stage'},
                 today=date(2026, 1, 1))
    got = {m.measurement_source_value: m.value_as_string
           for m in Measurement.objects.filter(person=person)}
    assert got == {'21908-9': 'III', '21905-5': 't2', '21906-3': 'n1', '21901-4': 'm1'}


def test_re_edit_updates_in_place_not_appends():
    _seed()
    person = PersonFactory()
    for val in ('t1', 't3'):
        sync_to_omop(SimpleNamespace(person=person, tumor_stage=val), {'tumor_stage'}, today=date(2026, 1, 1))
    rows = Measurement.objects.filter(person=person, measurement_source_value='21905-5')
    assert rows.count() == 1
    assert rows.first().value_as_string == 't3'


def test_does_not_clobber_an_imported_same_day_fact():
    """The upsert key is scoped to the Patient-self-report type, so a same-day IMPORTED fact for the
    same concept (a different type) survives untouched — only our own patient-authored row is written."""
    _seed()
    person = PersonFactory()
    concept = Concept.objects.get(concept_code='21905-5', vocabulary_id='LOINC')
    tc = VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    lab_type = ConceptFactory(concept_id=LAB_TYPE, concept_name='Lab', concept_code=str(LAB_TYPE), vocabulary=tc)
    imported = MeasurementFactory(person=person, measurement_concept=concept,
                                  measurement_date=date(2026, 1, 1), measurement_type_concept=lab_type,
                                  value_as_string='IMPORTED')

    sync_to_omop(SimpleNamespace(person=person, tumor_stage='t1'), {'tumor_stage'}, today=date(2026, 1, 1))

    imported.refresh_from_db()
    assert imported.value_as_string == 'IMPORTED'          # imported fact untouched
    ours = Measurement.objects.filter(person=person, measurement_concept=concept,
                                      measurement_type_concept_id=PATIENT_REPORTED_TYPE)
    assert ours.count() == 1 and ours.first().value_as_string == 't1'


def test_generic_fallback_keeps_staging_fields_on_distinct_rows():
    """When the staging LOINC concepts are missing from a partially-loaded vocab, all four fields fall
    back to the generic sentinel (concept 0). measurement_source_value being part of the upsert key keeps
    stage/T/N/M on their own rows instead of overwriting each other into one."""
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=loinc)
    tc = VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(concept_id=PATIENT_REPORTED_TYPE, concept_name='Patient self-report',
                   concept_code=str(PATIENT_REPORTED_TYPE), vocabulary=tc)
    person = PersonFactory()
    pi = SimpleNamespace(person=person, stage='III', tumor_stage='t2', nodes_stage='n1',
                         distant_metastasis_stage='m1')
    sync_to_omop(pi, {'stage', 'tumor_stage', 'nodes_stage', 'distant_metastasis_stage'},
                 today=date(2026, 1, 1))
    got = {m.measurement_source_value: m.value_as_string
           for m in Measurement.objects.filter(person=person, measurement_concept_id=0)}
    assert got == {'21908-9': 'III', '21905-5': 't2', '21906-3': 'n1', '21901-4': 'm1'}


def test_blank_value_is_a_no_op():
    _seed()
    person = PersonFactory()
    sync_to_omop(SimpleNamespace(person=person, tumor_stage=''), {'tumor_stage'}, today=date(2026, 1, 1))
    assert not Measurement.objects.filter(person=person, measurement_source_value='21905-5').exists()
