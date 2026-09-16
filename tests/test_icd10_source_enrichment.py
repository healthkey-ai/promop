"""Tests for ICD-10 source concept resolution and backfill (#1042, #1043)."""
import io

import pytest
from django.core.management import call_command

from omop_core.mapping.suggestions import _find_source_concept
from omop_core.models import Concept, SourceCodeConceptMapping, UmlsSourceCode
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture()
def condition_domain():
    return DomainFactory(domain_id='Condition', domain_name='Condition')


@pytest.fixture()
def icd10cm_vocab():
    return VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD-10-CM')


@pytest.fixture()
def icd10_vocab():
    return VocabularyFactory(vocabulary_id='ICD10', vocabulary_name='ICD-10')


@pytest.fixture()
def icd10cm_concept(condition_domain, icd10cm_vocab):
    return ConceptFactory(
        concept_name='Cholera due to Vibrio cholerae 01, biovar cholerae',
        concept_code='A00.0',
        vocabulary=icd10cm_vocab,
        domain=condition_domain,
        standard_concept='',
    )


# ---------------------------------------------------------------------------
# _find_source_concept tests (#1042)
# ---------------------------------------------------------------------------

class TestFindSourceConcept:
    def test_finds_concept_in_own_vocabulary(self, icd10cm_concept, icd10cm_vocab):
        result = _find_source_concept('ICD10CM', 'A00.0')
        assert result == icd10cm_concept

    def test_falls_back_to_icd10cm_for_icd10(self, icd10cm_concept, icd10_vocab):
        """ICD10 code with no ICD10 concept falls back to ICD10CM."""
        result = _find_source_concept('ICD10', 'A00.0')
        assert result == icd10cm_concept
        assert result.vocabulary_id == 'ICD10CM'

    def test_returns_none_when_no_concept(self, icd10_vocab):
        result = _find_source_concept('ICD10', 'Z99.99')
        assert result is None

    def test_returns_none_for_empty_vocabulary(self):
        result = _find_source_concept('', 'A00.0')
        assert result is None

    def test_returns_none_for_none_vocabulary(self):
        result = _find_source_concept(None, 'A00.0')
        assert result is None

    def test_prefers_own_vocabulary_over_fallback(self, condition_domain, icd10_vocab, icd10cm_concept):
        """If ICD10 has its own concept, use it instead of falling back."""
        icd10_concept = ConceptFactory(
            concept_name='Cholera',
            concept_code='A00.0',
            vocabulary=icd10_vocab,
            domain=condition_domain,
            standard_concept='',
        )
        result = _find_source_concept('ICD10', 'A00.0')
        assert result == icd10_concept


# ---------------------------------------------------------------------------
# backfill_icd10_source_info tests (#1043)
# ---------------------------------------------------------------------------

@pytest.fixture()
def umls_release():
    from omop_core.models import UmlsRelease
    return UmlsRelease.objects.create(release_version='2024AA')


@pytest.fixture()
def umls_data(umls_release, icd10cm_concept):
    from omop_core.models import UmlsConcept
    cui = UmlsConcept.objects.create(
        cui='C0008031', preferred_name='Cholera', release=umls_release,
    )
    UmlsSourceCode.objects.create(
        concept=cui, root_source='ICD10CM', code='A00.0',
        term_type='PT', name='Cholera due to Vibrio cholerae 01, biovar cholerae',
        is_preferred=True,
    )
    return cui


class TestBackfillICD10SourceInfo:
    def test_dry_run_does_not_write(self, umls_data, icd10cm_concept):
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='ICD10', source_code='A00.0',
            domain_id='Condition', omop_table='condition',
            status='proposed', origin='import',
        )
        out = io.StringIO()
        call_command('backfill_icd10_source_info', '--dry-run', stdout=out)
        assert 'DRY RUN' in out.getvalue()
        row = SourceCodeConceptMapping.objects.get(
            source_vocabulary_id='ICD10', source_code='A00.0',
        )
        assert row.source_code_description == ''
        assert row.source_concept is None

    def test_fills_all_three_fields(self, umls_data, icd10cm_concept):
        row = SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='ICD10', source_code='A00.0',
            domain_id='Condition', omop_table='condition',
            status='proposed', origin='import',
        )
        out = io.StringIO()
        call_command('backfill_icd10_source_info', stdout=out)
        assert 'Updated 1' in out.getvalue()
        row.refresh_from_db()
        assert row.source_code_description == 'Cholera due to Vibrio cholerae 01, biovar cholerae'
        assert row.source_concept == icd10cm_concept
        assert 'Cholera' in row.umls_source_name

    def test_skips_already_enriched_rows(self, umls_data, icd10cm_concept):
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='ICD10', source_code='A00.0',
            source_code_description='Already set',
            source_concept=icd10cm_concept,
            umls_source_name='Already set',
            domain_id='Condition', omop_table='condition',
            status='proposed', origin='import',
        )
        out = io.StringIO()
        call_command('backfill_icd10_source_info', stdout=out)
        assert 'Nothing to do' in out.getvalue()

    def test_nothing_to_do_when_no_icd10_rows(self):
        out = io.StringIO()
        call_command('backfill_icd10_source_info', stdout=out)
        assert 'Nothing to do' in out.getvalue()
