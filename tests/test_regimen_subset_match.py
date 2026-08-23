"""Naming the regimen behind a set of drugs (#642).

A line of therapy is a set of drug exposures, and the name shown for it is
looked up from that set. When the set matches no regimen exactly, the lookup
falls back to the entries contained within it — and that fallback used to return
the first subset it happened to meet.

Two things went wrong there. A combination could be named after a single drug,
so bortezomib and lenalidomide read as "Lenalidomide monotherapy" — the word
saying the opposite of what happened. And when several entries were contained in
the set, whichever sat earlier in the literals won, so reordering the tables
would silently change what a clinician reads.
"""
import pytest

from omop_core.services.lot_regimens import (
    MYELOMA_REGIMEN_LOOKUP, REGIMEN_LOOKUP,
    get_regimen_concept_id, get_regimen_name,
)


class TestExactMatchesAreUnchanged:
    """The fallback is the only thing being altered."""

    @pytest.mark.parametrize('drugs,expected', [
        (['lenalidomide', 'dexamethasone'], 'Rd'),
        (['bortezomib', 'lenalidomide', 'dexamethasone'], 'VRD'),
        (['carfilzomib', 'lenalidomide', 'dexamethasone'], 'KRD'),
    ])
    def test_a_known_regimen_still_resolves(self, drugs, expected):
        assert get_regimen_name(drugs) == expected

    def test_a_known_regimen_still_carries_its_concept(self):
        assert get_regimen_concept_id(['lenalidomide', 'dexamethasone']) == 35806053


class TestSingleAgentNamesNeverLabelACombination:
    @pytest.mark.parametrize('drugs', [
        ['bortezomib', 'lenalidomide'],
        ['daratumumab', 'bortezomib'],
        ['daratumumab', 'lenalidomide'],
    ])
    def test_a_combination_is_not_named_after_one_of_its_drugs(self, drugs):
        # The reported symptom. A single-drug entry contained in a larger set is
        # not a less specific match, it is a wrong one.
        name = get_regimen_name(drugs)
        assert name is None, f'{drugs} was named {name!r}'

    @pytest.mark.parametrize('drugs', [
        ['bortezomib', 'lenalidomide'],
        ['daratumumab', 'bortezomib'],
    ])
    def test_no_concept_is_asserted_for_one_either(self, drugs):
        assert get_regimen_concept_id(drugs) is None

    def test_no_combination_anywhere_resolves_to_a_monotherapy_name(self):
        # Guards the class of bug rather than the three cases that were found.
        merged = {**MYELOMA_REGIMEN_LOOKUP, **REGIMEN_LOOKUP}
        singles = [set(k) for k in merged if len(k) == 1]
        assert singles, 'expected single-drug entries to exist'
        for single in singles:
            drug = next(iter(single))
            name = get_regimen_name([drug, 'a-drug-in-no-regimen'])
            assert name is None or 'monotherapy' not in name.lower(), (
                f'{drug} + an unknown drug was named {name!r}'
            )

    @pytest.mark.parametrize('drugs,expected', [
        (['lenalidomide'], 'Lenalidomide monotherapy'),
        (['daratumumab'], 'Dara mono'),
    ])
    def test_a_genuine_single_agent_line_keeps_its_name(self, drugs, expected):
        # The guard is about combinations. One drug really is monotherapy.
        assert get_regimen_name(drugs) == expected


class TestTheMatchIsSpecificAndNotIncidental:
    def test_the_largest_contained_entry_wins(self):
        # A doublet contained in the set beats a single, so adding an unrelated
        # drug to a known doublet still names the doublet rather than dropping to
        # one of its members.
        assert get_regimen_name(
            ['lenalidomide', 'dexamethasone', 'a-drug-in-no-regimen'],
        ) == 'Rd'

    def test_the_answer_does_not_depend_on_table_order(self):
        # The defect: {daratumumab, lenalidomide} contains two single-drug
        # entries, and whichever came first in the literals was returned. Both
        # orderings must now agree.
        drugs = ['daratumumab', 'lenalidomide']
        forward = get_regimen_name(drugs)
        reversed_ = get_regimen_name(list(reversed(drugs)))
        assert forward == reversed_

    def test_an_ambiguous_tie_is_refused_rather_than_guessed(self):
        # Several equally-sized entries contained in the set have no ranking
        # between them, and inventing one is what this fixes.
        assert get_regimen_name(['daratumumab', 'lenalidomide']) is None
