"""Composite readers in the provisional pass (#2534).

Two readers run over provisional rows only: the disease fields, from the cancer
among the provisional conditions, and ``concomitant_medications``, from the
provisional drug exposures. Each keeps a value only where the real record has no
answer, and carries the lowest confidence among the rows that decide it.
"""
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from omop_core.models import (
    ConditionOccurrence, DrugExposure, PatientRecord, PatientRecordConfidence,
    PatientRecordProvisional,
)
from omop_core.services import provisional_record
from omop_core.services.patient_record_service import (
    _build_snapshot, refresh_patient_record,
)
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConditionOccurrenceFactory, DrugExposureFactory, PatientRecordFactory,
)
from tests.test_provisional_derivation import _concept, snomed  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''


@pytest.fixture()
def person():
    return PatientRecordFactory().person


def provisional(person):
    return PatientRecordProvisional.objects.get(person=person)


def marks(person):
    return PatientRecordConfidence.objects.get(person=person)


def cancer(person, confidence, name='Squamous cell carcinoma', concept_id=4001, start=date(2024, 3, 1)):
    concept = _concept('Condition', concept_id, f'C{concept_id}', name)
    return ConditionOccurrenceFactory(
        person=person, condition_concept=concept, condition_source_value=f'C{concept_id}',
        condition_start_date=start, confidence=confidence)


def drug(person, confidence, name, concept_id, start):
    concept = _concept('Drug', concept_id, f'RX-{concept_id}', name)
    return DrugExposureFactory(
        person=person, drug_concept=concept, drug_source_value=f'RX-{concept_id}',
        drug_exposure_start_date=start, drug_exposure_end_date=start, confidence=confidence)


# --- disease fields --------------------------------------------------------------------

def test_a_provisional_cancer_fills_the_disease_fields_with_its_confidence(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 82)

    refreshed = refresh_patient_record(person)

    assert refreshed.disease is None and refreshed.diagnosis_date is None   # approved only
    row, confidence = provisional(person), marks(person)
    assert 'squamous' in row.disease.lower()
    assert row.diagnosis_date == date(2024, 3, 1)
    assert row.disease_slug == 'squamous-cell-carcinoma'
    for field in provisional_record.DISEASE_FIELDS:
        if getattr(row, field) not in (None, ''):
            assert getattr(confidence, field) == 82, field


def test_the_confidence_is_the_one_of_the_cancer_that_is_chosen(snomed, person):
    with suppress_patient_record_refresh():
        # Newest, but ended: the real ranking puts a resolved cancer last.
        ended = ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4002, 'C4002', 'Basal cell carcinoma'),
            condition_source_value='C4002', condition_start_date=date(2024, 6, 1),
            condition_end_date=date(2024, 7, 1), confidence=95)
        cancer(person, 80, 'Squamous cell carcinoma', 4001, date(2023, 1, 1))   # current: chosen
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4005, 'C4005', 'Merkel cell carcinoma'),
            condition_source_value='C4005', condition_start_date=date(2022, 1, 1),
            condition_end_date=date(2022, 2, 1), confidence=71)
    assert ended.confidence == 95

    refresh_patient_record(person)

    # 80 is neither the highest (95) nor the lowest (71): it is the chosen row's own.
    assert 'squamous' in provisional(person).disease.lower()
    for field in provisional_record.DISEASE_FIELDS:
        if getattr(provisional(person), field) not in (None, ''):
            assert getattr(marks(person), field) == 80, field


def test_a_lower_confidence_cancer_is_chosen_when_the_ranking_says_so(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 55, 'Squamous cell carcinoma', 4001, date(2024, 6, 1))    # newest: chosen
        cancer(person, 97, 'Basal cell carcinoma', 4002, date(2023, 1, 1))

    refresh_patient_record(person)

    assert 'squamous' in provisional(person).disease.lower()
    assert marks(person).disease == 55 and marks(person).diagnosis_date == 55


def test_the_cancer_with_a_line_of_therapy_is_chosen(snomed, person, monkeypatch):
    from omop_core.services.disease_episodes import disease_key
    with suppress_patient_record_refresh():
        cancer(person, 74, 'Squamous cell carcinoma', 4001, date(2020, 1, 1))    # older, has the line
        cancer(person, 96, 'Basal cell carcinoma', 4002, date(2024, 6, 1))
    treated = disease_key('squamous-cell-carcinoma')
    monkeypatch.setattr(
        'omop_core.services.patient_record_service._cancers_with_lines',
        lambda person, snapshot, today: ({treated}, {treated}))

    refresh_patient_record(person)

    assert 'squamous' in provisional(person).disease.lower()
    assert marks(person).disease == 74


