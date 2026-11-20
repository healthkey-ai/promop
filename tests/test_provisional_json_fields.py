"""List fields in the provisional pass (#2534).

``disease_slugs``, ``active_malignancies``, ``prior_procedures``,
``genetic_mutations`` and the ``genomics_*`` markers are lists the real record
may already fill. A provisional item is added to the list, never put in its
place: the provisional column holds only the added items, the confidence is the
lowest among them, and the reader lays them after the live items.
"""
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from omop_core.models import (
    ConceptAncestor, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
    ProcedureOccurrence,
)
from omop_core.services import provisional_record
from omop_core.services.patient_record_service import (
    _build_snapshot, clear_descendant_cache, refresh_patient_record,
)
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConditionOccurrenceFactory, MeasurementFactory, PatientRecordFactory,
)
from tests.test_provisional_derivation import _concept, snomed  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''
    clear_descendant_cache()


@pytest.fixture()
def person():
    return PatientRecordFactory().person


def provisional(person):
    return PatientRecordProvisional.objects.get(person=person)


def marks(person):
    return PatientRecordConfidence.objects.get(person=person)


def real(person):
    return PatientRecord.objects.get(person=person)


def procedure(person, name, concept_id, when, confidence=None):
    concept = _concept('Procedure', concept_id, f'P{concept_id}', name)
    type_concept = _concept('Type Concept', 44818, '44818', 'EHR', 'Type Concept')
    return ProcedureOccurrence.objects.create(
        procedure_occurrence_id=next_pk(ProcedureOccurrence, 'procedure_occurrence_id'),
        person=person, procedure_concept=concept, procedure_date=when,
        procedure_type_concept=type_concept, procedure_source_value=f'P{concept_id}',
        confidence=confidence,
    )


def malignancy_root():
    root = _concept('Condition', 8_900_021, '363346000', 'Malignant neoplastic disease')
    clear_descendant_cache()
    return root


def malignancy(person, name, concept_id, confidence, root, days_ago=5):
    concept = _concept('Condition', concept_id, f'M{concept_id}', name)
    ConceptAncestor.objects.get_or_create(
        ancestor_concept=root, descendant_concept=concept,
        defaults=dict(min_levels_of_separation=1, max_levels_of_separation=1))
    clear_descendant_cache()
    return ConditionOccurrenceFactory(
        person=person, condition_concept=concept, condition_source_value=f'M{concept_id}',
        condition_start_date=timezone.localdate() - timedelta(days=days_ago), confidence=confidence)


def variant(person, gene, text, confidence, when='2024-02-01'):
    concept = _concept('Measurement', 4000100, '81252-9', 'Discrete genetic variant', 'LOINC')
    return MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=f'genomics:{gene.lower()}',
        measurement_date=when, value_as_string=text, qualifier_source_value=gene,
        confidence=confidence)


# --- prior_procedures ----------------------------------------------------------------------

def test_a_provisional_procedure_fills_an_empty_list_with_its_confidence(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 84)

    refresh_patient_record(person)

    assert not real(person).prior_procedures                        # approved only
    assert provisional(person).prior_procedures == [
        {'procedure': 'Bone marrow biopsy', 'date': '2024-05-01', 'concept_id': 4101}]
    assert marks(person).prior_procedures == 84


def test_provisional_items_are_added_to_a_list_the_record_already_fills(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Chest X-ray', 4102, date(2024, 1, 1))                 # real
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
        procedure(person, 'PET scan', 4103, date(2024, 6, 1), 72)

    refresh_patient_record(person)

    assert [p['procedure'] for p in real(person).prior_procedures] == ['Chest X-ray']
    # Only the added items are stored, and the confidence is the lowest of them.
    assert {p['procedure'] for p in provisional(person).prior_procedures} == {
        'Bone marrow biopsy', 'PET scan'}
    assert marks(person).prior_procedures == 72
    shown, shown_marks = provisional_record.overlay(real(person))
    assert [p['procedure'] for p in shown.prior_procedures][0] == 'Chest X-ray'   # real kept, first
    assert {p['procedure'] for p in shown.prior_procedures} == {
        'Chest X-ray', 'Bone marrow biopsy', 'PET scan'}
    assert shown_marks['prior_procedures'] == 72
    assert {p['procedure'] for p in shown.provisional_items['prior_procedures']} == {
        'Bone marrow biopsy', 'PET scan'}
    # The stored record is untouched.
    assert [p['procedure'] for p in real(person).prior_procedures] == ['Chest X-ray']


def test_an_item_the_record_already_holds_is_not_added_twice(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1))          # real
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 80)      # same, proposed

    refresh_patient_record(person)

    assert marks(person).prior_procedures is None
    shown, shown_marks = provisional_record.overlay(real(person))
    assert len(shown.prior_procedures) == 1 and 'prior_procedures' not in shown_marks
    assert not hasattr(shown, 'provisional_items')


