"""Live Athena domain corrections preserve everything except Concept.domain_id."""
import csv
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import Concept, MappingDestinationCandidate, SourceCodeConceptMapping as Mapping
from omop_core.services.athena_domain_reconciliation import destination_snapshots, reconcile_domain
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def mapping(target, vocabulary='ICD10', code='example'):
    return Mapping.objects.create(source_vocabulary_id=vocabulary, source_code=code,
        target_concept=target, status='approved', notes='Curator decision', occurrence_count=71)


def snapshot(c):
    return dict(concept_id=c.pk, vocabulary_id=c.vocabulary_id, concept_code=c.concept_code,
                domain_id=c.domain_id, source=c.source)


def evidence(c, **overrides):
    return {'concept': dict(concept_id=c.pk, vocabulary_id=c.vocabulary_id,
        concept_code=c.concept_code, domain_id='Condition', standard_concept='S',
        invalid_reason=None, valid_start_date='1970-01-01', valid_end_date='2099-12-31', **overrides)}


def test_scope_includes_choices_and_deduplicates_only_requested_source_vocabulary():
    chosen, choice, other, nonstandard = (ConceptFactory() for _ in range(4))
    nonstandard.standard_concept = None; nonstandard.save()
    row = mapping(chosen)
    mapping(chosen, code='second')
    mapping(other, vocabulary='ICD10CM')
    mapping(nonstandard, code='nonstandard')
    for target in (choice, chosen, nonstandard):
        MappingDestinationCandidate.objects.create(mapping=row, target_concept=target,
            target_vocabulary_id=target.vocabulary_id, target_concept_code=target.concept_code)
    assert [r['concept_id'] for r in destination_snapshots('ICD10')] == sorted([chosen.pk, choice.pk])


@pytest.mark.parametrize('apply', [False, True])
def test_domain_only_change_for_general_concept_preserves_metadata_and_mapping(apply):
    DomainFactory(domain_id='Condition')
    concept = ConceptFactory(concept_id=9871234, concept_code='outside-workbook',
        vocabulary=VocabularyFactory(vocabulary_id='RxNorm'),
        domain=DomainFactory(domain_id='Observation'), invalid_reason='U', valid_end_date='2002-01-31')
    row = mapping(concept)
    before = Concept.objects.values().get(pk=concept.pk)
    mapping_before = Mapping.objects.values().get(pk=row.pk)
    result = reconcile_domain(snapshot(concept), evidence(concept), apply=apply)
    assert result['outcome'] == ('updated' if apply else 'would_update')
    assert Concept.objects.values().get(pk=concept.pk) == {**before, 'domain_id': 'Condition' if apply else 'Observation'}
    assert Mapping.objects.values().get(pk=row.pk) == mapping_before
    concept.refresh_from_db()
    assert reconcile_domain(snapshot(concept), evidence(concept), apply=apply)['outcome'] == ('unchanged' if apply else 'would_update')


@pytest.mark.parametrize(('change', 'outcome'), [
    ({'concept_id': 123}, 'identity_conflict'),
    ({'vocabulary_id': 'Wrong'}, 'identity_conflict'),
    ({'concept_code': 'Wrong'}, 'identity_conflict'),
    ({'standard_concept': ''}, 'upstream_nonstandard'),
    ({'invalid_reason': 'U'}, 'upstream_inactive'),
    ({'valid_end_date': '2000-01-01'}, 'upstream_inactive'),
    ({'valid_end_date': 'bad'}, 'lookup_failed'),
    ({'domain_id': ''}, 'lookup_failed'),
    ({'domain_id': 'UninstalledDomain'}, 'missing_domain'),
])
def test_unverified_or_inapplicable_evidence_never_changes_domain(change, outcome):
    c = ConceptFactory(); before = Concept.objects.values().get(pk=c.pk)
    result = evidence(c); result['concept'].update(change)
    assert reconcile_domain(snapshot(c), result, apply=True)['outcome'] == outcome
    assert Concept.objects.values().get(pk=c.pk) == before


