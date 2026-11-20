"""#4889: `_sync_measurement` (numeric labs) must key its upsert on measurement_source_value.

When a lab's LOINC concept is missing from a partially-loaded vocab, `_sync_measurement` falls back to
the generic sentinel (CONCEPT_GENERIC_LAB == 0). Before the fix the upsert keyed only on
(person, measurement_concept, measurement_date), so several same-day fields all sharing concept 0
collapsed onto ONE row: the second write overwrote the first field's value while keeping the first
field's source, corrupting a neighbour and losing itself. Including measurement_source_value in the key
keeps each field on its own row — mirroring the staging string-Measurement upsert.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.models import Concept, Measurement
from omop_core.services.omop_write_service import sync_to_omop
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

EHR_TYPE = 32817   # a genuinely IMPORTED/EHR type — distinct from what the numeric write path stamps


def test_generic_fallback_keeps_numeric_labs_on_distinct_rows():
    # No specific LOINC concepts seeded -> hemoglobin (718-7) and WBC (6690-2) both fall back to the
    # generic sentinel (concept 0). With source_value in the upsert key they land on two rows carrying
    # their true values; before the fix they collapsed onto one (source 718-7, value 4.0).
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=loinc)
    person = PersonFactory()
    pi = SimpleNamespace(person=person, hemoglobin_g_dl=12.5, wbc_count_thousand_per_ul=4.0)

    sync_to_omop(pi, {'hemoglobin_g_dl', 'wbc_count_thousand_per_ul'}, today=date(2026, 1, 1))

    rows = Measurement.objects.filter(person=person, measurement_concept_id=0)
    assert rows.count() == 2
    got = {m.measurement_source_value: float(m.value_as_number) for m in rows}
    assert got == {'718-7': 12.5, '6690-2': 4.0}


def test_re_editing_the_same_lab_updates_in_place():
    # Guard: a resolved LOINC concept must still upsert (one row, latest value) with source_value in the
    # key — adding it to the key must not turn re-edits into duplicate rows.
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=loinc)
    ConceptFactory(concept_id=910718, concept_code='718-7', vocabulary=loinc,
                   concept_name='Hemoglobin [Mass/volume] in Blood')
    person = PersonFactory()
    for val in (12.5, 13.1):
        sync_to_omop(SimpleNamespace(person=person, hemoglobin_g_dl=val), {'hemoglobin_g_dl'},
                     today=date(2026, 1, 1))

    rows = Measurement.objects.filter(person=person, measurement_source_value='718-7')
    assert rows.count() == 1
    assert float(rows.first().value_as_number) == 13.1


def test_generic_fallback_re_edit_stays_on_one_row():
    # Update-in-place under the generic-fallback case (concept 0): re-editing the SAME field keeps one row
    # (its source_value keys it) even though the LOINC concept is unresolved — the riskier path than the
    # resolved-concept re-edit above.
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=loinc)
    person = PersonFactory()
    for val in (12.5, 13.1):
        sync_to_omop(SimpleNamespace(person=person, hemoglobin_g_dl=val), {'hemoglobin_g_dl'},
                     today=date(2026, 1, 1))
    rows = Measurement.objects.filter(person=person, measurement_concept_id=0,
                                      measurement_source_value='718-7')
    assert rows.count() == 1
    assert float(rows.first().value_as_number) == 13.1


def test_a_same_day_imported_loinc_coded_lab_is_overwritten_by_a_patient_edit():
    # Pins a PRE-EXISTING numeric-path gap surfaced in the #4889 review. Unlike the staging string path,
    # the numeric upsert does NOT scope the key by measurement_type_concept — so it overwrites a same-day
    # imported lab (same concept + source_value) IN PLACE even though that imported row carries a DISTINCT
    # type (EHR) from what a patient edit represents. Using a distinct imported type makes this a real
    # guard, not a tautology: today the write ignores type and clobbers it; when #787 adds type-scoping +
    # patient-reported provenance, the write will no longer match the EHR-typed row, so it must be
    # PRESERVED and the patient edit must land on its own row — at which point both assertions below flip.
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=loinc)
    concept = ConceptFactory(concept_id=910718, concept_code='718-7', vocabulary=loinc,
                             concept_name='Hemoglobin [Mass/volume] in Blood')
    tc = VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ehr_type = ConceptFactory(concept_id=EHR_TYPE, concept_code=str(EHR_TYPE), concept_name='EHR',
                              vocabulary=tc)
    person = PersonFactory()
    imported = MeasurementFactory(person=person, measurement_concept=concept, measurement_date=date(2026, 1, 1),
                                  measurement_type_concept=ehr_type, measurement_source_value='718-7',
                                  value_as_number=9.9)

    sync_to_omop(SimpleNamespace(person=person, hemoglobin_g_dl=12.5), {'hemoglobin_g_dl'},
                 today=date(2026, 1, 1))

    imported.refresh_from_db()
    assert float(imported.value_as_number) == 12.5   # today: clobbered in place despite the distinct type
    assert Measurement.objects.filter(person=person, measurement_source_value='718-7').count() == 1