def test_the_same_proposed_item_from_two_rows_keeps_the_lowest_confidence(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 95)
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 76)

    refresh_patient_record(person)

    assert len(provisional(person).prior_procedures) == 1
    assert marks(person).prior_procedures == 76


def test_a_list_a_user_edited_is_never_added_to(snomed, person):
    PatientRecord.objects.filter(person=person).update(
        prior_procedures=[{'procedure': 'Typed', 'date': None, 'concept_id': 0}],
        user_edited_fields=['prior_procedures'])
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)

    refresh_patient_record(person)

    assert marks(person).prior_procedures is None
    shown, shown_marks = provisional_record.overlay(real(person))
    assert [p['procedure'] for p in shown.prior_procedures] == ['Typed'] and shown_marks == {}


def test_approving_or_reverting_a_proposal_takes_its_item_back_out(snomed, person):
    with suppress_patient_record_refresh():
        row = procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)
    assert marks(person).prior_procedures == 90

    ProcedureOccurrence.objects.filter(pk=row.pk).update(confidence=None)        # approved
    refresh_patient_record(person)

    assert marks(person).prior_procedures is None
    assert [p['procedure'] for p in real(person).prior_procedures] == ['Bone marrow biopsy']
    shown, shown_marks = provisional_record.overlay(real(person))
    assert shown_marks == {} and len(shown.prior_procedures) == 1


def test_a_reverted_proposal_leaves_no_item(snomed, person):
    with suppress_patient_record_refresh():
        row = procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)

    ProcedureOccurrence.objects.filter(pk=row.pk).update(
        confidence=None, procedure_concept=_concept('Procedure', 0, '0', 'No matching concept'))
    refresh_patient_record(person)

    assert marks(person).prior_procedures is None
    shown, shown_marks = provisional_record.overlay(real(person))
    assert shown_marks == {}


def test_a_stored_item_the_record_has_since_gained_stops_being_provisional(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)
    # A later approved write puts the same item on the record before the next pass.
    PatientRecord.objects.filter(person=person).update(prior_procedures=[
        {'procedure': 'Bone marrow biopsy', 'date': '2024-05-01', 'concept_id': 4101}])

    shown, shown_marks = provisional_record.overlay(real(person))

    assert shown_marks == {} and len(shown.prior_procedures) == 1


def test_overlay_over_an_overlay_adds_nothing(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Chest X-ray', 4102, date(2024, 1, 1))
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)

    once, _ = provisional_record.overlay(real(person))
    twice, again = provisional_record.overlay(once)

    assert twice.prior_procedures == once.prior_procedures and again == {}
    assert twice.provisional_items == once.provisional_items      # the serializer still sees them


def test_the_flag_off_computes_stores_and_shows_nothing(snomed, person, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)

    refresh_patient_record(person)

    assert not PatientRecordProvisional.objects.filter(person=person).exists()
    shown, shown_marks = provisional_record.overlay(real(person))
    assert shown_marks == {} and not shown.prior_procedures


# --- a user's edit of a list ---------------------------------------------------------------

def test_editing_the_list_makes_it_the_users_and_drops_the_proposal(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)

    PatientRecord.objects.filter(person=person).update(
        prior_procedures=[{'procedure': 'Typed', 'date': None, 'concept_id': 0}],
        user_edited_fields=['prior_procedures'])
    provisional_record.mirror_user_edit(real(person), ['prior_procedures'])

    assert marks(person).prior_procedures is None
    assert provisional(person).prior_procedures == [{'procedure': 'Typed', 'date': None, 'concept_id': 0}]


def test_editing_another_field_leaves_a_list_proposal_alone(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)
    PatientRecord.objects.filter(person=person).update(
        prior_procedures=[{'procedure': 'Chest X-ray', 'date': None, 'concept_id': 4102}])

    provisional_record.mirror_user_edit(real(person), ['disease'])

    assert marks(person).prior_procedures == 90
    shown, shown_marks = provisional_record.overlay(real(person))
    assert [p['procedure'] for p in shown.prior_procedures] == ['Chest X-ray', 'Bone marrow biopsy']


# --- active_malignancies and disease_slugs -------------------------------------------------

def test_provisional_malignancies_are_added_by_the_real_current_condition_rule(snomed, person):
    root = malignancy_root()
    with suppress_patient_record_refresh():
        malignancy(person, 'Malignant neoplasm of breast', 8_900_022, 88, root)
        malignancy(person, 'Malignant neoplasm of lung', 8_900_023, 73, root)
        # Ended: not current, so the real rule drops it and so does the pass.
        old = malignancy(person, 'Old malignancy', 8_900_024, 99, root, days_ago=40)
        type(old).objects.filter(pk=old.pk).update(
            condition_end_date=timezone.localdate() - timedelta(days=1))

    refresh_patient_record(person)

    assert real(person).active_malignancies == []
    assert provisional(person).active_malignancies == [
        'Malignant neoplasm of breast', 'Malignant neoplasm of lung']
    assert marks(person).active_malignancies == 73