def test_a_condition_that_is_not_a_cancer_proposes_nothing(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 90, 'Pain', 4003)
    record = PatientRecord.objects.get(person=person)

    # Without a cancer the reader's fallbacks would take a symptom's date as the diagnosis.
    assert provisional_record.composite_proposals(person, record, _build_snapshot(person)) == ({}, {})
    refresh_patient_record(person)
    assert all(getattr(marks(person), field.name) is None
               for field in PatientRecordConfidence._meta.concrete_fields if field.name != 'person')
    assert provisional(person).diagnosis_date is None and provisional(person).disease is None


def test_real_beats_provisional_for_the_disease(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, None, 'Multiple myeloma', 4004, date(2020, 1, 1))
        cancer(person, 88, 'Squamous cell carcinoma', 4001, date(2024, 3, 1))

    refreshed = refresh_patient_record(person)

    assert 'myeloma' in refreshed.disease.lower()
    assert provisional(person).disease == refreshed.disease
    assert provisional(person).diagnosis_date == refreshed.diagnosis_date == date(2020, 1, 1)
    assert marks(person).disease is None and marks(person).diagnosis_date is None


def test_another_cancers_dates_do_not_fill_a_disease_the_record_already_names(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 88, 'Squamous cell carcinoma', 4001, date(2024, 3, 1))
    record = PatientRecord.objects.get(person=person)
    record.disease, record.disease_slug, record.diagnosis_date = 'multiple myeloma', 'multiple-myeloma', None

    values, found = provisional_record.composite_proposals(
        person, record, _build_snapshot(person))

    assert values == {} and found == {}


def test_the_same_cancer_fills_the_fields_the_record_leaves_empty(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 76, 'Multiple myeloma', 4004, date(2021, 5, 5))
    record = PatientRecord.objects.get(person=person)
    record.disease, record.disease_slug, record.diagnosis_date = 'multiple myeloma', 'multiple-myeloma', None

    values, found = provisional_record.composite_proposals(
        person, record, _build_snapshot(person))

    assert values == {'diagnosis_date': date(2021, 5, 5), 'diagnosis_date_precision': 'day'}
    assert found == {'diagnosis_date': 76, 'diagnosis_date_precision': 76}


def test_a_typed_disease_blocks_the_whole_group_even_for_the_same_cancer(snomed, person):
    """The slug guard would let the same cancer fill the date; only the edit stops it."""
    with suppress_patient_record_refresh():
        cancer(person, 88)
    PatientRecord.objects.filter(person=person).update(
        disease='Squamous cell carcinoma', disease_slug='squamous-cell-carcinoma',
        user_edited_fields=['disease'])

    refreshed = refresh_patient_record(person)

    assert refreshed.disease == 'Squamous cell carcinoma' and refreshed.diagnosis_date is None
    assert provisional(person).diagnosis_date is None
    assert marks(person).diagnosis_date is None and marks(person).disease is None


def test_a_cleared_disease_is_not_refilled_by_a_proposal(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 88)
    PatientRecord.objects.filter(person=person).update(disease=None, user_edited_fields=['disease'])

    refresh_patient_record(person)

    row = provisional(person)
    assert row.disease is None and row.disease_slug is None and row.diagnosis_date is None
    assert marks(person).disease is None and marks(person).disease_slug is None


def test_editing_the_disease_takes_the_whole_group_back_from_the_proposal(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 88)
    refresh_patient_record(person)
    assert marks(person).diagnosis_date == 88 and marks(person).disease_slug == 88

    record = PatientRecord.objects.get(person=person)
    record.disease = 'Multiple myeloma'
    record.user_edited_fields = ['disease']
    record.save(update_fields=['disease', 'user_edited_fields'])
    provisional_record.mirror_user_edit(record, ['disease'])

    shown, shown_marks = provisional_record.overlay(PatientRecord.objects.get(person=person))
    for field in provisional_record.DISEASE_FIELDS:
        assert field not in shown_marks, field
        assert getattr(marks(person), field) is None, field
    # Nothing of the proposal's cancer is shown beside the typed disease.
    assert shown.disease == 'Multiple myeloma'
    assert shown.diagnosis_date is None and not (shown.disease_slug or '').startswith('squamous')


