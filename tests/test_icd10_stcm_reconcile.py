"""Reconcile hand-mapped ICD-10 SCCM rows against Athena's live STCM table."""
from datetime import date

import pytest
from django.apps import apps as live_apps
from django.db import connection

from omop_core.data_migrations.athena_icd10_stcm_reconcile import reconcile, summarize
from omop_core.models import (
    Concept, SourceCodeConceptMapping as Mapping, SourceToConceptMap as STCM,
)
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory


def _stcm(source_code, source_vocab, target_concept, target_vocab='SNOMED'):
    return STCM.objects.create(
        source_code=source_code,
        source_concept=target_concept,  # not critical for this test
        source_vocabulary_id=source_vocab,
        target_concept=target_concept,
        target_vocabulary_id=target_vocab,
        valid_start_date=date(2000, 1, 1),
        valid_end_date=date(2099, 12, 31),
    )


def _mapping(source_code, source_vocab, target_concept, origin_system='HT-One',
             status='approved'):
    return Mapping.objects.create(
        source_code=source_code,
        source_vocabulary_id=source_vocab,
        target_concept=target_concept,
        destination_vocabulary_id=target_concept.vocabulary_id,
        domain_id=target_concept.domain_id,
        omop_table='condition',
        status=status,
        origin='import',
        origin_system=origin_system,
        source='HealthKey',
    )


@pytest.fixture
def condition_domain():
    return DomainFactory(domain_id='Condition', domain_name='Condition')


@pytest.fixture
def snomed_vocab():
    return VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED CT')


@pytest.fixture
def old_target(condition_domain, snomed_vocab):
    """A hand-mapped destination concept."""
    return ConceptFactory(
        concept_id=5001,
        concept_code='OLD-1',
        concept_name='Old Target',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )


@pytest.fixture
def athena_target(condition_domain, snomed_vocab):
    """Athena's destination concept (different from old_target)."""
    return ConceptFactory(
        concept_id=5002,
        concept_code='ATHENA-1',
        concept_name='Athena Target',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )


@pytest.mark.django_db
def test_destination_changed(old_target, athena_target):
    """When Athena disagrees with hand-mapped destination, update it."""
    row = _mapping('A01.0', 'ICD10CM', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'destination_changed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.concept_id
    assert row.origin_system == 'athena'
    assert row.source == 'Athena'
    assert row.destination_vocabulary_id == 'SNOMED'
    assert row.domain_id == 'Condition'
    assert row.reviewed_at is not None
    assert 'destination changed' in row.notes


@pytest.mark.django_db
def test_destination_confirmed(old_target):
    """When Athena agrees with hand-mapped destination, re-attribute only."""
    row = _mapping('B02.0', 'ICD10CM', old_target)
    _stcm('B02.0', 'ICD10CM', old_target)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'reattributed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'athena'
    assert 'destination confirmed' in row.notes


@pytest.mark.django_db
def test_no_stcm_match(old_target):
    """When Athena has no mapping for the code, leave unchanged."""
    row = _mapping('Z99.9', 'ICD10CM', old_target)
    # No STCM row created

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'HT-One'  # unchanged


@pytest.mark.django_db
def test_multiple_targets_skipped(old_target, athena_target, condition_domain, snomed_vocab):
    """When Athena has multiple valid targets, leave the row unchanged."""
    third_target = ConceptFactory(
        concept_id=5003,
        concept_code='ATHENA-2',
        concept_name='Another Athena Target',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )
    row = _mapping('C10.0', 'ICD10CM', old_target)
    _stcm('C10.0', 'ICD10CM', athena_target)
    _stcm('C10.0', 'ICD10CM', third_target)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'multiple_targets': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'HT-One'  # unchanged


@pytest.mark.django_db
def test_cross_vocabulary_fallback(old_target, athena_target):
    """SCCM is ICD10CM but STCM only has ICD10 — still matches."""
    row = _mapping('D10.0', 'ICD10CM', old_target)
    _stcm('D10.0', 'ICD10', athena_target)  # different vocab in STCM

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'destination_changed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.concept_id
    assert row.origin_system == 'athena'


@pytest.mark.django_db
def test_already_athena_skipped(old_target):
    """Rows already origin_system='athena' are not touched."""
    row = _mapping('E10.0', 'ICD10CM', old_target, origin_system='athena')
    _stcm('E10.0', 'ICD10CM', old_target)

    receipts = reconcile(live_apps, connection)

    assert len(receipts) == 0
    row.refresh_from_db()
    assert row.origin_system == 'athena'  # unchanged


@pytest.mark.django_db
def test_idempotent(old_target, athena_target):
    """Second run finds zero eligible rows."""
    _mapping('F10.0', 'ICD10CM', old_target)
    _stcm('F10.0', 'ICD10CM', athena_target)

    first = reconcile(live_apps, connection)
    assert summarize(first) == {'destination_changed': 1}

    second = reconcile(live_apps, connection)
    assert summarize(second) == {}


@pytest.mark.django_db
def test_nonstandard_stcm_target_ignored(old_target, condition_domain, snomed_vocab):
    """STCM target that is not standard_concept='S' is filtered out."""
    nonstandard = ConceptFactory(
        concept_id=5004,
        concept_code='NS-1',
        concept_name='Non-Standard',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='C',  # not standard
    )
    row = _mapping('G10.0', 'ICD10CM', old_target)
    _stcm('G10.0', 'ICD10CM', nonstandard)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id


@pytest.mark.django_db
def test_proposed_rows_not_eligible(old_target, athena_target):
    """Only approved rows are eligible — proposed rows are skipped."""
    row = _mapping('H10.0', 'ICD10CM', old_target, status='proposed')
    _stcm('H10.0', 'ICD10CM', athena_target)

    receipts = reconcile(live_apps, connection)

    assert len(receipts) == 0
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'  # unchanged


@pytest.mark.django_db
def test_case_insensitive_matching(old_target, athena_target):
    """Source codes match case-insensitively."""
    row = _mapping('j10.0', 'ICD10CM', old_target)  # lowercase
    _stcm('J10.0', 'ICD10CM', athena_target)  # uppercase in STCM

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'destination_changed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.concept_id
