"""suggest_synonym_term: the domain-scoped synonym table Suggest reads (#1467).

Two things matter. The table must hold exactly the synonyms Suggest may
retrieve, and reading it must give the same answer as searching
concept_synonym directly -- it is an index-shaped copy, not a second opinion.
"""
from unittest import mock

import pytest

from omop_core.mapping.suggestions import lexical_candidates
from omop_core.models import (
    SUGGEST_DESTINATION_DOMAINS,
    ConceptSynonym,
    SuggestSynonymTerm,
)
from omop_core.services import suggest_synonym_terms
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE
from tests.factories import ConceptFactory, DomainFactory

pytestmark = pytest.mark.django_db

ENGLISH = 4180186


def _synonym(concept, name):
    ConceptFactory(concept_id=ENGLISH)
    return ConceptSynonym.objects.create(
        concept=concept, concept_synonym_name=name, language_concept_id=ENGLISH,
    )


def _concept(domain='Condition', **kwargs):
    return ConceptFactory(domain=DomainFactory(domain_id=domain), **kwargs)


def _terms():
    return set(SuggestSynonymTerm.objects.values_list('concept_id', 'domain_id', 'term'))


def test_domains_match_the_tables_a_mapping_can_land_in():
    """models cannot import services, so the tuple is repeated; hold them in step."""
    assert set(SUGGEST_DESTINATION_DOMAINS) == set(DOMAIN_TO_TABLE)


def test_refresh_holds_only_synonyms_of_eligible_concepts():
    good = _concept(concept_name='Preferred name')
    _synonym(good, 'Plumbus disorder')
    _synonym(_concept(standard_concept=None), 'Plumbus non-standard')
    _synonym(_concept(invalid_reason='U'), 'Plumbus retired')
    _synonym(_concept(domain='Device'), 'Plumbus device')

    assert suggest_synonym_terms.refresh() == (1, 0)
    assert _terms() == {(good.pk, 'Condition', 'PLUMBUS DISORDER')}


def test_refresh_is_idempotent():
    _synonym(_concept(), 'Plumbus disorder')
    suggest_synonym_terms.refresh()
    assert suggest_synonym_terms.refresh() == (0, 0)
    assert SuggestSynonymTerm.objects.count() == 1


def test_synonyms_differing_only_by_case_are_one_term():
    concept = _concept()
    _synonym(concept, 'Plumbus disorder')
    _synonym(concept, 'PLUMBUS DISORDER')
    assert suggest_synonym_terms.refresh() == (1, 0)


def test_refresh_removes_terms_that_stopped_being_eligible():
    retired, moved, dropped, kept = _concept(), _concept(), _concept(), _concept()
    for concept in (retired, moved, kept):
        _synonym(concept, f'Plumbus {concept.pk}')
    gone = _synonym(dropped, f'Plumbus {dropped.pk}')
    suggest_synonym_terms.refresh()
    assert SuggestSynonymTerm.objects.count() == 4

    type(retired).objects.filter(pk=retired.pk).update(invalid_reason='D')
    type(moved).objects.filter(pk=moved.pk).update(domain=DomainFactory(domain_id='Observation'))
    gone.delete()

    inserted, deleted = suggest_synonym_terms.refresh()
    # The concept that changed domain is removed under the old one and added
    # under the new one; a partial index per domain depends on that.
    assert (inserted, deleted) == (1, 3)
    assert _terms() == {
        (kept.pk, 'Condition', f'PLUMBUS {kept.pk}'),
        (moved.pk, 'Observation', f'PLUMBUS {moved.pk}'),
    }


def test_a_synonym_longer_than_a_btree_entry_does_not_fail_the_refresh():
    """The unique rule hashes the term: 1000 multi-byte characters is ~3kB."""
    _synonym(_concept(), 'дисплазия ' * 100)
    assert suggest_synonym_terms.refresh() == (1, 0)


def test_is_populated():
    assert not suggest_synonym_terms.is_populated('Condition')
    _synonym(_concept(), 'Plumbus disorder')
    suggest_synonym_terms.refresh()
    assert suggest_synonym_terms.is_populated('Condition')
    # Built for another domain, and domains Suggest never scopes to.
    assert not suggest_synonym_terms.is_populated('Procedure')
    assert not suggest_synonym_terms.is_populated('Device')
    assert not suggest_synonym_terms.is_populated(None)


def _ids_and_scores(query, domain):
    return [(c['concept_id'], c['lexical_score']) for c in lexical_candidates(query, domain)]


def test_lexical_candidates_answers_the_same_from_either_source():
    by_synonym = _concept(concept_name='Unrelated preferred name')
    _synonym(by_synonym, 'Plumbus disorder of the knee')
    _synonym(by_synonym, 'Plumbus knee disorder')
    by_name = _concept(concept_name='Plumbus disorder of knee')
    _synonym(_concept(domain='Procedure'), 'Plumbus disorder of the knee')
    _synonym(_concept(standard_concept=None), 'Plumbus disorder of the knee')

    direct = _ids_and_scores('plumbus disorder of the knee', 'Condition')
    assert {by_synonym.pk, by_name.pk} == {cid for cid, _ in direct}

    suggest_synonym_terms.refresh()
    assert suggest_synonym_terms.is_populated('Condition')
    assert _ids_and_scores('plumbus disorder of the knee', 'Condition') == direct


def test_lexical_candidates_reads_the_table_when_it_is_populated():
    """Guard against the fast path quietly never being taken."""
    concept = _concept(concept_name='Unrelated preferred name')
    _synonym(concept, 'Plumbus disorder')
    suggest_synonym_terms.refresh()
    # Remove the source row: only the table can now produce the hit.
    ConceptSynonym.objects.filter(concept=concept).delete()
    assert [c['concept_id'] for c in lexical_candidates('Plumbus disorder', 'Condition')] == [concept.pk]


def test_a_stale_term_never_surfaces_an_ineligible_concept():
    """Retired since the last refresh: the term is still there, the concept must not be."""
    concept = _concept(concept_name='Unrelated preferred name')
    _synonym(concept, 'Plumbus disorder')
    suggest_synonym_terms.refresh()
    type(concept).objects.filter(pk=concept.pk).update(invalid_reason='U')
    assert lexical_candidates('Plumbus disorder', 'Condition') == []


def test_no_domain_searches_concept_synonym_directly():
    """ICD-10 searches every domain; the table is per domain, so it is not used."""
    concept = _concept(concept_name='Unrelated preferred name')
    _synonym(concept, 'Plumbus disorder')
    suggest_synonym_terms.refresh()
    SuggestSynonymTerm.objects.all().delete()
    assert [c['concept_id'] for c in lexical_candidates('Plumbus disorder', None)] == [concept.pk]


def test_the_vocabulary_loader_refreshes_the_table():
    from omop_core.management.commands.load_athena_vocabularies import Command
    command = Command()
    command._log = mock.Mock()
    _synonym(_concept(), 'Plumbus disorder')
    command._refresh_suggest_synonym_terms()
    assert SuggestSynonymTerm.objects.count() == 1
    assert '1 added' in command._log.call_args_list[-1].args[0]