@pytest.mark.parametrize('error', [{'error': 'Timeout'}, {}])
def test_failed_lookup_is_not_reported_as_match(error):
    c = ConceptFactory()
    assert reconcile_domain(snapshot(c), error, apply=True)['outcome'] == 'lookup_failed'
    c.refresh_from_db(); assert c.domain_id == 'Measurement'


@pytest.mark.parametrize('change', ['domain', 'identity', 'source', 'standard', 'deleted'])
def test_concurrent_change_is_preserved(change):
    DomainFactory(domain_id='Condition'); c = ConceptFactory(); old = snapshot(c)
    if change == 'domain': c.domain = DomainFactory(domain_id='Observation')
    elif change == 'identity': c.concept_code = 'changed'
    elif change == 'source': c.source = 'HealthKey'
    elif change == 'standard': c.standard_concept = None
    if change == 'deleted': c.delete()
    else: c.save()
    result = {'concept': dict(evidence(c)['concept'], concept_id=old['concept_id'], concept_code=old['concept_code'])}
    assert reconcile_domain(old, result, apply=True)['outcome'] == 'changed_during_lookup'


def test_command_dry_run_then_apply_uses_web_once_per_distinct_concept(tmp_path):
    DomainFactory(domain_id='Condition'); c = ConceptFactory()
    mapping(c); mapping(c, code='duplicate')
    path = tmp_path / 'audit.csv'
    with patch('omop_core.management.commands.reconcile_athena_domains.AthenaDomainBrowser') as browser:
        browser.return_value.lookup_many.return_value = {c.pk: evidence(c)}
        output = StringIO()
        call_command('reconcile_athena_domains', 'ICD10', report=str(path), stdout=output)
        assert 'dry run' in output.getvalue()
        browser.return_value.lookup_many.assert_called_once_with([c.pk])
        c.refresh_from_db(); assert c.domain_id == 'Measurement'
        with path.open() as stream: records = list(csv.DictReader(stream))
        assert len(records) == 1 and records[0]['outcome'] == 'would_update'
        call_command('reconcile_athena_domains', 'ICD10', apply=True, stdout=StringIO())
        c.refresh_from_db(); assert c.domain_id == 'Condition'


def test_command_preserves_local_concept_without_requesting_athena():
    c = ConceptFactory(source='HealthKey'); mapping(c)
    with patch('omop_core.management.commands.reconcile_athena_domains.AthenaDomainBrowser') as browser:
        call_command('reconcile_athena_domains', 'ICD10', apply=True, stdout=StringIO())
        browser.return_value.lookup_many.assert_not_called()
    c.refresh_from_db(); assert c.domain_id == 'Measurement'


def test_partial_lookup_failure_is_reported_and_returns_failure(tmp_path):
    DomainFactory(domain_id='Condition'); good, bad = ConceptFactory(), ConceptFactory()
    mapping(good); mapping(bad, code='bad')
    path = tmp_path / 'report.csv'
    with patch('omop_core.management.commands.reconcile_athena_domains.AthenaDomainBrowser') as browser:
        browser.return_value.lookup_many.return_value = {good.pk: evidence(good), bad.pk: {'error': 'HTTP 403'}}
        with pytest.raises(CommandError, match='1 destinations remain unresolved'):
            call_command('reconcile_athena_domains', 'ICD10', apply=True, report=str(path), stdout=StringIO())
    good.refresh_from_db(); bad.refresh_from_db()
    assert good.domain_id == 'Condition' and bad.domain_id == 'Measurement'
    with path.open() as stream: assert {r['outcome'] for r in csv.DictReader(stream)} == {'updated', 'lookup_failed'}


def test_unknown_vocabulary_fails_without_browser():
    with patch('omop_core.management.commands.reconcile_athena_domains.AthenaDomainBrowser') as browser:
        with pytest.raises(CommandError, match='No source mappings'):
            call_command('reconcile_athena_domains', 'typo', stdout=StringIO())
        browser.assert_not_called()
