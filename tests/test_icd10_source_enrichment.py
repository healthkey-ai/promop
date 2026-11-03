"""Tests for ICD-10 source concept resolution and backfill (#1042, #1043).

After migration 0266, all ICD10 SCCM rows are relabeled to ICD10CM, so the
backfill command exits early (ICD10 no longer in VOCAB_TO_UMLS_ROOT).  The
_find_source_concept tests verify direct lookup only — no ICD10→ICD10CM
fallback, because no ICD10 rows exist after the migration.
"""
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

    def test_returns_none_when_no_concept(self, icd10cm_vocab):
        result = _find_source_concept('ICD10CM', 'Z99.99')
        assert result is None

    def test_returns_none_for_empty_vocabulary(self):
        result = _find_source_concept('', 'A00.0')
        assert result is None

    def test_returns_none_for_none_vocabulary(self):
        result = _find_source_concept(None, 'A00.0')
        assert result is None


# ---------------------------------------------------------------------------
# backfill_icd10_source_info tests (#1043)
# After migration 0266, ICD10 is no longer in VOCAB_TO_UMLS_ROOT, so the
# command always exits immediately.
# ---------------------------------------------------------------------------

class TestBackfillICD10SourceInfo:
    def test_command_exits_when_icd10_not_in_umls_root(self):
        out = io.StringIO()
        err = io.StringIO()
        call_command('backfill_icd10_source_info', stdout=out, stderr=err)
        assert 'nothing to do' in err.getvalue().lower()