def test_a_cancer_is_not_read_when_the_record_already_holds_every_disease_field(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 80, 'Squamous cell carcinoma', 4001, date(2023, 1, 1))
        cancer(person, 90, 'Basal cell carcinoma', 4002, date(2024, 6, 1))
    record = PatientRecord.objects.get(person=person)
    for field in provisional_record.DISEASE_FIELDS:
        setattr(record, field, 'x' if field != 'diagnosis_date' else date(2020, 1, 1))
    snapshot = _build_snapshot(person)

    with CaptureQueriesContext(connection) as queries:
        found = provisional_record._disease_proposals(person, record, snapshot)

    assert found == ({}, {}) and len(queries) == 0


def test_a_cancers_slug_is_worked_out_once_per_row(snomed, person):
    cancers = 5
    with suppress_patient_record_refresh():
        for index in range(cancers):
            cancer(person, 80 + index, f'Carcinoma type {index}', 4100 + index, date(2023, 1 + index, 1))
    record = PatientRecord.objects.get(person=person)
    snapshot = _build_snapshot(person)

    with CaptureQueriesContext(connection) as queries:
        provisional_record._disease_proposals(person, record, snapshot)

    categories = [q for q in queries.captured_queries if 'disease_category_mapping' in q['sql']]
    # One category lookup per cancer's slug, plus the two that name the disease
    # itself: not one per cancer in each of the ranking and the slug list.
    assert len(categories) <= cancers + 2, len(categories)


def test_a_reverted_proposal_takes_the_disease_back(snomed, person):
    with suppress_patient_record_refresh():
        condition = cancer(person, 88)
    refresh_patient_record(person)
    assert provisional(person).disease

    ConditionOccurrence.objects.filter(pk=condition.pk).update(condition_concept_id=0, confidence=None)
    refresh_patient_record(person)

    assert provisional(person).disease is None and marks(person).disease is None


# --- medications -----------------------------------------------------------------------

def test_provisional_drugs_fill_concomitant_medications_at_the_lowest_confidence(snomed, person):
    with suppress_patient_record_refresh():
        drug(person, 95, 'Levothyroxine', 5001, date(2024, 1, 1))
        drug(person, 72, 'Potassium chloride', 5002, date(2024, 2, 1))

    refreshed = refresh_patient_record(person)

    assert refreshed.concomitant_medications is None   # approved only
    assert provisional(person).concomitant_medications == 'Potassium chloride, Levothyroxine'
    assert marks(person).concomitant_medications == 72


def test_a_drug_left_out_of_the_list_does_not_lower_its_confidence(snomed, person):
    with suppress_patient_record_refresh():
        drug(person, 71, 'Oldest drug', 5000, date(2023, 1, 1))     # the sixth newest: not listed
        for index, name in enumerate(['A', 'B', 'C', 'D', 'E']):
            drug(person, 90, f'Drug {name}', 5010 + index, date(2024, 1, 1 + index))

    refresh_patient_record(person)

    assert 'Oldest drug' not in provisional(person).concomitant_medications
    assert marks(person).concomitant_medications == 90


def test_real_beats_provisional_for_medications(snomed, person):
    with suppress_patient_record_refresh():
        drug(person, None, 'Acalabrutinib', 5020, date(2024, 1, 1))
        drug(person, 80, 'Levothyroxine', 5001, date(2024, 6, 1))   # newer, but a guess

    refreshed = refresh_patient_record(person)

    assert refreshed.concomitant_medications == 'Acalabrutinib'
    assert provisional(person).concomitant_medications == 'Acalabrutinib'
    assert marks(person).concomitant_medications is None


def test_a_typed_medication_list_is_not_replaced(snomed, person):
    with suppress_patient_record_refresh():
        drug(person, 80, 'Levothyroxine', 5001, date(2024, 6, 1))
    PatientRecord.objects.filter(person=person).update(user_edited_fields=['concomitant_medications'])

    refresh_patient_record(person)

    assert provisional(person).concomitant_medications is None
    assert marks(person).concomitant_medications is None


def test_an_approved_drug_makes_the_list_real_and_drops_the_mark(snomed, person):
    with suppress_patient_record_refresh():
        row = drug(person, 80, 'Levothyroxine', 5001, date(2024, 6, 1))
    refresh_patient_record(person)
    assert marks(person).concomitant_medications == 80

    DrugExposure.objects.filter(pk=row.pk).update(confidence=None)
    refreshed = refresh_patient_record(person)

    assert refreshed.concomitant_medications == 'Levothyroxine'
    assert marks(person).concomitant_medications is None


# --- the switch and the reader ---------------------------------------------------------

