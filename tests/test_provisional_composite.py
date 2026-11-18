"""Composite readers in the provisional pass (#2534).

Two readers run over provisional rows only: the disease fields, from the cancer
among the provisional conditions, and ``concomitant_medications``, from the
provisional drug exposures. Each keeps a value only where the real record has no
answer, and carries the lowest confidence among the rows that decide it.
"""
from datetime import date

import pytest

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
        cancer(person, 71, 'Squamous cell carcinoma', 4001, date(2023, 1, 1))
        cancer(person, 93, 'Basal cell carcinoma', 4002, date(2024, 6, 1))

    refresh_patient_record(person)

    # The newer cancer is the primary one, so its 93 is the field's, not the lower 71.
    assert 'basal' in provisional(person).disease.lower()
    assert marks(person).disease == 93
    assert marks(person).diagnosis_date == 93


def test_a_condition_that_is_not_a_cancer_proposes_nothing(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 90, 'Pain', 4003)

    refresh_patient_record(person)

    # Without a cancer the reader's fallbacks would take a symptom's date as the diagnosis.
    assert not PatientRecordProvisional.objects.filter(person=person).exists() \
        or provisional(person).diagnosis_date is None
    assert not PatientRecordConfidence.objects.filter(person=person, diagnosis_date__isnull=False).exists()


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


def test_a_typed_disease_is_not_replaced(snomed, person):
    with suppress_patient_record_refresh():
        cancer(person, 88)
    PatientRecord.objects.filter(person=person).update(user_edited_fields=['disease'])

    refreshed = refresh_patient_record(person)

    assert provisional(person).disease == refreshed.disease     # the typed one, not the proposal
    assert 'squamous' not in provisional(person).disease.lower()
    assert marks(person).disease is None


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
