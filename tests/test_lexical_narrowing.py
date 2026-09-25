"""Narrowing a drug search on its ingredient keeps the answers it used to find.

The point of `narrowing_text` is speed, so the tests that matter are the ones
about recall: the right concept still comes back, and nothing new appears.
"""
import pytest

from omop_core.mapping.source_text import narrowing_text
from omop_core.mapping.suggestions import _narrowing_filter, lexical_candidates
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _drug(name: str):
    """A standard Drug concept, which is all lexical retrieval will look at."""
    return ConceptFactory(
        concept_name=name, standard_concept='S', invalid_reason=None,
        domain=DomainFactory(domain_id='Drug'),
        vocabulary=VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm'),
    )


def _ids(source_value: str, domain_id: str | None = 'Drug') -> list[int]:
    return [c['concept_id'] for c in lexical_candidates(source_value, domain_id)]


class TestNarrowingText:
    @pytest.mark.parametrize('source_value, expected', [
        ('ASPIRIN 81 MG ORAL TABLET', 'ASPIRIN'),
        ('aspirin 81 mg oral tablet', 'ASPIRIN'),
        ('Metformin Hydrochloride 500 MG Extended Release Tablet',
         'METFORMIN HYDROCHLORIDE'),
        ('Insulin Glargine 100 UNT/ML Injectable Solution', 'INSULIN GLARGINE'),
    ])
    def test_drops_dose_and_form_words(self, source_value, expected):
        assert narrowing_text(source_value) == expected

    @pytest.mark.parametrize('source_value, expected', [
        # SNOMED phrasing. The hyphen has to split or the exact match is lost.
        ('Norfloxacin-containing product', 'NORFLOXACIN'),
        ('Famciclovir-containing product', 'FAMCICLOVIR'),
        ('Dezocine only product', 'DEZOCINE'),
        ('Plazomicin-containing product in parenteral dose form', 'PLAZOMICIN'),
        ('Edetate trisodium 200 mg/mL solution for injection', 'EDETATE TRISODIUM'),
    ])
    def test_keeps_only_ingredient_words_from_snomed_phrasing(self, source_value, expected):
        assert narrowing_text(source_value) == expected

    def test_repeated_ingredient_appears_once(self):
        # The key matches one extent of the name, so a repeat can only lose matches.
        assert narrowing_text('Granisetron (as granisetron hydrochloride) 200 micrograms') == (
            'GRANISETRON HYDROCHLORIDE'
        )

    @pytest.mark.parametrize('source_value', [
        'Type 2 diabetes mellitus',
        'Total knee replacement',
        'Hemoglobin A1c',
        'Isoeugenol',
        'Bis-gamma-glutamylcystine reductase (NADPH)',
        # Function words must not look like a drug, or conditions get narrowed too.
        'Disorder of the liver',
    ])
    def test_leaves_text_without_drug_words_alone(self, source_value):
        assert narrowing_text(source_value) is None

    @pytest.mark.parametrize('source_value', [
        '',
        None,
        'MG ORAL TABLET',
        '500 MG TABLET',
        '10 ML',
    ])
    def test_refuses_when_no_ingredient_is_left(self, source_value):
        assert narrowing_text(source_value) is None

    def test_picks_the_word_similarity_operator_only_when_narrowed(self):
        assert _narrowing_filter('ASPIRIN 81 MG ORAL TABLET', 'Drug') == {
            'name_upper__trigram_word_similar': 'ASPIRIN',
        }
        assert _narrowing_filter('Type 2 diabetes mellitus', 'Drug') == {
            'name_upper__trigram_similar': 'Type 2 diabetes mellitus',
        }

    @pytest.mark.parametrize('domain_id, source_value', [
        # Form words mean something outside a drug name, so these stay wide.
        ('Condition', 'Oral candidiasis'),
        ('Procedure', 'Injection of joint'),
        ('Measurement', 'Unit of blood transfused'),
        (None, 'Oral candidiasis'),
    ])
    def test_only_drug_searches_are_narrowed(self, domain_id, source_value):
        assert _narrowing_filter(source_value, domain_id) == {
            'name_upper__trigram_similar': source_value,
        }


class TestLexicalRecall:
    def test_finds_the_exact_product_despite_the_dose_words(self):
        target = _drug('aspirin 81 MG Oral Tablet')
        _drug('ibuprofen 200 MG Oral Tablet')
        _drug('metformin 500 MG Oral Tablet')

        assert target.pk in _ids('ASPIRIN 81 MG ORAL TABLET')

    def test_does_not_admit_concepts_the_wider_search_rejects(self):
        """Narrowing only prefilters, so its results stay a subset of the old ones."""
        _drug('aspirin 81 MG Oral Tablet')
        _drug('aspirin 325 MG Oral Tablet')
        for i in range(5):
            _drug(f'ibuprofen {i}00 MG Oral Tablet')

        narrowed = set(_ids('ASPIRIN 81 MG ORAL TABLET'))
        # The same query with narrowing disabled is what the old code ran.
        from omop_core.mapping import suggestions
        wide = set(_ids_with_narrowing_off(suggestions, 'ASPIRIN 81 MG ORAL TABLET'))

        assert narrowed <= wide
        assert narrowed

    def test_ingredient_only_source_still_matches_products(self):
        target = _drug('aspirin 81 MG Oral Tablet')

        assert target.pk in _ids('ASPIRIN TABLET')

    def test_drops_drugs_that_only_share_dose_and_form_words(self):
        """The whole point: warfarin matched 'MG ORAL TABLET', not the ingredient."""
        _drug('aspirin 81 MG Oral Tablet')
        warfarin = _drug('warfarin sodium 5 MG Oral Tablet')
        from omop_core.mapping import suggestions

        assert warfarin.pk in _ids_with_narrowing_off(suggestions, 'ASPIRIN 81 MG ORAL TABLET')
        assert warfarin.pk not in _ids('ASPIRIN 81 MG ORAL TABLET')

    def test_procedure_keeps_its_form_word(self):
        """'Injection of joint' is the procedure, so narrowing must not touch it."""
        target = ConceptFactory(
            concept_name='Injection of joint', standard_concept='S', invalid_reason=None,
            domain=DomainFactory(domain_id='Procedure'),
        )

        assert target.pk in _ids('Injection of joint', 'Procedure')

    def test_non_drug_domain_is_unaffected(self):
        condition = ConceptFactory(
            concept_name='Type 2 diabetes mellitus', standard_concept='S',
            invalid_reason=None, domain=DomainFactory(domain_id='Condition'),
        )

        assert _ids('Type 2 diabetes mellitus', 'Condition') == [condition.pk]


def _ids_with_narrowing_off(suggestions_module, source_value: str) -> list[int]:
    """Run retrieval the way it ran before narrowing existed."""
    original = suggestions_module.narrowing_text
    suggestions_module.narrowing_text = lambda _text: None
    try:
        return [c['concept_id'] for c in suggestions_module.lexical_candidates(source_value, 'Drug')]
    finally:
        suggestions_module.narrowing_text = original
