"""The drug backfill's trailing-parenthetical strip, without a database.

The command used `re.sub(r'\\s*\\([^)]+\\)\\s*$', '', source_value)`, which
backtracks over every space in the value (CodeQL py/polynomial-redos) and the
values come from uploads. The scan must keep the pattern's answers.
"""
import itertools
import re

import pytest

from omop_core.management.commands.backfill_drug_concepts import (
    _without_trailing_parenthetical,
)

CASES = [
    'Lenalidomide (Revlimid)',
    'Lenalidomide (oral)  ',
    '  (oral)',
    'Lenalidomide',
    'a (b) (c)',
    'a ((b))',
    'Lenalidomide ()',
    'Revlimid (',
    '(unbalanced',
    # The greedy `[^)]+` strips from the earliest opener, not the last one.
    'a (b (c)',
    'x (y()',
    '(a ()',
    'Dexamethasone (Decadron (oral)',
    'a)b(c)',
]


@pytest.mark.parametrize('value', CASES)
def test_the_scan_answers_what_the_pattern_did(value):
    assert _without_trailing_parenthetical(value) == re.sub(r'\s*\([^)]+\)\s*$', '', value)


def test_a_long_run_of_spaces_is_one_pass():
    # 20,000 spaces before the parenthetical: the pattern re-scanned the run
    # from every offset; the scan strips the value once.
    assert _without_trailing_parenthetical(' ' * 20000 + '(oral)') == ''


def test_the_scan_is_exactly_the_pattern_over_every_short_value():
    """The alphabet is the one that distinguishes the shapes: brackets, space."""
    pattern = re.compile(r'\s*\([^)]+\)\s*$')
    for length in range(0, 8):
        for chars in itertools.product('() ab', repeat=length):
            value = ''.join(chars)
            assert _without_trailing_parenthetical(value) == pattern.sub('', value), value