def test_a_real_malignancy_stays_and_a_proposed_one_joins_it(snomed, person):
    root = malignancy_root()
    with suppress_patient_record_refresh():
        malignancy(person, 'Malignant neoplasm of breast', 8_900_022, None, root)
        malignancy(person, 'Malignant neoplasm of lung', 8_900_023, 81, root)

    refresh_patient_record(person)

    assert real(person).active_malignancies == ['Malignant neoplasm of breast']
    shown, shown_marks = provisional_record.overlay(real(person))
    assert shown.active_malignancies == ['Malignant neoplasm of breast', 'Malignant neoplasm of lung']
    assert shown_marks['active_malignancies'] == 81
    assert shown.provisional_items['active_malignancies'] == ['Malignant neoplasm of lung']


def test_a_second_provisional_cancer_adds_its_slug_beside_the_real_disease(snomed, person):
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4001, 'C4001', 'Squamous cell carcinoma'),
            condition_source_value='C4001', condition_start_date=date(2023, 1, 1))     # real
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4002, 'C4002', 'Basal cell carcinoma'),
            condition_source_value='C4002', condition_start_date=date(2024, 1, 1), confidence=77)

    refresh_patient_record(person)

    assert real(person).disease_slugs == ['squamous-cell-carcinoma']
    assert provisional(person).disease_slugs == ['basal-cell-carcinoma']
    assert marks(person).disease_slugs == 77
    shown, _ = provisional_record.overlay(real(person))
    assert shown.disease_slugs == ['squamous-cell-carcinoma', 'basal-cell-carcinoma']


def test_the_slug_confidence_is_the_one_of_the_cancer_that_has_it(snomed, person):
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4001, 'C4001', 'Squamous cell carcinoma'),
            condition_source_value='C4001', condition_start_date=date(2024, 6, 1), confidence=95)
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4002, 'C4002', 'Basal cell carcinoma'),
            condition_source_value='C4002', condition_start_date=date(2023, 1, 1), confidence=71)

    refresh_patient_record(person)

    assert set(provisional(person).disease_slugs) == {'squamous-cell-carcinoma', 'basal-cell-carcinoma'}
    assert marks(person).disease_slugs == 71          # the lowest of the added slugs


def test_a_typed_disease_slugs_list_is_not_added_to(snomed, person):
    PatientRecord.objects.filter(person=person).update(
        disease_slugs=['multiple-myeloma'], user_edited_fields=['disease_slugs'])
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=person, condition_concept=_concept('Condition', 4001, 'C4001', 'Squamous cell carcinoma'),
            condition_source_value='C4001', condition_start_date=date(2024, 1, 1), confidence=90)

    refresh_patient_record(person)

    assert marks(person).disease_slugs is None
    shown, _ = provisional_record.overlay(real(person))
    assert shown.disease_slugs == ['multiple-myeloma']


# --- genetic_mutations and the genomics markers --------------------------------------------

def test_a_provisional_variant_is_added_to_genetic_mutations_and_its_marker(snomed, person):
    from django.core.management import call_command
    call_command('seed_genomics_catalog')
    with suppress_patient_record_refresh():
        row = variant(person, 'BRCA1', 'c.68_69delAG', 86)

    refresh_patient_record(person)

    stored = provisional(person).genetic_mutations
    assert [v['gene'].lower() for v in stored] == ['brca1']
    assert stored[0]['id'] == row.pk and stored[0]['variant'] == 'c.68_69delAG'
    assert marks(person).genetic_mutations == 86
    assert not real(person).genetic_mutations
    assert provisional(person).genomics_brca1 and marks(person).genomics_brca1 == 86


def test_a_variant_the_record_already_holds_is_not_added_again(snomed, person):
    from django.core.management import call_command
    call_command('seed_genomics_catalog')
    with suppress_patient_record_refresh():
        variant(person, 'BRCA1', 'c.68_69delAG', None)                 # real
        variant(person, 'BRCA1', 'c.68_69delAG', 80)                   # same finding, proposed
        variant(person, 'TP53', 'c.743G>A', 78)

    refresh_patient_record(person)

    assert len(real(person).genetic_mutations) == 1
    assert [v['variant'] for v in provisional(person).genetic_mutations] == ['c.743G>A']
    assert marks(person).genetic_mutations == 78
    shown, _ = provisional_record.overlay(real(person))
    assert {v['variant'] for v in shown.genetic_mutations} == {'c.68_69delAG', 'c.743G>A'}
    assert shown.genetic_mutations[0]['variant'] == 'c.68_69delAG'        # real first


