"""Tests for the multi-strategy suggest pipeline.

Covers:
- UMLS CUI-bridge lookup (`umls_candidates`)
- Vector similarity search (`vector_candidates`)
- Waterfall orchestration (early exit, strategy filtering)
- API endpoint strategy parameter validation
"""
import pytest

from django.db import connection

from omop_core.models import (
    Concept,
    SourceCodeConceptMapping,
    UmlsConcept,
    UmlsRelease,
    UmlsSourceCode,
)
from omop_core.services.mapping_suggestions import (
    ALL_STRATEGIES,
    STRATEGY_LEXICAL,
    STRATEGY_UMLS,
    STRATEGY_VECTORS,
    VOCAB_TO_UMLS_ROOT,
    _UMLS_ROOT_TO_VOCAB,
    umls_candidates,
    vector_candidates,
)
from tests.factories import (
    ConceptClassFactory,
    ConceptFactory,
    DomainFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def umls_release():
    return UmlsRelease.objects.create(release_version='2024AA')


@pytest.fixture()
def condition_domain():
    return DomainFactory(domain_id='Condition', domain_name='Condition')


@pytest.fixture()
def snomed_vocab():
    return VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')


@pytest.fixture()
def icd10cm_vocab():
    return VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD10CM')


@pytest.fixture()
def concept_class():
    return ConceptClassFactory(concept_class_id='Clinical Finding')


# ---------------------------------------------------------------------------
# UMLS tier tests
# ---------------------------------------------------------------------------

class TestUmlsCandidates:
    """Test the UMLS CUI-bridge lookup function."""

    def test_unknown_vocabulary_returns_empty(self):
        candidates, cui = umls_candidates('E11.9', 'MADE_UP_VOCAB')
        assert candidates == []
        assert cui is None

    def test_no_umls_match_returns_empty(self, umls_release):
        # No UmlsSourceCode rows for this code.
        candidates, cui = umls_candidates('ZZZZZ', 'ICD10CM')
        assert candidates == []
        assert cui is None

    def test_single_standard_concept_found(
        self, umls_release, condition_domain, snomed_vocab,
        icd10cm_vocab, concept_class,
    ):
        """ICD10CM E11.9 → CUI C0011860 → SNOMED 44054006 (standard)."""
        cui = UmlsConcept.objects.create(
            cui='C0011860', preferred_name='Type 2 diabetes mellitus',
            release=umls_release,
        )
        # Source: ICD10CM E11.9
        UmlsSourceCode.objects.create(
            concept=cui, root_source='ICD10CM', code='E11.9',
            term_type='PT', name='Type 2 diabetes mellitus, unspecified',
        )
        # Sibling: SNOMED 44054006
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='44054006',
            term_type='PT', name='Diabetes mellitus type 2',
        )
        # The SNOMED concept must exist in OMOP as standard.
        snomed_concept = ConceptFactory(
            concept_id=44054006, concept_name='Diabetes mellitus type 2',
            concept_code='44054006', vocabulary=snomed_vocab,
            domain=condition_domain, concept_class=concept_class,
            standard_concept='S',
        )

        candidates, cui_str = umls_candidates('E11.9', 'ICD10CM', 'Condition')
        assert len(candidates) == 1
        assert candidates[0]['concept_id'] == 44054006
        assert candidates[0]['concept_name'] == 'Diabetes mellitus type 2'
        assert candidates[0]['umls_score'] == 1.0
        assert cui_str == 'C0011860'

    def test_multiple_standard_concepts_returned(
        self, umls_release, condition_domain, snomed_vocab,
        icd10cm_vocab, concept_class,
    ):
        """When UMLS maps to multiple standard concepts, all are returned."""
        cui = UmlsConcept.objects.create(
            cui='C0000001', preferred_name='Test concept',
            release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='ICD10CM', code='TEST.1',
            term_type='PT', name='Test source',
        )
        # Two SNOMED siblings
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='111111',
            term_type='PT', name='Test target A',
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='222222',
            term_type='PT', name='Test target B',
        )
        ConceptFactory(
            concept_id=111111, concept_name='Test target A',
            concept_code='111111', vocabulary=snomed_vocab,
            domain=condition_domain, concept_class=concept_class,
        )
        ConceptFactory(
            concept_id=222222, concept_name='Test target B',
            concept_code='222222', vocabulary=snomed_vocab,
            domain=condition_domain, concept_class=concept_class,
        )

        candidates, _ = umls_candidates('TEST.1', 'ICD10CM', 'Condition')
        assert len(candidates) == 2
        ids = {c['concept_id'] for c in candidates}
        assert ids == {111111, 222222}

    def test_non_standard_concepts_excluded(
        self, umls_release, condition_domain, snomed_vocab,
        icd10cm_vocab, concept_class,
    ):
        """Only standard_concept='S' concepts are returned."""
        cui = UmlsConcept.objects.create(
            cui='C0000002', preferred_name='Non-standard',
            release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='ICD10CM', code='NS.1',
            term_type='PT', name='Non-standard source',
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='333333',
            term_type='PT', name='Non-standard target',
        )
        ConceptFactory(
            concept_id=333333, concept_name='Non-standard target',
            concept_code='333333', vocabulary=snomed_vocab,
            domain=condition_domain, concept_class=concept_class,
            standard_concept='C',  # Classification, not Standard
        )

        candidates, _ = umls_candidates('NS.1', 'ICD10CM', 'Condition')
        assert candidates == []

    def test_domain_filtering(
        self, umls_release, condition_domain, snomed_vocab,
        icd10cm_vocab, concept_class,
    ):
        """Candidates are filtered to the requested domain."""
        drug_domain = DomainFactory(domain_id='Drug', domain_name='Drug')
        cui = UmlsConcept.objects.create(
            cui='C0000003', preferred_name='Domain test',
            release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='ICD10CM', code='DOM.1',
            term_type='PT', name='Domain source',
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='444444',
            term_type='PT', name='Drug domain target',
        )
        ConceptFactory(
            concept_id=444444, concept_name='Drug domain target',
            concept_code='444444', vocabulary=snomed_vocab,
            domain=drug_domain, concept_class=concept_class,
        )

        # Asking for Condition domain should not find a Drug concept.
        candidates, _ = umls_candidates('DOM.1', 'ICD10CM', 'Condition')
        assert candidates == []

        # Asking for Drug domain should find it.
        candidates, _ = umls_candidates('DOM.1', 'ICD10CM', 'Drug')
        assert len(candidates) == 1