def test_switch_off_computes_and_stores_nothing(snomed, person, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    with suppress_patient_record_refresh():
        cancer(person, 88)
        drug(person, 80, 'Levothyroxine', 5001, date(2024, 6, 1))

    refresh_patient_record(person)

    assert not PatientRecordProvisional.objects.filter(person=person).exists()
    assert not PatientRecordConfidence.objects.filter(person=person).exists()


def test_the_overlay_shows_the_proposals_with_their_confidence(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 82)
        drug(person, 77, 'Levothyroxine', 5001, date(2024, 6, 1))
    refresh_patient_record(person)

    view, shown = provisional_record.overlay(PatientRecord.objects.get(person=person))

    assert 'squamous' in view.disease.lower() and shown['disease'] == 82
    assert view.concomitant_medications == 'Levothyroxine' and shown['concomitant_medications'] == 77


def test_a_failing_reader_costs_only_its_own_fields(snomed, person, monkeypatch):
    with suppress_patient_record_refresh():
        cancer(person, 82)
        drug(person, 77, 'Levothyroxine', 5001, date(2024, 6, 1))

    def boom(*args, **kwargs):
        raise RuntimeError('reader failed')
    monkeypatch.setattr(provisional_record, '_disease_proposals', boom)

    refresh_patient_record(person)

    assert provisional(person).disease is None
    assert provisional(person).concomitant_medications == 'Levothyroxine'


def test_the_real_medication_list_is_unchanged_by_the_shared_helper(snomed, person):
    with suppress_patient_record_refresh():
        for index in range(7):
            drug(person, None, f'Drug {index}', 5100 + index, date(2024, 1, 1 + index))

    refreshed = refresh_patient_record(person)

    assert refreshed.concomitant_medications == 'Drug 6, Drug 5, Drug 4, Drug 3, Drug 2'


# --- review follow-ups -----------------------------------------------------------------

def test_a_database_error_in_one_reader_costs_only_its_own_fields(snomed, person, monkeypatch):
    with suppress_patient_record_refresh():
        cancer(person, 82)
        drug(person, 77, 'Levothyroxine', 5001, date(2024, 6, 1))

    def broken(*args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute('SELECT * FROM a_table_that_does_not_exist')
    monkeypatch.setattr(provisional_record, '_disease_proposals', broken)

    refresh_patient_record(person)

    # The failed statement poisons a transaction without a savepoint; with one the
    # medications are still written.
    assert provisional(person).disease is None
    assert provisional(person).concomitant_medications == 'Levothyroxine'
    assert marks(person).concomitant_medications == 77


def test_a_curated_value_for_the_same_field_wins_over_a_composite_one(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 82)
        drug(person, 77, 'Levothyroxine', 5001, date(2024, 6, 1))
    PatientRecord.objects.filter(person=person).update(disease=None, disease_slug=None)
    record = PatientRecord.objects.get(person=person)

    values, found = provisional_record.composite_proposals(
        person, record, _build_snapshot(person), taken={'disease', 'concomitant_medications'})
    assert 'disease' not in values and 'concomitant_medications' not in values
    assert 'disease_slug' in values and 'diagnosis_date' in values   # only the taken ones are skipped

    values, found = provisional_record.composite_proposals(person, record, _build_snapshot(person))
    assert 'disease' in values and 'concomitant_medications' in values


def test_a_nameless_drug_is_dropped_and_does_not_lower_the_confidence(snomed, person):
    from omop_core.services.patient_record_service import _SENTINEL_CONCEPT_NAME
    nameless = _concept('Drug', 5200, 'RX-5200', _SENTINEL_CONCEPT_NAME)
    with suppress_patient_record_refresh():
        drug(person, 90, 'Levothyroxine', 5001, date(2024, 1, 1))
        DrugExposureFactory(
            person=person, drug_concept=nameless, drug_source_value='',
            drug_exposure_start_date=date(2024, 6, 1), drug_exposure_end_date=date(2024, 6, 1),
            confidence=71)

    refresh_patient_record(person)

    # The newest row has no name: it is dropped, and its 71 is not in the field's.
    assert provisional(person).concomitant_medications == 'Levothyroxine'
    assert marks(person).concomitant_medications == 90


def test_the_status_alone_fills_when_the_record_holds_the_rest(snomed, person):
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4004, 'C4004', 'Multiple myeloma'),
            condition_source_value='C4004', condition_start_date=date(2021, 5, 5),
            condition_status_source_value='remission', confidence=84)
    record = PatientRecord.objects.get(person=person)
    record.disease, record.disease_slug = 'multiple myeloma', 'multiple-myeloma'
    record.diagnosis_date, record.diagnosis_date_precision = date(2020, 1, 1), 'day'
    record.condition_clinical_status = None

    values, found = provisional_record.composite_proposals(person, record, _build_snapshot(person))

    assert values == {'condition_clinical_status': 'remission'} and found == {'condition_clinical_status': 84}