def test_a_marker_derived_from_proposed_parents_is_not_offered(snomed, person, monkeypatch):
    from django.core.management import call_command
    from omop_core.services import genomics_catalog
    call_command('seed_genomics_catalog')
    monkeypatch.setitem(
        genomics_catalog._DERIVATION_FUNCTIONS, 'complex_karyotype',
        lambda variants: genomics_catalog.DerivedFinding(
            finding={'gene': 'x', 'variant': 'derived'}, derivation_version='test-1'))
    with suppress_patient_record_refresh():
        variant(person, 'BRCA1', 'c.68_69delAG', 86)
    snapshot = _build_snapshot(person)

    candidates = provisional_record._genomic_candidates(person, snapshot)

    assert 'genomics_complex_karyotype' not in candidates         # the reader derived one; it is dropped
    assert 'genomics_brca1' in candidates
    assert all(item.get('provenance') != 'derived' for found in candidates.values() for item, _ in found)


# --- shared behaviour ----------------------------------------------------------------------

def test_a_reader_that_raises_costs_only_its_own_fields(snomed, person, monkeypatch):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
        malignancy(person, 'Malignant neoplasm of breast', 8_900_022, 88, malignancy_root())

    def boom(person, snapshot):
        raise RuntimeError('bad reader')
    monkeypatch.setattr(provisional_record, '_LIST_READERS',
                        (boom, provisional_record._malignancy_candidates))

    refresh_patient_record(person)

    assert provisional(person).active_malignancies == ['Malignant neoplasm of breast']
    assert marks(person).prior_procedures is None


def test_only_the_covered_json_fields_have_a_confidence_column():
    from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
    columns = {f.name for f in PatientRecordConfidence._meta.concrete_fields}
    assert set(PROVISIONAL_JSON_FIELDS) <= columns
    # Left out: they come from persisted Episodes and contexts, or curator-written keys.
    for name in ('later_therapies', 'therapy_component_ids', 'medication_contexts',
                 'stem_cell_transplant_history', 'sct_eligibility'):
        assert name not in columns


def test_a_list_proposal_costs_no_query_per_item(snomed, person):
    with suppress_patient_record_refresh():
        for i in range(30):
            procedure(person, f'Procedure {i}', 4200 + i, date(2024, 1, 1) + timedelta(days=i), 80)
    snapshot = _build_snapshot(person)
    record = real(person)
    with CaptureQueriesContext(connection) as few:
        provisional_record.list_proposals(person, record, snapshot)
    with suppress_patient_record_refresh():
        for i in range(30, 60):
            procedure(person, f'Procedure {i}', 4200 + i, date(2024, 1, 1) + timedelta(days=i), 80)
    snapshot = _build_snapshot(person)
    with CaptureQueriesContext(connection) as many:
        provisional_record.list_proposals(person, record, snapshot)

    assert len(many) <= len(few) + 2


def _staff():
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='lists@example.test', password='pw', is_staff=True))
    return client


def test_the_record_read_carries_the_merged_list_its_mark_and_which_items_are_proposals(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Chest X-ray', 4102, date(2024, 1, 1))
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)

    body = _staff().get(f'/api/v1/patient-records/{person.person_id}/').json()['patient_info']

    assert {p['procedure'] for p in body['prior_procedures']} == {'Chest X-ray', 'Bone marrow biopsy'}
    assert body['provisional_fields']['prior_procedures'] == 90
    assert [p['procedure'] for p in body['provisional_items']['prior_procedures']] == ['Bone marrow biopsy']
    assert [p['procedure'] for p in real(person).prior_procedures] == ['Chest X-ray']   # a view, never a write


def test_a_record_with_no_proposed_items_carries_no_item_list(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Chest X-ray', 4102, date(2024, 1, 1))
    refresh_patient_record(person)

    body = _staff().get(f'/api/v1/patient-records/{person.person_id}/').json()['patient_info']

    assert 'provisional_items' not in body and body['provisional_fields'] == {}


def test_the_patient_list_carries_the_same(snomed, person):
    with suppress_patient_record_refresh():
        procedure(person, 'Bone marrow biopsy', 4101, date(2024, 5, 1), 90)
    refresh_patient_record(person)

    rows = _staff().get('/api/v1/patient-records/').json()
    rows = rows['results'] if isinstance(rows, dict) else rows
    row = next(r for r in rows if r['person_id'] == person.person_id)

    assert row['provisional_fields']['prior_procedures'] == 90
    assert [p['procedure'] for p in row['provisional_items']['prior_procedures']] == ['Bone marrow biopsy']
