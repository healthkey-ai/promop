"""Live Athena domain corrections preserve everything except Concept.domain_id."""
import csv
import sys
import types
from contextlib import contextmanager
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import Concept, MappingDestinationCandidate, SourceCodeConceptMapping as Mapping
from omop_core.services.athena_domain_reconciliation import (
    AthenaDomainBrowser, destination_snapshots, reconcile_domain,
)
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
    ({'domain_id': 'Procedure'}, 'missing_domain'),
    ({'domain_id': 'UninstalledDomain'}, 'unsupported_domain'),
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


# ── AthenaDomainBrowser.lookup_many ─────────────────────────────────────────
# Every test above hands reconcile_domain a `{'concept': {...}}` built by hand,
# and the four command tests replace AthenaDomainBrowser with a mock — so the
# class this module adds is never executed. concept_from_api and
# BrowserTransport.get are covered on their own in tests/test_athena_destinations
# .py; what is not covered is the wiring between them and reconcile_domain: the
# seconds-to-milliseconds timeout conversion, closing the browser on the success
# path, a failed page producing no evidence rather than a false match, and the
# mapped payload actually satisfying reconcile_domain's identity checks.

ATHENA_TERM_RESPONSE = {
    'id': 4193704, 'name': 'Type 2 diabetes mellitus', 'domainId': 'Condition',
    'vocabularyId': 'SNOMED', 'conceptClassId': 'Clinical Finding',
    'conceptCode': '44054006', 'standardConcept': 'Standard', 'invalidReason': 'Valid',
    'validStart': 662688000000, 'validEnd': 32503680000000,
}


class FakeResponse:
    def __init__(self, url, status, payload):
        self.url, self.status, self._payload = url, status, payload

    def json(self):
        return self._payload


class _Pending:
    """Playwright's pending-event handle: .value is only readable after the block."""

    def __init__(self):
        self.resolved, self._value = False, None

    @property
    def value(self):
        if not self.resolved:
            raise AssertionError('response is not available until the block exits')
        return self._value

    def resolve(self, response):
        self._value, self.resolved = response, True


class FakePage:
    """Enough of the Playwright page API for BrowserTransport.get."""

    def __init__(self, payloads, status=200):
        self.payloads, self.status = payloads, status
        self.visited, self.timeout = [], None

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    @contextmanager
    def expect_response(self, predicate):
        # Playwright raises if .value is read before the event arrives; a fake
        # that returns None instead would accept a refactor the real API rejects.
        pending = _Pending()
        self._predicate, self._pending = predicate, pending
        yield pending
        assert pending.resolved, 'goto() was never called inside expect_response()'

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        concept_id = int(url.rsplit('/', 1)[-1])
        response = FakeResponse(f'https://athena.ohdsi.org/api/v1/concepts/{concept_id}',
                                self.status, self.payloads[concept_id])
        # The transport only takes the response its own predicate accepts.
        assert self._predicate(response), 'transport predicate rejected the concept response'
        self._pending.resolve(response)


@contextmanager
def fake_playwright(page):
    """Install a fake playwright.sync_api so lookup_many runs without a browser."""
    browser = types.SimpleNamespace(
        new_context=lambda storage_state=None: types.SimpleNamespace(new_page=lambda: page),
        close=lambda: closed.append(True),
    )
    closed = []
    chromium = types.SimpleNamespace(launch=lambda headless=True, executable_path=None: browser)

    @contextmanager
    def sync_playwright():
        yield types.SimpleNamespace(chromium=chromium)

    module = types.ModuleType('playwright.sync_api')
    module.sync_playwright = sync_playwright
    package = types.ModuleType('playwright')
    package.sync_api = module
    with patch.dict(sys.modules, {'playwright': package, 'playwright.sync_api': module}):
        yield closed


def test_lookup_many_maps_the_live_athena_payload_and_closes_the_browser():
    page = FakePage({4193704: ATHENA_TERM_RESPONSE})
    with fake_playwright(page) as closed:
        results = AthenaDomainBrowser(interval=0, timeout=5).lookup_many([4193704])

    assert results == {4193704: {'concept': {
        'concept_id': 4193704, 'concept_name': 'Type 2 diabetes mellitus',
        'domain_id': 'Condition', 'vocabulary_id': 'SNOMED',
        'concept_class_id': 'Clinical Finding', 'concept_code': '44054006',
        'standard_concept': 'S', 'invalid_reason': None,
        'valid_start_date': '1991-01-01', 'valid_end_date': '3000-01-01',
    }}}
    assert page.visited == ['https://athena.ohdsi.org/search-terms/terms/4193704']
    assert page.timeout == 5000
    assert closed == [True], 'the browser must close even on the success path'


