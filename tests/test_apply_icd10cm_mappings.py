"""Tests for the apply_icd10cm_mappings management command (#1028)."""
import io

import pytest
from django.core.management import call_command

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture()
def condition_domain():
    return DomainFactory(domain_id='Condition', domain_name='Condition')


@pytest.fixture()
def snomed_vocab():
    return VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')


@pytest.fixture()
def target_concept(condition_domain, snomed_vocab):
    return ConceptFactory(
        concept_name='Cholera',
        concept_code='63650001',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )


@pytest.fixture()
def approved_icd10cm(target_concept):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10CM',
        source_code='A00.0',
        source_code_description='Cholera due to Vibrio cholerae',
        target_concept=target_concept,
        destination_vocabulary_id='SNOMED',
        domain_id='Condition',
        omop_table='condition',
        status='approved',
        origin='import',
        origin_system='athena',
    )


@pytest.fixture()
def proposed_icd10(approved_icd10cm):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10',
        source_code='A00.0',
        source_code_description='Cholera',
        target_concept=None,
        domain_id='Condition',
        omop_table='condition',
        status='proposed',
        origin='import',
        origin_system='hk-one',
    )


class TestApplyICD10CMMappings:
    def test_dry_run_does_not_write(self, approved_icd10cm, proposed_icd10):
        out = io.StringIO()
        call_command('apply_icd10cm_mappings', '--dry-run', stdout=out)
        assert 'DRY RUN' in out.getvalue()
        proposed_icd10.refresh_from_db()
        assert proposed_icd10.status == 'proposed'
        assert proposed_icd10.target_concept is None

    def test_approves_matching_proposed_rows(self, approved_icd10cm, proposed_icd10, target_concept):
        out = io.StringIO()
        call_command('apply_icd10cm_mappings', stdout=out)
        assert 'Approved 1' in out.getvalue()
        proposed_icd10.refresh_from_db()
        assert proposed_icd10.status == 'approved'
        assert proposed_icd10.target_concept == target_concept
        assert proposed_icd10.destination_vocabulary_id == 'SNOMED'
        assert proposed_icd10.reviewed_at is not None
        assert proposed_icd10.reviewer is not None
        assert proposed_icd10.reviewer.name == 'system'
        assert 'auto-approved from ICD10CM' in proposed_icd10.origin_system

    def test_skips_already_approved_icd10_rows(self, approved_icd10cm, target_concept):
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='ICD10',
            source_code='A00.0',
            target_concept=target_concept,
            domain_id='Condition',
            omop_table='condition',
            status='approved',
            origin='curator',
        )
        out = io.StringIO()
        call_command('apply_icd10cm_mappings', stdout=out)
        assert 'Nothing to do' in out.getvalue()

    def test_nothing_to_do_when_no_overlap(self):
        out = io.StringIO()
        call_command('apply_icd10cm_mappings', stdout=out)
        assert 'Nothing to do' in out.getvalue()

    def test_origin_system_truncated_to_max_length(self, approved_icd10cm):
        long_origin = 'x' * 50
        proposed = SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='ICD10',
            source_code='A00.0',
            domain_id='Condition',
            omop_table='condition',
            status='proposed',
            origin='import',
            origin_system=long_origin,
        )
        call_command('apply_icd10cm_mappings', stdout=io.StringIO())
        proposed.refresh_from_db()
        assert len(proposed.origin_system) <= 50
        assert proposed.status == 'approved'

