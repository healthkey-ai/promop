"""The benchmark command has to report a comparison, not just run."""
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _drug(name: str):
    return ConceptFactory(
        concept_name=name, standard_concept='S', invalid_reason=None,
        domain=DomainFactory(domain_id='Drug'),
        vocabulary=VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm'),
    )


def _run(*args: str) -> str:
    out = StringIO()
    call_command('benchmark_lexical_retrieval', *args, stdout=out)
    return out.getvalue()


def test_reports_timing_and_recall_for_a_drug_text():
    _drug('aspirin 81 MG Oral Tablet')

    output = _run('--text', 'ASPIRIN 81 MG ORAL TABLET')

    assert 'ASPIRIN' in output
    assert 'wide' in output and 'narrowed' in output
    assert 'kept' in output


def test_names_the_candidates_narrowing_dropped():
    """A dropped name is how a reviewer tells noise from a real loss."""
    _drug('aspirin 81 MG Oral Tablet')
    _drug('warfarin sodium 5 MG Oral Tablet')

    output = _run('--text', 'ASPIRIN 81 MG ORAL TABLET')

    assert 'dropped: warfarin sodium 5 MG Oral Tablet' in output


def test_skips_text_with_nothing_to_narrow():
    output = _run('--text', 'Type 2 diabetes mellitus')

    assert 'skip (nothing to narrow)' in output


def test_refuses_a_domain_that_never_narrows():
    with pytest.raises(CommandError, match='Drug only'):
        _run('--domain', 'Condition', '--text', 'Oral candidiasis')


def test_samples_the_queue_when_no_text_is_given():
    _drug('aspirin 81 MG Oral Tablet')
    SourceCodeConceptMapping.objects.create(
        domain_id='Drug', source_vocabulary_id='RxNorm', source_code='A1',
        source_code_description='ASPIRIN 81 MG ORAL TABLET', omop_table='drug_exposure',
    )

    output = _run('--count', '1')

    assert 'ASPIRIN' in output


def test_reports_the_index_each_variant_used():
    _drug('aspirin 81 MG Oral Tablet')

    output = _run('--text', 'ASPIRIN 81 MG ORAL TABLET', '--explain')

    assert 'index wide=' in output and 'narrowed=' in output