def test_lookup_many_records_a_failure_without_inventing_evidence():
    page = FakePage({4193704: ATHENA_TERM_RESPONSE}, status=503)
    with fake_playwright(page):
        results = AthenaDomainBrowser(interval=0, timeout=5).lookup_many([4193704])
    assert 'concept' not in results[4193704]
    assert 'LookupFailure' in results[4193704]['error'] and '503' in results[4193704]['error']

    # A payload missing a key concept_from_api requires is a failure, not a match.
    incomplete = dict(ATHENA_TERM_RESPONSE)
    del incomplete['domainId']
    page = FakePage({4193704: incomplete})
    with fake_playwright(page):
        results = AthenaDomainBrowser(interval=0, timeout=5).lookup_many([4193704])
    assert 'concept' not in results[4193704]


def test_a_real_lookup_drives_reconcile_domain_end_to_end():
    """The hand-built evidence() above must agree with what lookup_many produces."""
    DomainFactory(domain_id='Condition')
    concept = ConceptFactory(concept_id=4193704, concept_code='44054006',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Observation'))
    mapping(concept)
    page = FakePage({4193704: ATHENA_TERM_RESPONSE})
    with fake_playwright(page):
        results = AthenaDomainBrowser(interval=0, timeout=5).lookup_many([concept.pk])

    result = reconcile_domain(snapshot(concept), results[concept.pk], apply=True)
    assert result['outcome'] == 'updated'
    concept.refresh_from_db()
    assert concept.domain_id == 'Condition'


# ── Domains the rest of the pipeline cannot route ───────────────────────────

@pytest.mark.parametrize('domain', ['Device', 'Meas Value', 'Spec Anatomic Site'])
@pytest.mark.parametrize('apply', [False, True])
def test_a_domain_outside_the_routed_set_is_reported_not_written(domain, apply):
    DomainFactory(domain_id=domain)
    concept = ConceptFactory(concept_id=9871235, concept_code='outside-routing',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Observation'))
    mapping(concept)
    unroutable = {'concept': {**evidence(concept)['concept'], 'domain_id': domain}}
    result = reconcile_domain(snapshot(concept), unroutable, apply=apply)
    assert result['outcome'] == 'unsupported_domain'
    assert domain in result['reason']
    concept.refresh_from_db()
    assert concept.domain_id == 'Observation', 'an unroutable domain must never be written'


def test_a_correction_into_a_routed_domain_still_applies_from_an_unroutable_one():
    """The guard is on the destination domain, not on the domain being corrected."""
    DomainFactory(domain_id='Condition')
    concept = ConceptFactory(concept_id=9871236, concept_code='from-device',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Device'))
    assert reconcile_domain(snapshot(concept), evidence(concept), apply=True)['outcome'] == 'updated'


# ── The mappings a correction leaves out of step ────────────────────────────

def test_the_receipt_names_the_mappings_the_correction_leaves_stale():
    DomainFactory(domain_id='Condition')
    concept = ConceptFactory(concept_id=9871237, concept_code='stale-mappings',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Observation'))
    diverging = mapping(concept)
    diverging.domain_id, diverging.omop_table = 'Observation', 'observation'
    diverging.save()
    agreeing = mapping(concept, code='already-condition')
    agreeing.domain_id, agreeing.omop_table = 'Condition', 'condition'
    agreeing.save()
    unset = mapping(concept, code='no-domain-yet')

    result = reconcile_domain(snapshot(concept), evidence(concept), apply=True)

    assert result['outcome'] == 'updated'
    assert result['stale_mappings'] == str(diverging.pk), (
        'only the mapping whose own domain_id now contradicts the concept is reported'
    )
    assert str(agreeing.pk) not in result['stale_mappings']
    assert str(unset.pk) not in result['stale_mappings']
    # The correction still does not touch the mapping rows themselves.
    diverging.refresh_from_db()
    assert (diverging.domain_id, diverging.omop_table) == ('Observation', 'observation')


def test_a_truncated_stale_mapping_list_says_so():
    from omop_core.services import athena_domain_reconciliation as service
    DomainFactory(domain_id='Condition')
    concept = ConceptFactory(concept_id=9871238, concept_code='many-mappings',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Observation'))
    for index in range(4):
        row = mapping(concept, code=f'code-{index}')
        row.domain_id, row.omop_table = 'Observation', 'observation'
        row.save()

    with patch.object(service, 'STALE_MAPPING_LIMIT', 2):
        result = reconcile_domain(snapshot(concept), evidence(concept), apply=True)

    assert result['stale_mappings'].endswith(' +2 more'), result['stale_mappings']
    assert len(result['stale_mappings'].split()[:2]) == 2
