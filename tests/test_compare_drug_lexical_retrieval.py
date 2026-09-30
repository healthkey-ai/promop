import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.factories import ConceptFactory, DomainFactory

pytestmark = pytest.mark.django_db(transaction=True)


def test_reports_complete_outputs_and_timings():
    concept = ConceptFactory(concept_name='aspirin 81 MG Oral Tablet',
                             domain=DomainFactory(domain_id='Drug'))
    out = StringIO()
    call_command('compare_drug_lexical_retrieval', '--text', 'ASPIRIN 81 MG ORAL TABLET',
                 '--repeat', '2', '--json', stdout=out)
    row = json.loads(out.getvalue())['comparisons'][0]
    assert row['identical']
    assert row['before'] == row['after']
    assert row['before'][0]['concept_id'] == concept.pk
    assert row['before_median_ms'] > 0
    assert row['after_median_ms'] > 0


@pytest.mark.parametrize('change', ['score', 'order', 'missing'])
def test_fails_on_any_output_difference(change):
    def retrieve(text, domain, limit, *, optimize_drug_search):
        result = [{'concept_id': 1, 'lexical_score': 0.9},
                  {'concept_id': 2, 'lexical_score': 0.8}]
        if optimize_drug_search:
            if change == 'score':
                result[0]['lexical_score'] = 0.89
            elif change == 'order':
                result.reverse()
            else:
                result.pop()
        return result

    with patch('omop_core.management.commands.compare_drug_lexical_retrieval.lexical_candidates', retrieve):
        with pytest.raises(CommandError, match='Candidate output changed'):
            call_command('compare_drug_lexical_retrieval', '--text', 'aspirin', stdout=StringIO())


@pytest.mark.parametrize('option,value', [('--limit', '0'), ('--limit', '101'),
                                         ('--repeat', '0'), ('--count', '0')])
def test_invalid_bounds_are_rejected(option, value):
    with pytest.raises(CommandError):
        call_command('compare_drug_lexical_retrieval', option, value, stdout=StringIO())