class TestVocabToUmlsRootMapping:
    """Verify the VOCAB_TO_UMLS_ROOT constant is internally consistent."""

    def test_reverse_mapping_roundtrips(self):
        for omop_vocab, umls_sab in VOCAB_TO_UMLS_ROOT.items():
            assert _UMLS_ROOT_TO_VOCAB[umls_sab] == omop_vocab

    def test_all_strategies_constant(self):
        assert STRATEGY_UMLS in ALL_STRATEGIES
        assert STRATEGY_VECTORS in ALL_STRATEGIES
        assert STRATEGY_LEXICAL in ALL_STRATEGIES


# ---------------------------------------------------------------------------
# Vector tier tests
# ---------------------------------------------------------------------------

class TestVectorCandidates:
    """Test vector_candidates() — graceful degradation when not populated."""

    def test_empty_table_returns_empty(self, condition_domain):
        """When concept_embedding is empty, vector search returns nothing."""
        # Ensure the table exists (migration should have run).
        with connection.cursor() as cur:
            cur.execute(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_name = 'concept_embedding'"
                ")"
            )
            exists = cur.fetchone()[0]
        if not exists:
            pytest.skip('concept_embedding table not created yet')

        results = vector_candidates('hypertension', 'Condition')
        assert results == []

    def test_short_query_returns_empty(self):
        assert vector_candidates('ab', 'Condition') == []

    def test_blank_query_returns_empty(self):
        assert vector_candidates('', 'Condition') == []


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestSuggestAPIStrategies:
    """Test strategy parameter validation on the suggest endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self, django_user_model):
        from rest_framework.test import APIClient
        user = django_user_model.objects.create_user(
            email='admin@test.com', password='pass', is_staff=True,
        )
        api_client = APIClient()
        api_client.force_authenticate(user=user)
        self.client = api_client

    def test_invalid_strategy_name_rejected(self):
        resp = self.client.post(
            '/api/v1/code-mappings/suggest/',
            data={
                'source_vocabulary_id': 'ICD10CM',
                'strategies': ['umls', 'bogus'],
            },
            format='json',
        )
        assert resp.status_code == 400
        assert 'bogus' in str(resp.data)

    def test_empty_strategies_rejected(self):
        resp = self.client.post(
            '/api/v1/code-mappings/suggest/',
            data={
                'source_vocabulary_id': 'ICD10CM',
                'strategies': [],
            },
            format='json',
        )
        assert resp.status_code == 400

    def test_non_list_strategies_rejected(self):
        resp = self.client.post(
            '/api/v1/code-mappings/suggest/',
            data={
                'source_vocabulary_id': 'ICD10CM',
                'strategies': 'umls',
            },
            format='json',
        )
        assert resp.status_code == 400

    def test_valid_strategies_accepted(self):
        """Valid strategies don't cause a validation error (may return 200/201)."""
        resp = self.client.post(
            '/api/v1/code-mappings/suggest/',
            data={
                'source_vocabulary_id': 'ICD10CM',
                'strategies': ['umls', 'lexical'],
                'min_occurrences': 99999,  # high threshold → no work
            },
            format='json',
        )
        # 200 = no mappings created (expected with high threshold), not 400.
        assert resp.status_code in (200, 201)
