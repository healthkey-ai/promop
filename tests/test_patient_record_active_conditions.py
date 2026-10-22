"""Regression coverage for hierarchy-backed active-condition projections."""

from datetime import timedelta

import pytest
from django.utils import timezone

from omop_core.models import ConceptAncestor
from omop_core.services.patient_record_service import (
    clear_descendant_cache,
    refresh_patient_record,
)
from tests.factories import (
    ConceptFactory,
    ConditionOccurrenceFactory,
    DomainFactory,
    PatientRecordFactory,
    VocabularyFactory,
)


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear the SNOMED descendant cache before each test so factory-created
    concepts are resolved fresh."""
    clear_descendant_cache()


def _snomed_condition(code: str, name: str, concept_id: int):
    return ConceptFactory(
        concept_id=concept_id,
        concept_code=code,
        concept_name=name,
        vocabulary=VocabularyFactory(
            vocabulary_id='SNOMED', vocabulary_name='SNOMED CT',
        ),
        domain=DomainFactory(domain_id='Condition', domain_name='Condition'),
    )


def _descendant(root, child):
    ConceptAncestor.objects.create(
        ancestor_concept=root,
        descendant_concept=child,
        min_levels_of_separation=1,
        max_levels_of_separation=1,
    )


def test_active_infection_uses_current_snomed_descendants_and_computes_inverse():
    record = PatientRecordFactory()
    root = _snomed_condition('40733004', 'Infectious disease', 8_900_001)
    infection = _snomed_condition('123456789', 'Acute infection', 8_900_002)
    _descendant(root, infection)

    ConditionOccurrenceFactory(
        person=record.person,
        condition_concept=infection,
        condition_start_date=timezone.localdate() - timedelta(days=2),
    )

    refreshed = refresh_patient_record(record.person)

    assert refreshed.active_infection_status is True
    assert refreshed.no_active_infection_status is False


def test_expired_or_resolved_conditions_do_not_count_as_active():
    record = PatientRecordFactory()
    root = _snomed_condition('40733004', 'Infectious disease', 8_900_011)
    infection = _snomed_condition('123456790', 'Past infection', 8_900_012)
    resolved = _snomed_condition('410516002', 'Known resolved', 8_900_013)
    _descendant(root, infection)

    ConditionOccurrenceFactory(
        person=record.person,
        condition_concept=infection,
        condition_start_date=timezone.localdate() - timedelta(days=10),
        condition_end_date=timezone.localdate() - timedelta(days=1),
    )
    ConditionOccurrenceFactory(
        person=record.person,
        condition_concept=infection,
        condition_start_date=timezone.localdate() - timedelta(days=1),
        condition_status_concept=resolved,
    )

    refreshed = refresh_patient_record(record.person)

    assert refreshed.active_infection_status is False
    assert refreshed.no_active_infection_status is True


def test_active_malignancies_are_deduplicated_current_descendant_names():
    record = PatientRecordFactory()
    root = _snomed_condition('363346000', 'Malignant neoplastic disease', 8_900_021)
    breast_cancer = _snomed_condition('254837009', 'Malignant neoplasm of breast', 8_900_022)
    old_cancer = _snomed_condition('860001', 'Old malignancy', 8_900_023)
    _descendant(root, breast_cancer)
    _descendant(root, old_cancer)

    for offset in (4, 2):
        ConditionOccurrenceFactory(
            person=record.person,
            condition_concept=breast_cancer,
            condition_start_date=timezone.localdate() - timedelta(days=offset),
        )
    ConditionOccurrenceFactory(
        person=record.person,
        condition_concept=old_cancer,
        condition_start_date=timezone.localdate() - timedelta(days=30),
        condition_end_date=timezone.localdate() - timedelta(days=1),
    )

    refreshed = refresh_patient_record(record.person)

    assert refreshed.active_malignancies == ['Malignant neoplasm of breast']
    assert refreshed.no_other_active_malignancies is True


def test_active_condition_values_remain_unknown_without_the_snomed_hierarchy():
    record = PatientRecordFactory()

    refreshed = refresh_patient_record(record.person)

    assert refreshed.active_infection_status is None
    assert refreshed.active_malignancies is None
    assert refreshed.no_active_infection_status is None
    assert refreshed.no_other_active_malignancies is None
