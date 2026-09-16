"""Tests for the multi-strategy suggest pipeline.

Covers:
- UMLS CUI-bridge lookup (`umls_candidates`)
- Vector reranking of a retrieved shortlist (`vector_rerank`)
- Which queue rows a run is allowed to touch (`suggestable_mappings`)
- Pipeline orchestration (UMLS winner, progressive retrieval, strategy filtering)
- API endpoint parameter validation
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
    CANDIDATE_LIMIT,
    SUGGESTION_MODEL_VERSION,
    SUGGESTION_PROVENANCE,
    STRATEGY_LEXICAL,
    STRATEGY_UMLS,
    STRATEGY_VECTORS,
    VOCAB_TO_UMLS_ROOT,
    _UMLS_ROOT_TO_VOCAB,
    suggestable_mappings,
    umls_candidates,
    vector_rerank,
)
from omop_core.services.mapping_suggestions import suggest_mappings
from omop_core.services.suggest_jobs import (
    FakeDispatcher as FakeSuggestDispatcher,
    InlineDispatcher as InlineSuggestDispatcher,
    use_dispatcher as use_suggest_dispatcher,
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


@pytest.fixture()
def measurement_domain():
    return DomainFactory(domain_id='Measurement', domain_name='Measurement')


@pytest.fixture()
def loinc_vocab():
    return VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')


@pytest.fixture()
def lab_class():
    return ConceptClassFactory(concept_class_id='Lab Test')


@pytest.fixture()
def measurement_concept(measurement_domain, loinc_vocab, lab_class):
    return ConceptFactory(
        concept_id=3004501, concept_name='Glucose [Mass/volume] in Serum or Plasma',
        concept_code='2345-7', vocabulary=loinc_vocab, domain=measurement_domain,
        concept_class=lab_class, standard_concept='S',
    )


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
        # ICD10 shares the ICD10CM SAB, so the reverse map prefers ICD10CM.
        # Skip aliases that share a SAB with a canonical vocab.
        sab_aliases = {'ICD10'}  # ICD10 → ICD10CM SAB (alias of ICD10CM)
        for omop_vocab, umls_sab in VOCAB_TO_UMLS_ROOT.items():
            if omop_vocab in sab_aliases:
                continue
            assert _UMLS_ROOT_TO_VOCAB[umls_sab] == omop_vocab

    def test_all_strategies_constant(self):
        assert STRATEGY_UMLS in ALL_STRATEGIES
        assert STRATEGY_VECTORS in ALL_STRATEGIES
        assert STRATEGY_LEXICAL in ALL_STRATEGIES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def queue_row(source_code, **kwargs):
    """A Code Mapping queue row, the way ingest leaves one.

    Suggest reads the tab, so this — not a clinical row — is what puts a code
    in front of it.
    """
    defaults = {
        'source_vocabulary_id': '',
        'source_code_description': '',
        'domain_id': 'Measurement',
        'omop_table': 'measurement',
        'status': 'proposed',
        'origin': 'import',
        'origin_system': '',
        'occurrence_count': 12,
    }
    defaults.update(kwargs)
    return SourceCodeConceptMapping.objects.create(source_code=source_code, **defaults)


# ---------------------------------------------------------------------------
# Vector reranking
# ---------------------------------------------------------------------------

class TestVectorRerank:
    """Reranking reorders a shortlist; it never invents or drops candidates."""

    def _candidates(self, *concept_ids):
        return [
            {'concept_id': cid, 'concept_name': f'C{cid}', 'concept_code': str(cid),
             'vocabulary_id': 'LOINC', 'concept_class_id': 'Lab Test',
             'lexical_score': 0.5, 'retrieval': STRATEGY_LEXICAL}
            for cid in concept_ids
        ]

    def test_no_embeddings_leaves_the_order_alone(self, condition_domain):
        """concept_embedding is empty here, so retrieval order has to stand."""
        with connection.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'concept_embedding')"
            )
            if not cur.fetchone()[0]:
                pytest.skip('concept_embedding table not created yet')

        candidates = self._candidates(1, 2, 3)
        reranked, applied = vector_rerank('hypertension', candidates)
        assert applied is False
        assert [c['concept_id'] for c in reranked] == [1, 2, 3]

    def test_short_query_is_not_reranked(self):
        assert vector_rerank('ab', self._candidates(1, 2))[1] is False

    def test_blank_query_is_not_reranked(self):
        assert vector_rerank('', self._candidates(1, 2))[1] is False

    def test_a_single_candidate_needs_no_reranking(self):
        """There is no order to change, and embedding costs a model load."""
        assert vector_rerank('hypertension', self._candidates(1))[1] is False


# ---------------------------------------------------------------------------
# Which rows a run may touch
# ---------------------------------------------------------------------------

class TestSuggestableMappings:
    """The candidate set is the tab, filtered by provenance and review state."""

    def test_empty_provenance_is_eligible(self):
        queue_row('UNCLAIMED')
        assert [m.source_code for m in suggestable_mappings('measurement')] == ['UNCLAIMED']

    def test_a_previous_suggestion_with_no_destination_is_eligible(self):
        queue_row('MACHINE GUESS', origin_system='suggest v0.1')
        assert [m.source_code for m in suggestable_mappings('measurement')] == ['MACHINE GUESS']

    @pytest.mark.parametrize('provenance', ['HT-One', 'HT-FHIR', 'athena', 'hk-labs',
                                            'open-wearables-seed', 'fhir-upload'])
    def test_an_importer_row_with_no_destination_is_eligible(self, provenance):
        """There is nothing to overwrite. Staging has 69 of these -- 67
        open-wearables-seed and 2 hk-labs -- and they are exactly what Suggest
        is for."""
        queue_row('IMPORTED', origin_system=provenance)
        assert [m.source_code for m in suggestable_mappings('measurement')] == ['IMPORTED']

    @pytest.mark.parametrize('provenance', ['HT-One', 'HT-FHIR', 'athena', 'hk-labs'])
    def test_an_importer_destination_is_never_re_answered(self, provenance,
                                                          measurement_concept):
        """Its importer knew more than the source text does -- 75,257 of
        staging's 85,318 rows -- so re-deriving it would spend a model call to
        make the answer worse. Not even Replace touches it."""
        queue_row('IMPORTED', origin_system=provenance, target_concept=measurement_concept)
        assert suggestable_mappings('measurement') == []
        assert suggestable_mappings('measurement', resuggest=True) == []

    def test_replace_matches_suggest_provenance_case_insensitively(self, measurement_concept):
        queue_row('SHOUTED', origin_system='Suggest v0.2', target_concept=measurement_concept)
        assert suggestable_mappings('measurement') == []
        assert len(suggestable_mappings('measurement', resuggest=True)) == 1

    def test_approved_is_a_decision(self):
        queue_row('SIGNED OFF', status='approved')
        assert suggestable_mappings('measurement') == []

    def test_rejected_is_equally_a_decision(self):
        """Re-proposing put it back at the front of the queue on every run,
        where it spent a model call and created nothing."""
        queue_row('TURNED DOWN', status='rejected')
        assert suggestable_mappings('measurement') == []

    def test_a_row_that_already_has_a_destination_is_skipped(self, measurement_concept):
        queue_row('HAS ONE', target_concept=measurement_concept)
        assert suggestable_mappings('measurement') == []

    def test_replace_reaches_a_destination_only_a_suggest_run_set(self, measurement_concept):
        queue_row('HAS ONE', target_concept=measurement_concept,
                  origin_system=SUGGESTION_PROVENANCE)
        assert len(suggestable_mappings('measurement', resuggest=True)) == 1

    def test_below_the_threshold_is_skipped(self):
        """43% of unmapped source values appear exactly once."""
        queue_row('SEEN ONCE', occurrence_count=1)
        queue_row('SEEN OFTEN', occurrence_count=12)
        codes = [m.source_code for m in suggestable_mappings('measurement', min_occurrences=10)]
        assert codes == ['SEEN OFTEN']

    def test_a_threshold_of_one_keeps_uncounted_rows(self):
        """A seeded row can carry no count at all; comparing against 1 hides it."""
        queue_row('NEVER COUNTED', occurrence_count=0)
        assert len(suggestable_mappings('measurement', min_occurrences=1)) == 1

    def test_the_busiest_code_comes_first(self):
        queue_row('QUIET', occurrence_count=10)
        queue_row('BUSY', occurrence_count=30)
        assert suggestable_mappings('measurement')[0].source_code == 'BUSY'

    def test_another_tab_is_not_touched(self):
        queue_row('MINE', source_vocabulary_id='LOINC')
        queue_row('THEIRS', source_vocabulary_id='RxNorm')
        codes = [m.source_code for m in
                 suggestable_mappings('measurement', source_vocabulary_id='LOINC')]
        assert codes == ['MINE']

    def test_the_icd10_tab_covers_its_merged_alias(self):
        """HT-One sends ICD-10-CM-format codes under ICD10; Athena loads the
        concepts under ICD10CM. Both are one tab."""
        queue_row('A00.0', source_vocabulary_id='ICD10', omop_table='condition')
        queue_row('A00.1', source_vocabulary_id='ICD10CM', omop_table='condition')
        codes = {m.source_code for m in
                 suggestable_mappings('condition', source_vocabulary_id='ICD10')}
        assert codes == {'A00.0', 'A00.1'}

    def test_a_declined_code_stays_eligible(self):
        """It has no destination, so it is still work to do."""
        queue_row('DECLINED', origin_system=SUGGESTION_PROVENANCE,
                  last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        assert len(suggestable_mappings('measurement')) == 1

    def test_gaps_follow_seen_even_when_this_model_already_tried_them(self):
        queue_row('DECLINED BUT BUSY', occurrence_count=900,
                  origin_system=SUGGESTION_PROVENANCE,
                  last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        queue_row('NEVER TRIED', occurrence_count=12)
        codes = [m.source_code for m in suggestable_mappings('measurement')]
        assert codes == ['DECLINED BUT BUSY', 'NEVER TRIED']

    def test_a_declined_code_comes_round_again_once_the_tab_is_drained(self):
        queue_row('DECLINED', occurrence_count=900,
                  origin_system=SUGGESTION_PROVENANCE,
                  last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        assert [m.source_code for m in suggestable_mappings('measurement')] == ['DECLINED']

    def test_seen_outranks_model_version_for_gaps(self):
        queue_row('OLD GUESS', occurrence_count=12, origin_system='suggest v0.1',
                  last_suggest_attempt='v0.1')
        queue_row('THIS VERSION', occurrence_count=900,
                  origin_system=SUGGESTION_PROVENANCE,
                  last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        codes = [m.source_code for m in suggestable_mappings('measurement')]
        assert codes == ['THIS VERSION', 'OLD GUESS']

    def test_a_row_with_no_domain_is_not_guessed_at(self):
        """lexical filters on domain_id='' and finds nothing while UMLS skips
        the filter entirely, so a single cross-domain sibling would come back
        definitive and land a Drug concept on a measurement row."""
        from omop_core.mapping.suggestions import retrieval_pool
        candidates, cui, definitive = retrieval_pool(
            source_code='X', source_vocabulary_id='LOINC', source_text='glucose',
            domain_id='', strategies=list(ALL_STRATEGIES),
        )
        assert candidates == []
        assert definitive is False

    def test_another_table_is_not_touched(self):
        queue_row('DRUGGY', omop_table='drug_exposure', domain_id='Drug')
        assert suggestable_mappings('measurement') == []

    def test_several_tables_are_selected_and_ordered_together(self):
        """A tab maps to up to five clinical tables. Selecting per table would
        order within each and apply the limit to each."""
        queue_row('QUIET MEASUREMENT', occurrence_count=10, omop_table='measurement')
        queue_row('BUSY DRUG', occurrence_count=900, omop_table='drug_exposure',
                  domain_id='Drug')
        rows = suggestable_mappings(['measurement', 'drug_exposure'])
        assert [m.source_code for m in rows] == ['BUSY DRUG', 'QUIET MEASUREMENT']

    def test_the_limit_counts_codes_not_codes_per_table(self):
        for i in range(4):
            queue_row(f'MEAS-{i}', omop_table='measurement')
            queue_row(f'DRUG-{i}', omop_table='drug_exposure', domain_id='Drug')
        rows = suggestable_mappings(['measurement', 'drug_exposure'], limit=3)
        assert len(rows) == 3

    def test_replace_takes_the_gaps_before_the_replacements(self, measurement_concept):
        """An empty destination is a gap; a replaceable one is an improvement.
        The gap is worth the model call first."""
        queue_row('ALREADY ANSWERED', occurrence_count=900,
                  origin_system=SUGGESTION_PROVENANCE,
                  target_concept=measurement_concept)
        queue_row('NO DESTINATION', occurrence_count=10)
        rows = suggestable_mappings('measurement', resuggest=True)
        assert [m.source_code for m in rows] == ['NO DESTINATION', 'ALREADY ANSWERED']

    def test_gaps_by_seen_then_untried_replacements(self, measurement_concept):
        queue_row('GAP TRIED', occurrence_count=900,
                  origin_system=SUGGESTION_PROVENANCE,
                  last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        queue_row('GAP UNTRIED', occurrence_count=10)
        queue_row('ANSWERED TRIED', occurrence_count=900,
                  origin_system=SUGGESTION_PROVENANCE,
                  target_concept=measurement_concept,
                  last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        queue_row('ANSWERED UNTRIED', occurrence_count=10,
                  origin_system=SUGGESTION_PROVENANCE,
                  target_concept=measurement_concept)
        rows = suggestable_mappings('measurement', resuggest=True)
        assert [m.source_code for m in rows] == [
            'GAP TRIED', 'GAP UNTRIED', 'ANSWERED UNTRIED', 'ANSWERED TRIED',
        ]

    @pytest.mark.parametrize('attempted', [False, True])
    def test_seen_orders_both_destination_groups_before_the_limit(
        self, measurement_concept, attempted,
    ):
        attempt = SUGGESTION_MODEL_VERSION if attempted else ''
        for code, count, target in [
            ('ANSWER QUIET', 1, measurement_concept),
            ('GAP QUIET', 1, None),
            ('ANSWER BUSY', 900, measurement_concept),
            ('GAP BUSY', 30, None),
            ('GAP UNCOUNTED', 0, None),
        ]:
            queue_row(code, occurrence_count=count, target_concept=target,
                      origin_system=SUGGESTION_PROVENANCE,
                      last_suggest_attempt=attempt)
        rows = suggestable_mappings(
            'measurement', resuggest=True, min_occurrences=1, limit=4,
        )
        assert [m.source_code for m in rows] == [
            'GAP BUSY', 'GAP QUIET', 'GAP UNCOUNTED', 'ANSWER BUSY',
        ]


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestSuggestAPIStrategies:
    """Test parameter validation on the suggest endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self, django_user_model):
        from rest_framework.test import APIClient
        user = django_user_model.objects.create_user(
            email='admin@test.com', password='pass', is_staff=True,
        )
        api_client = APIClient()
        api_client.force_authenticate(user=user)
        self.client = api_client

    def _post(self, **payload):
        payload.setdefault('source_vocabulary_id', 'ICD10CM')
        return self.client.post(
            '/api/v1/code-mappings/suggest/', data=payload, format='json',
        )

    def test_invalid_strategy_name_rejected(self):
        resp = self._post(strategies=['umls', 'bogus'])
        assert resp.status_code == 400
        assert 'bogus' in str(resp.data)

    def test_empty_strategies_rejected(self):
        assert self._post(strategies=[]).status_code == 400

    def test_non_list_strategies_rejected(self):
        assert self._post(strategies='umls').status_code == 400

    def test_valid_strategies_accepted(self):
        """Valid strategies don't cause a validation error."""
        resp = self._post(strategies=['umls', 'lexical'], min_occurrences=99999)
        assert resp.status_code == 202

    def test_lexical_limit_must_be_a_number(self):
        assert self._post(lexical_limit='lots').status_code == 400

    def test_lexical_limit_is_capped(self):
        """The shortlist goes into one prompt per source code, so an unbounded
        value is an unbounded token bill."""
        resp = self._post(lexical_limit=100000)
        assert resp.status_code == 400
        assert 'lexical_limit' in resp.data

    def test_lexical_limit_reaches_the_runner(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            self._post(lexical_limit=5, min_occurrences=99999)
        assert fake.calls[0][1]['lexical_limit'] == 5

    def test_lexical_limit_defaults_when_absent(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            self._post(min_occurrences=99999)
        assert fake.calls[0][1]['lexical_limit'] == CANDIDATE_LIMIT

    def test_vectors_alone_is_rejected(self):
        """It reranks what retrieval found and retrieves nothing itself, so on
        its own it reports "no candidate concept" for every code -- which reads
        as a broken tab rather than a bad selection."""
        resp = self._post(strategies=['vectors'])
        assert resp.status_code == 400
        assert 'cannot run alone' in str(resp.data)

    def test_vectors_alone_is_rejected_by_suggest_one_too(self):
        resp = self.client.post(
            '/api/v1/code-mappings/suggest-one/',
            data={'source_code': 'X', 'omop_table': 'measurement',
                  'strategies': ['vectors']},
            format='json',
        )
        assert resp.status_code == 400
        assert 'cannot run alone' in str(resp.data)

    def test_vectors_with_a_retriever_is_accepted(self):
        assert self._post(
            strategies=['lexical', 'vectors'], min_occurrences=99999,
        ).status_code == 202

    @pytest.mark.parametrize('strategies', [['semantic'], ['semantic', 'vectors']])
    def test_semantic_is_an_independent_retriever(self, strategies):
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            response = self._post(strategies=strategies, min_occurrences=99999)
        assert response.status_code == 202
        assert fake.calls[0][1]['strategies'] == strategies

    def test_individual_preview_queues_and_never_changes_the_mapping(self, monkeypatch, measurement_concept):
        from omop_core.models import SuggestRun
        from omop_core.services.suggest_jobs import execute_run
        mapping = queue_row('DIALOG', target_concept=measurement_concept)
        before = SourceCodeConceptMapping.objects.filter(pk=mapping.pk).values().get()
        batch = SuggestRun.objects.create(state='success')
        hit = {
            'concept_id': measurement_concept.pk, 'concept_name': measurement_concept.concept_name,
            'concept_code': measurement_concept.concept_code, 'vocabulary_id': 'LOINC',
            'retrieval': 'umls', 'umls_score': 1,
        }
        monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *args: ([hit], 'C123'))
        def lexical(*args, **kwargs):
            run = SuggestRun.objects.exclude(pk=batch.pk).get()
            response = self.client.get(f'/api/v1/code-mappings/suggest-runs/{run.pk}/?include_activity=1')
            assert response.data['state'] == 'running'
            assert next(event for event in response.data['activity'] if event['stage'] == 'candidates')['candidates'] == [hit]
            return []
        monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lexical)
        monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *args: [])
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as dispatcher:
            response = self.client.post('/api/v1/code-mappings/suggest-one/', {
                'source_code': 'DIALOG', 'omop_table': 'measurement', 'async': True,
            }, format='json')
        assert response.status_code == 202
        assert response.data['state'] == 'queued'
        execute_run(*dispatcher.calls[0])
        run = SuggestRun.objects.get(pk=response.data['run_id'])
        assert run.state == 'success'
        assert [event['strategy'] for event in run.activity if event['stage'] == 'candidates'] == ['umls', 'lexical', 'semantic']
        assert run.activity[-1]['suggested']['concept_id'] == measurement_concept.pk
        assert run.activity[-1]['dry_run'] is True
        assert run.destinations == 0
        assert SourceCodeConceptMapping.objects.filter(pk=mapping.pk).values().get() == before
        assert self.client.get('/api/v1/code-mappings/suggest-runs/latest/').data['run_id'] == str(batch.pk)

    def test_individual_preview_failure_retains_candidates(self, monkeypatch):
        from omop_core.models import SuggestRun
        def fail(*args, activity, **kwargs):
            activity({'stage': 'candidates', 'strategy': 'umls', 'candidates': [{'concept_id': 123}]})
            raise RuntimeError('retrieval failed')
        monkeypatch.setattr('omop_core.mapping.suggestions.suggest_one_mapping', fail)
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            response = self.client.post('/api/v1/code-mappings/suggest-one/', {
                'source_code': 'DIALOG', 'omop_table': 'measurement', 'async': True,
            }, format='json')
        assert response.status_code == 202
        assert response.data['state'] == 'failure'
        assert response.data['activity'][0]['candidates'] == [{'concept_id': 123}]
        assert response.data['error'] == 'retrieval failed'
        assert SuggestRun.objects.get(pk=response.data['run_id']).done == 0

    def test_suggest_one_accepts_semantic_without_other_retrievers(self):
        response = self.client.post(
            '/api/v1/code-mappings/suggest-one/',
            data={'source_code': 'LOCAL-123', 'omop_table': 'measurement',
                  'strategies': ['semantic']}, format='json',
        )
        assert response.status_code == 200

    def test_default_strategies_include_semantic(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            self._post(min_occurrences=99999)
        assert fake.calls[0][1]['strategies'] == ['umls', 'lexical', 'semantic']

    def test_replace_requires_a_vocabulary(self):
        resp = self.client.post(
            '/api/v1/code-mappings/suggest/',
            data={'destination_vocabulary_id': 'HK-Labs', 'replace': True},
            format='json',
        )
        assert resp.status_code == 400


class TestSuggestRunLifecycle:
    """The queued contract: 202 with a run id, then poll that run."""

    @pytest.fixture(autouse=True)
    def _setup(self, django_user_model):
        from rest_framework.test import APIClient
        self.user = django_user_model.objects.create_user(
            email='runs@test.com', password='pass', is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, **payload):
        payload.setdefault('source_vocabulary_id', '')
        return self.client.post(
            '/api/v1/code-mappings/suggest/', data=payload, format='json',
        )

    def test_post_returns_202_and_a_run_id(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()):
            resp = self._post(min_occurrences=1)
        assert resp.status_code == 202
        assert resp.data['run_id']
        assert resp.data['state'] == 'queued'

    def test_queued_log_records_limit_and_order_before_worker_starts(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()):
            response = self._post(limit=50)
        run_id = response.data['run_id']
        log = self.client.get(f'/api/v1/code-mappings/suggest-runs/{run_id}/?include_activity=1')
        assert log.data['selection']['limit'] == 50
        assert 'without destinations first, regardless of provenance' in log.data['selection']['order']
        assert 'Seen count highest first' in log.data['selection']['order']
        assert log.data['activity'] == []
        assert 'activity' not in response.data  # normal progress polls stay small

    def test_log_snapshots_order_live_source_and_chosen_destination(self, monkeypatch, measurement_concept):
        from omop_core.models import SuggestRun
        busy = queue_row('BUSY', occurrence_count=900, origin_system='HT-One',
                         last_suggest_attempt=SUGGESTION_MODEL_VERSION)
        queue_row('QUIET', occurrence_count=1)
        chosen = {
            'concept_id': measurement_concept.pk,
            'concept_name': measurement_concept.concept_name,
            'concept_code': measurement_concept.concept_code,
            'vocabulary_id': 'LOINC', 'retrieval': 'lexical',
        }
        monkeypatch.setattr('omop_core.mapping.suggestions.retrieval_pool',
                            lambda **kwargs: ([chosen], None, False))

        def rank(*args, **kwargs):
            # Persisted before network ranking, readable from another request.
            events = SuggestRun.objects.latest('created_at').activity
            assert any(e.get('source_code') == 'BUSY' and e['stage'] == 'retrieving' for e in events)
            return chosen, 'High confidence: matching analyte.'

        monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            response = self._post(limit=1)
        assert response.data['state'] == 'success'
        run_id = response.data['run_id']
        path = f'/api/v1/code-mappings/suggest-runs/{run_id}/?include_activity=1'
        log = self.client.get(path).data
        assert [s['source_code'] for s in log['activity'][0]['sources']] == ['BUSY']
        result = next(e for e in log['activity'] if e['stage'] == 'result')
        assert result['suggested'] == chosen
        assert result['updated'] is True
        assert result['note'] == 'High confidence: matching analyte.'
        busy.refresh_from_db()
        busy.source_code = 'CHANGED LATER'
        busy.target_concept = None
        busy.save()
        assert self.client.get(path).data['activity'] == log['activity']

    def test_candidates_are_persisted_before_next_stage_and_inline_response_includes_them(
        self, monkeypatch, measurement_concept,
    ):
        from omop_core.models import SuggestRun
        mapping = queue_row('LIVE')
        hit = {
            'concept_id': measurement_concept.pk,
            'concept_name': measurement_concept.concept_name,
            'concept_code': measurement_concept.concept_code,
            'vocabulary_id': 'LOINC', 'retrieval': 'umls', 'umls_score': 1,
        }
        monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *args: ([hit], 'C123'))

        def lexical(*args, **kwargs):
            events = SuggestRun.objects.latest('created_at').activity
            candidate_events = [event for event in events if event['stage'] == 'candidates']
            assert [event['strategy'] for event in candidate_events] == ['umls']
            assert candidate_events[0]['candidates'] == [hit]
            assert candidate_events[0]['mapping_id'] == mapping.pk
            return []

        def semantic(*args, **kwargs):
            events = SuggestRun.objects.latest('created_at').activity
            assert [event['strategy'] for event in events if event['stage'] == 'candidates'] == ['umls', 'lexical']
            return [{**hit, 'retrieval': 'semantic', 'semantic_score': 0.9, 'vector_distance': 0.1}]

        monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lexical)
        monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', semantic)
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            response = self._post(strategies=['umls', 'lexical', 'semantic'], include_activity=True)
        assert response.data['state'] == 'success'
        events = response.data['activity']
        assert [event['strategy'] for event in events if event['stage'] == 'candidates'] == ['umls', 'lexical', 'semantic']
        ranked = next(event for event in events if event['stage'] == 'ranked')
        assert ranked['suggested']['concept_id'] == measurement_concept.pk
        assert ranked['candidates'][0]['vector_distance'] == 0.1
        # The UMLS event stays an immutable snapshot of the first retrieval.
        assert 'vector_distance' not in next(event for event in events if event['stage'] == 'candidates')['candidates'][0]
        alternative = ConceptFactory(domain=measurement_concept.domain, standard_concept='S')
        response = self.client.patch(f'/api/v1/code-mappings/{mapping.pk}/',
                                    {'destination_concept_id': alternative.pk, 'status': 'proposed'}, format='json')
        assert response.status_code == 200
        mapping.refresh_from_db()
        assert mapping.target_concept_id == alternative.pk
        assert mapping.suggested_target_concept_id == measurement_concept.pk
        assert mapping.status == 'proposed'
        assert mapping.suggestion_outcome == ''
        response = self.client.patch(f'/api/v1/code-mappings/{mapping.pk}/', {'status': 'approved'}, format='json')
        assert response.status_code == 200
        mapping.refresh_from_db()
        assert mapping.suggestion_outcome == 'overridden'


    def test_failed_log_preserves_current_source_and_error(self, monkeypatch):
        queue_row('BROKEN')
        def explode(**kwargs):
            raise RuntimeError('retrieval unavailable')
        monkeypatch.setattr('omop_core.mapping.suggestions.retrieval_pool', explode)
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            response = self._post()
        log = self.client.get(
            f"/api/v1/code-mappings/suggest-runs/{response.data['run_id']}/?include_activity=1",
        ).data
        assert log['state'] == 'failure'
        assert log['activity'][-2]['source_code'] == 'BROKEN'
        assert log['activity'][-1]['stage'] == 'failure'
        assert log['activity'][-1]['note'] == 'retrieval unavailable'

    def test_no_candidate_is_recorded_and_log_requires_admin(self):
        queue_row('NO MATCH')
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            response = self._post(strategies=['umls'])
        path = f"/api/v1/code-mappings/suggest-runs/{response.data['run_id']}/?include_activity=1"
        log = self.client.get(path).data
        result = next(e for e in log['activity'] if e['stage'] == 'result')
        assert result['suggested'] is None
        assert 'No candidate' in result['note']
        self.user.is_staff = False
        self.user.save()
        assert self.client.get(path).status_code == 403

    def test_default_includes_single_occurrences_and_uncounted_rows(self):
        queue_row('BUSY', occurrence_count=900)
        queue_row('ONCE', occurrence_count=1)
        queue_row('UNCOUNTED', occurrence_count=0)
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            resp = self._post()
        assert resp.status_code == 202
        assert resp.data['total'] == 3
        assert fake.calls[0][1]['min_occurrences'] == 1

    @pytest.mark.parametrize('obsolete_threshold', [10, 999999, 0, 'obsolete'])
    def test_cached_client_cannot_reenable_seen_threshold(self, obsolete_threshold):
        queue_row('BUSY', occurrence_count=900)
        queue_row('ONCE', occurrence_count=1)
        queue_row('UNCOUNTED', occurrence_count=0)
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            resp = self._post(min_occurrences=obsolete_threshold)
        assert resp.status_code == 202
        assert resp.data['total'] == 3
        assert fake.calls[0][1]['min_occurrences'] == 1

    @pytest.mark.parametrize('maximum', [3, 100])
    def test_reference_exposes_the_active_batch_ceiling(self, maximum):
        fake = FakeSuggestDispatcher()
        fake.max_codes = maximum
        with use_suggest_dispatcher(fake):
            response = self.client.get('/api/v1/code-mappings/reference/')
        assert response.status_code == 200
        assert response.data['suggest_max_per_run'] == maximum

    def test_the_total_is_known_before_any_work_runs(self):
        """The strip needs a denominator on its first poll, not a bar filling
        against a moving total."""
        queue_row('ONE', occurrence_count=12)
        queue_row('TWO', occurrence_count=12)
        with use_suggest_dispatcher(FakeSuggestDispatcher()):
            resp = self._post(min_occurrences=1)
        assert resp.data['total'] == 2

    def test_the_run_is_dispatched_with_the_tab_it_was_asked_for(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            self._post(source_vocabulary_id='LOINC', min_occurrences=1)
        assert fake.calls[0][1]['source_vocabulary_id'] == 'LOINC'

    def test_progress_is_pollable_by_run_id(self):
        with use_suggest_dispatcher(FakeSuggestDispatcher()):
            resp = self._post(min_occurrences=1)
        run_id = resp.data['run_id']
        poll = self.client.get(f'/api/v1/code-mappings/suggest-runs/{run_id}/')
        assert poll.status_code == 200
        assert poll.data['run_id'] == run_id
        assert poll.data['state'] == 'queued'

    def test_latest_saved_run_is_discoverable_after_completion(self):
        from omop_core.models import SuggestRun
        path = '/api/v1/code-mappings/suggest-runs/latest/'
        assert self.client.get(path).data == {'run_id': None}
        SuggestRun.objects.create(state='success')
        latest = SuggestRun.objects.create(state='success', activity=[{'message': 'Saved log'}])
        response = self.client.get(path)
        assert response.status_code == 200
        assert response.data == {'run_id': str(latest.id)}
        log = self.client.get(f'/api/v1/code-mappings/suggest-runs/{latest.id}/?include_activity=1')
        assert log.data['activity'] == [{'message': 'Saved log'}]
        self.client.force_authenticate(user=None)
        assert self.client.get(path).status_code in (401, 403)

    def test_an_unknown_run_is_404_not_a_stuck_bar(self):
        import uuid as _uuid
        resp = self.client.get(f'/api/v1/code-mappings/suggest-runs/{_uuid.uuid4()}/')
        assert resp.status_code == 404

    def test_polling_needs_the_same_permission_as_running(self):
        from rest_framework.test import APIClient
        with use_suggest_dispatcher(FakeSuggestDispatcher()):
            run_id = self._post(min_occurrences=1).data['run_id']
        outsider = APIClient()
        assert outsider.get(
            f'/api/v1/code-mappings/suggest-runs/{run_id}/'
        ).status_code in (401, 403)

    def test_the_inline_path_finishes_before_it_answers(self, measurement_concept):
        """A machine with no broker runs it in the request. Same wire contract,
        so the page needs one code path either way."""
        # On the Uncoded tab, which is the tab _post asks for and the one
        # Suggest actually works on.
        queue_row('2345-7')
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            resp = self._post(min_occurrences=1)
        assert resp.status_code == 202
        assert resp.data['state'] == 'success'
        assert resp.data['total'] == 1

    def test_the_inline_path_has_a_much_smaller_ceiling(self):
        """render.yaml leaves CELERY_BROKER_URL dashboard-managed on the web
        service, so a deployment that has not filled it in runs inline -- inside
        a request whose gunicorn default timeout is 30s. Fifty codes there is
        ~125s and a 502."""
        from omop_core.services.suggest_jobs import (
            INLINE_MAX_CODES, QUEUED_MAX_CODES, CeleryDispatcher, InlineDispatcher,
        )
        assert InlineDispatcher.max_codes == INLINE_MAX_CODES
        assert CeleryDispatcher.max_codes == QUEUED_MAX_CODES
        assert INLINE_MAX_CODES < QUEUED_MAX_CODES

    def test_the_run_limit_is_capped_by_the_dispatcher(self):
        from omop_core.services.suggest_jobs import INLINE_MAX_CODES
        with use_suggest_dispatcher(FakeSuggestDispatcher()) as fake:
            fake.max_codes = INLINE_MAX_CODES
            self._post(min_occurrences=1, limit=50)
        assert fake.calls[0][1]['limit'] == INLINE_MAX_CODES

    def test_the_limit_counts_codes_however_many_tables_the_tab_maps_to(self):
        """The Uncoded tab maps to all five clinical tables. The limit is a
        performance bound on codes evaluated; tables are an implementation
        detail of where the rows live."""
        for i in range(4):
            queue_row(f'MEAS-{i}', omop_table='measurement', domain_id='Measurement')
            queue_row(f'OBS-{i}', omop_table='observation', domain_id='Observation')
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            resp = self._post(min_occurrences=1, limit=3)
        assert resp.data['total'] == 3
        assert resp.data['done'] == 3

    def test_the_run_reports_what_the_tab_still_holds(self, measurement_concept):
        """A run is capped well below a tab's backlog, so without this the
        curator cannot tell another run is warranted."""
        for i in range(5):
            queue_row(f'CODE-{i}')
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            resp = self._post(min_occurrences=1, limit=2)
        assert resp.data['total'] == 2
        assert resp.data['remaining'] == 3

    def test_nothing_remains_once_every_code_has_been_tried(self, measurement_concept):
        """Including the ones that got no destination: re-running only
        re-declines them, so advising another click would go nowhere."""
        queue_row('ONLY ONE')
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            resp = self._post(min_occurrences=1, limit=5)
        assert resp.data['remaining'] == 0

    def test_a_failure_lands_on_the_row_not_in_a_worker_log(self, monkeypatch):
        """The page polls the row; an exception that only reached the log would
        leave the curator watching a bar that never moves."""
        queue_row('BOOM')

        def explode(*args, **kwargs):
            raise RuntimeError('retrieval exploded')

        monkeypatch.setattr(
            'omop_core.mapping.suggestions.lexical_candidates', explode,
        )
        with use_suggest_dispatcher(InlineSuggestDispatcher()):
            resp = self._post(min_occurrences=1)
        assert resp.status_code == 202
        assert resp.data['state'] == 'failure'
        assert 'retrieval exploded' in resp.data['error']

    def test_progress_is_reported_as_the_run_moves(self, measurement_concept):
        """suggest_mappings reports retrieval and writing separately, because
        retrieval is two thirds of the wait and finishes first."""
        queue_row('2345-7', source_vocabulary_id='LOINC')
        queue_row('OTHER CODE', source_vocabulary_id='LOINC')
        seen = []
        suggest_mappings(
            'measurement', min_occurrences=1, strategies=['lexical'], dry_run=True,
            progress=lambda stage, done, total: seen.append((stage, done, total)),
        )
        assert ('retrieving', 0, 2) in seen
        assert ('retrieving', 2, 2) in seen
        assert ('writing', 2, 2) in seen
        # Retrieval must be fully reported before the first write is.
        assert seen.index(('retrieving', 2, 2)) < seen.index(('writing', 1, 2))


# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """End-to-end: queue rows → suggest_mappings → correct strategy chosen."""

    @pytest.fixture()
    def glucose_bridge(self, umls_release, measurement_domain, loinc_vocab, lab_class):
        """LOINC 2345-7 → CUI → a standard SNOMED concept."""
        cui = UmlsConcept.objects.create(
            cui='C0017725', preferred_name='Glucose measurement', release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='LNC', code='2345-7',
            term_type='PT', name='Glucose [Mass/volume] in Serum or Plasma',
            is_preferred=True,
        )
        snomed_vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
        concept = ConceptFactory(
            concept_id=4144235, concept_name='Glucose measurement',
            concept_code='33747003', vocabulary=snomed_vocab,
            domain=measurement_domain, concept_class=lab_class, standard_concept='S',
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='33747003',
            term_type='PT', name='Glucose measurement',
        )
        return concept

    def test_umls_resolves_without_a_model_call(self, glucose_bridge):
        """A single NLM-curated equivalency is the answer; ranking it would be
        four seconds spent to agree."""
        queue_row('2345-7', source_vocabulary_id='LOINC')

        results = suggest_mappings('measurement', min_occurrences=10,
                                   strategies=['umls'], dry_run=True)

        assert len(results) == 1
        r = results[0]
        assert r['strategy_used'] == STRATEGY_UMLS
        assert r['umls_cui'] == 'C0017725'
        assert r['suggested']['concept_id'] == glucose_bridge.concept_id

    def test_umls_data_is_ignored_when_the_strategy_is_off(self, glucose_bridge):
        queue_row('2345-7', source_vocabulary_id='LOINC')

        results = suggest_mappings('measurement', min_occurrences=10,
                                   strategies=['lexical'], dry_run=True)

        assert len(results) == 1
        assert results[0]['strategy_used'] != STRATEGY_UMLS

    def test_no_strategies_selected_suggests_nothing(self):
        queue_row('2345-7', source_vocabulary_id='LOINC')

        results = suggest_mappings('measurement', min_occurrences=10,
                                   strategies=[], dry_run=True)

        assert len(results) == 1
        assert results[0]['suggested'] is None
        assert results[0]['strategy_used'] is None
        assert results[0]['note'] == 'No candidate concept found by any enabled strategy.'

    def test_a_run_writes_onto_the_existing_row(self, glucose_bridge):
        """No row is created: ingest made it, Suggest fills in the destination."""
        row = queue_row('2345-7', source_vocabulary_id='LOINC')
        before = SourceCodeConceptMapping.objects.count()

        results = suggest_mappings('measurement', min_occurrences=10,
                                   strategies=['umls'])

        assert SourceCodeConceptMapping.objects.count() == before
        assert results[0]['updated'] is True
        assert results[0]['mapping_id'] == row.id
        row.refresh_from_db()
        assert row.target_concept_id == glucose_bridge.concept_id
        assert row.suggested_target_concept_id == glucose_bridge.concept_id
        assert row.origin_system.startswith('suggest ')
        assert row.suggest_strategy == STRATEGY_UMLS
        assert row.umls_cui == 'C0017725'
        assert row.status == 'proposed', 'a machine guess is not a decision'

    def test_the_occurrence_count_survives_a_run(self, glucose_bridge):
        """Replace used to delete the row, taking its count and first_seen."""
        row = queue_row('2345-7', source_vocabulary_id='LOINC', occurrence_count=417)
        suggest_mappings('measurement', min_occurrences=10, strategies=['umls'])
        row.refresh_from_db()
        assert row.occurrence_count == 417

    def test_dry_run_writes_nothing(self, glucose_bridge):
        row = queue_row('2345-7', source_vocabulary_id='LOINC')
        suggest_mappings('measurement', min_occurrences=10,
                         strategies=['umls'], dry_run=True)
        row.refresh_from_db()
        assert row.target_concept_id is None
        assert row.origin_system == ''

    def test_an_importer_destination_is_never_rewritten(self, glucose_bridge,
                                                        measurement_concept):
        """A destination its importer asserted stands. A run must not spend a
        model call to replace it with one derived from the source text alone."""
        row = queue_row('2345-7', source_vocabulary_id='LOINC', origin_system='HT-One',
                        target_concept=measurement_concept)
        results = suggest_mappings('measurement', min_occurrences=10, strategies=['umls'])
        assert results == []
        row.refresh_from_db()
        assert row.origin_system == 'HT-One'
        assert row.target_concept_id == measurement_concept.concept_id

    def test_an_importer_row_with_no_destination_is_filled_in(self, glucose_bridge):
        """Nothing to overwrite, and 69 such rows on staging. Once a
        destination is proposed the provenance is the suggestion's, which is
        what set it."""
        row = queue_row('2345-7', source_vocabulary_id='LOINC',
                        origin_system='open-wearables-seed')
        results = suggest_mappings('measurement', min_occurrences=10, strategies=['umls'])
        assert len(results) == 1
        row.refresh_from_db()
        assert row.target_concept_id == glucose_bridge.concept_id
        assert row.origin_system == SUGGESTION_PROVENANCE
        assert row.suggestion_model_version == SUGGESTION_MODEL_VERSION

    def test_a_declined_row_keeps_the_provenance_that_raised_it(self):
        """Nothing was proposed, so nothing here is a suggestion. Stamping the
        provenance anyway would erase the ingest channel *and* enrol the code in
        the accuracy figures: code_mapping_detail reads an origin_system
        beginning "suggest" as one, so a curator's own hand-picked concept
        would later be recorded as having overridden a suggestion."""
        row = queue_row('ZZQQ NOTHING LIKE THIS', origin_system='hk-labs')

        suggest_mappings('measurement', min_occurrences=10)

        row.refresh_from_db()
        assert row.target_concept_id is None
        assert row.origin_system == 'hk-labs'
        assert row.suggestion_model_version == ''
        assert row.last_suggest_attempt == SUGGESTION_MODEL_VERSION, (
            'but it must still record that it tried, or it retries for ever'
        )

    def test_an_unmatchable_code_keeps_no_destination(self, measurement_domain,
                                                      loinc_vocab, lab_class):
        """A raw code is evidence, not a meaningful HK-* concept name."""
        row = queue_row('ZZQQ NOTHING LIKE THIS', source_vocabulary_id='LOINC')
        suggest_mappings('measurement', min_occurrences=10)
        row.refresh_from_db()
        assert row.target_concept_id is None
        assert row.destination_vocabulary_id == ''
        assert not Concept.objects.filter(
            vocabulary_id='HK-Labs', concept_name='ZZQQ NOTHING LIKE THIS',
        ).exists()


# ---------------------------------------------------------------------------
# Source enrichment: source_concept and umls_source_name persistence
# ---------------------------------------------------------------------------

class TestSourceEnrichment:
    """Verify suggest_mappings persists source-side metadata on SCCM rows."""

    def test_persists_source_concept_and_umls_name(
        self, umls_release, measurement_domain, loinc_vocab, lab_class,
    ):
        loinc_source_concept = ConceptFactory(
            concept_name='Glucose [Mass/volume] in Serum', concept_code='2345-7',
            vocabulary=loinc_vocab, domain=measurement_domain,
            concept_class=lab_class, standard_concept=None,
        )
        cui = UmlsConcept.objects.create(
            cui='C0017725', preferred_name='Glucose measurement', release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='LNC', code='2345-7', term_type='PT',
            name='Glucose [Mass/volume] in Serum or Plasma', is_preferred=True,
        )
        snomed_vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
        ConceptFactory(
            concept_id=4144237, concept_name='Glucose measurement',
            concept_code='33747005', vocabulary=snomed_vocab,
            domain=measurement_domain, concept_class=lab_class, standard_concept='S',
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='33747005',
            term_type='PT', name='Glucose measurement',
        )
        queue_row('2345-7', source_vocabulary_id='LOINC')

        results = suggest_mappings('measurement', min_occurrences=10,
                                   strategies=['umls'], dry_run=False)

        assert results[0]['updated'] is True
        mapping = SourceCodeConceptMapping.objects.get(id=results[0]['mapping_id'])
        assert mapping.source_concept == loinc_source_concept
        assert mapping.umls_source_name == 'Glucose [Mass/volume] in Serum or Plasma'

    def test_umls_name_used_as_description_fallback(
        self, umls_release, measurement_domain, loinc_vocab, lab_class,
    ):
        """No OMOP concept and no importer description, but UMLS has a name."""
        cui = UmlsConcept.objects.create(
            cui='C9999999', preferred_name='Fictional analyte', release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='LNC', code='99999-9', term_type='PT',
            name='Fictional Analyte Level in Serum', is_preferred=True,
        )
        snomed_vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
        ConceptFactory(
            concept_id=9999998, concept_name='Fictional analyte measurement',
            concept_code='99999998', vocabulary=snomed_vocab,
            domain=measurement_domain, concept_class=lab_class, standard_concept='S',
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='99999998',
            term_type='PT', name='Fictional analyte measurement',
        )
        queue_row('99999-9', source_vocabulary_id='LOINC')

        results = suggest_mappings('measurement', min_occurrences=10,
                                   strategies=['umls'], dry_run=False)

        mapping = SourceCodeConceptMapping.objects.get(id=results[0]['mapping_id'])
        assert mapping.source_code_description == 'Fictional Analyte Level in Serum'
        assert mapping.umls_source_name == 'Fictional Analyte Level in Serum'

    def test_a_curator_note_is_not_overwritten(self, django_user_model,
                                               measurement_domain, loinc_vocab,
                                               lab_class):
        """The candidate set is every queue row with no destination, whatever
        raised it, so the row may carry a note a person wrote. updated_by is the
        signal: the curator edit path is its only writer."""
        curator = django_user_model.objects.create_user(
            email='curator@test.com', password='pass', is_staff=True,
        )
        queue_row('2345-7', source_vocabulary_id='LOINC', origin_system='hk-labs',
                  notes='waiting on lab confirmation', updated_by=curator,
                  last_suggest_attempt='v0.1')

        suggest_mappings('measurement', min_occurrences=10, strategies=['lexical'])

        mapping = SourceCodeConceptMapping.objects.get(source_code='2345-7')
        assert mapping.notes == 'waiting on lab confirmation'
        assert mapping.last_suggest_attempt == SUGGESTION_MODEL_VERSION, (
            'the run still records that it tried'
        )

    def test_an_ingest_note_survives_the_first_run(self, measurement_domain,
                                                   loinc_vocab, lab_class):
        """_record_proposal can supply a note, and on the first run nothing here
        has written one yet, so there is nothing of ours to refresh."""
        queue_row('2345-7', source_vocabulary_id='LOINC', origin_system='hk-labs',
                  notes='code arrived without a display name')
        suggest_mappings('measurement', min_occurrences=10, strategies=['lexical'])
        assert SourceCodeConceptMapping.objects.get(
            source_code='2345-7').notes == 'code arrived without a display name'

    def test_a_blank_note_is_filled_in(self, measurement_domain, loinc_vocab, lab_class):
        queue_row('2345-7', source_vocabulary_id='LOINC', notes='')
        suggest_mappings('measurement', min_occurrences=10, strategies=['lexical'])
        assert SourceCodeConceptMapping.objects.get(source_code='2345-7').notes

    def test_a_previous_runs_note_is_replaced(self, measurement_domain, loinc_vocab,
                                              lab_class):
        """Nobody has edited the row and a previous run wrote the note, so it is
        ours to refresh."""
        queue_row('2345-7', source_vocabulary_id='LOINC',
                  origin_system='suggest v0.1', last_suggest_attempt='v0.1',
                  notes='an older run said this')
        suggest_mappings('measurement', min_occurrences=10, strategies=['lexical'])
        mapping = SourceCodeConceptMapping.objects.get(source_code='2345-7')
        assert mapping.notes != 'an older run said this'

    def test_a_curator_description_is_not_overwritten(
        self, umls_release, measurement_domain, loinc_vocab, lab_class,
    ):
        """These describe the code, not the suggestion."""
        cui = UmlsConcept.objects.create(
            cui='C0017725', preferred_name='Glucose', release=umls_release,
        )
        UmlsSourceCode.objects.create(
            concept=cui, root_source='LNC', code='2345-7', term_type='PT',
            name='Glucose [Mass/volume] in Serum or Plasma', is_preferred=True,
        )
        queue_row('2345-7', source_vocabulary_id='LOINC',
                  source_code_description='What the lab actually calls it')

        suggest_mappings('measurement', min_occurrences=10, strategies=['umls'])

        mapping = SourceCodeConceptMapping.objects.get(source_code='2345-7')
        assert mapping.source_code_description == 'What the lab actually calls it'


# ---------------------------------------------------------------------------
# ICD10 vocabulary → UMLS ICD10CM SAB lookup (#1028)
# ---------------------------------------------------------------------------

class TestICD10UmlsLookup:
    """Verify that ICD10 (HT-One) codes resolve through the ICD10CM UMLS SAB."""

    def test_icd10_vocab_maps_to_icd10cm_sab(self):
        assert VOCAB_TO_UMLS_ROOT.get('ICD10') == 'ICD10CM'

    def test_icd10_code_finds_umls_candidates(
        self, umls_release, condition_domain, snomed_vocab, concept_class,
    ):
        """An ICD10 code should find UMLS candidates via the ICD10CM SAB."""
        # Create ICD10 vocab (the source row's vocabulary)
        icd10_vocab = VocabularyFactory(vocabulary_id='ICD10', vocabulary_name='ICD10')

        cui = UmlsConcept.objects.create(
            cui='C0008031', preferred_name='Cholera',
            release=umls_release,
        )
        # UMLS source code under ICD10CM SAB (same code format)
        UmlsSourceCode.objects.create(
            concept=cui, root_source='ICD10CM', code='A00.0',
            term_type='PT', name='Cholera due to Vibrio cholerae 01, biovar cholerae',
        )
        # Sibling: SNOMED concept
        UmlsSourceCode.objects.create(
            concept=cui, root_source='SNOMEDCT_US', code='63650001',
            term_type='PT', name='Cholera',
        )
        ConceptFactory(
            concept_id=63650001, concept_name='Cholera',
            concept_code='63650001', vocabulary=snomed_vocab,
            domain=condition_domain, concept_class=concept_class,
            standard_concept='S',
        )

        # Look up with vocabulary_id='ICD10' — should work via ICD10CM SAB
        candidates, cui_str = umls_candidates('A00.0', 'ICD10', 'Condition')
        assert len(candidates) == 1
        assert candidates[0]['concept_id'] == 63650001
        assert cui_str == 'C0008031'


@pytest.mark.parametrize('cui_count', [3, 6])
def test_suggest_persists_all_bridge_cuis_without_varchar_overflow(
    cui_count, umls_release, condition_domain, snomed_vocab, icd10cm_vocab, concept_class,
):
    target = ConceptFactory(
        concept_id=777001, concept_code='44054006', concept_name='Test destination',
        vocabulary=snomed_vocab, domain=condition_domain, concept_class=concept_class,
        standard_concept='S',
    )
    cuis = [f'C{n:07d}' for n in range(1, cui_count + 1)]
    for value in cuis:
        cui = UmlsConcept.objects.create(cui=value, preferred_name='Test bridge', release=umls_release)
        UmlsSourceCode.objects.create(concept=cui, root_source='ICD10CM', code='TEST.OVERFLOW', term_type='PT', name='Test source')
        UmlsSourceCode.objects.create(concept=cui, root_source='SNOMEDCT_US', code=target.concept_code, term_type='PT', name='Test destination')
    mapping = queue_row('TEST.OVERFLOW', source_vocabulary_id='ICD10CM', domain_id='Condition', omop_table='condition')
    results = suggest_mappings('condition', source_vocabulary_id='ICD10CM', strategies=['umls'], min_occurrences=1)
    assert results[0]['updated'] is True
    mapping.refresh_from_db()
    assert mapping.target_concept_id == target.pk
    assert mapping.umls_cui == ','.join(cuis)
    assert len(mapping.umls_cui) > 20


@pytest.mark.parametrize('source', ['ICD10', 'ICD10CM', 'ICD10PCS', 'urn:oid:2.16.840.1.113883.6.90'])
@pytest.mark.parametrize('destination_domain', ['Observation', 'Procedure', 'Drug', 'Device'])
def test_icd_retrieves_other_standard_domains_without_umls(source, destination_domain):
    from omop_core.mapping.suggestions import retrieval_pool
    concept = ConceptFactory(concept_name='Unique candidate description', standard_concept='S',
                             domain=DomainFactory(domain_id=destination_domain))
    candidates, cui, definitive = retrieval_pool(
        source_code='TEST', source_vocabulary_id=source,
        source_text='Unique candidate description', domain_id='Condition',
        strategies=[STRATEGY_UMLS, STRATEGY_LEXICAL],
    )
    assert [c['concept_id'] for c in candidates] == [concept.pk]
    assert candidates[0]['domain_id'] == destination_domain
    assert cui is None
    assert not definitive


def test_lexical_filters_ineligible_synonyms_before_limit():
    from omop_core.mapping.suggestions import lexical_candidates
    from omop_core.models import ConceptSynonym
    ConceptFactory(concept_id=4180186)
    for i in range(12):
        concept = ConceptFactory(standard_concept=None, concept_name=f'ineligible {i}')
        ConceptSynonym.objects.create(concept=concept, concept_synonym_name='Distinctive synonym target', language_concept_id=4180186)
    good = ConceptFactory(standard_concept='S', concept_name='Different preferred name')
    ConceptSynonym.objects.create(concept=good, concept_synonym_name='Distinctive synonym target', language_concept_id=4180186)
    assert [c['concept_id'] for c in lexical_candidates('Distinctive synonym target', None)] == [good.pk]
