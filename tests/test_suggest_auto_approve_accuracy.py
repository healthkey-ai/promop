"""What curators make of the suggestions Suggest approved by itself (#2856).

An auto-approved row is approved by a machine, so ``suggestion_outcome`` (the
curator's first disposition) stays blank until a person reviews it. The
``suggestion_auto_approved`` column keeps the fact where the accuracy figures can
read it: accepted is an auto-approval a curator confirmed, overridden or rejected
one that was wrong. The model's own precision, recall and F1 count these rows as
they always counted them.
"""
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.mapping import suggestions
from omop_core.mapping.suggestions import archive_suggestion_review
from omop_core.models import MappingSuggestionReview, SourceCodeConceptMapping
from omop_core.services.field_curation_transfer import apply_payload, read_payload
# The fixtures and helpers the auto-approval tests already share.
from tests.test_suggest_auto_approve import (  # noqa: F401
    pick, queue_row, rank_as, run, staff,
)
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db

ACCURACY = '/api/v1/code-mappings/accuracy/'
DASHBOARD = '/api/v1/code-mappings/accuracy/dashboard/'
ANALYTICS = '/api/v1/code-mappings/accuracy/analytics/'
AUTO_ORIGIN = 'suggest v0.4 auto-approved 100%'


@pytest.fixture
def client(staff):
    api = APIClient()
    api.force_authenticate(staff)
    return api


@pytest.fixture
def goal():
    return ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC', concept_name='Albumin goal')


def row(code, target, **overrides):
    """A row a forward Suggest run answered at v0.4 (a proposal unless told otherwise)."""
    return SourceCodeConceptMapping.objects.create(**{
        'source_vocabulary_id': 'VendorLab', 'source_code': code, 'source_code_description': 'albumin',
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'status': 'proposed',
        'occurrence_count': 5, 'target_concept': target, 'suggested_target_concept': target,
        'origin_system': 'suggest v0.4', 'suggestion_model_version': 'v0.4',
        'suggestion_confidence': 1.0, **overrides,
    })


def auto(code, target, outcome='', **overrides):
    """An auto-approved row, with *outcome* the first disposition a curator gave it."""
    return row(code, target, status='approved', origin_system=AUTO_ORIGIN,
               suggestion_auto_approved=True, suggestion_outcome=outcome, **overrides)


def scores(data):
    return {key: data[key] for key in
            ('suggestions', 'reviewed', 'accepted', 'overridden', 'rejected', 'precision', 'recall', 'f1')}


# --- the flag is written where the approval is, and follows the suggestion --------

def test_an_auto_approval_marks_the_row_and_a_proposal_that_stays_proposed_is_not_marked(
    monkeypatch, pick, staff,
):
    approved = queue_row(source_code='A')
    rank_as(monkeypatch, pick, 1.0)
    run(staff, 100)
    approved.refresh_from_db()
    assert approved.status == 'approved'
    assert approved.suggestion_auto_approved is True
    assert approved.suggestion_outcome == '', 'the curator disposition is still to come'

    stays = queue_row(source_code='B')
    rank_as(monkeypatch, pick, 0.95)
    run(staff, 100)
    stays.refresh_from_db()
    assert (stays.status, stays.suggestion_auto_approved) == ('proposed', False)


def test_a_curators_own_approval_is_not_an_auto_approval(client, pick):
    mapping = queue_row(source_code='HAND')

    client.patch(f'/api/v1/code-mappings/{mapping.pk}/',
                 {'destination_concept_id': pick.pk, 'status': 'approved'}, format='json')

    mapping.refresh_from_db()
    assert mapping.status == 'approved'
    assert mapping.suggestion_auto_approved is False


def test_a_curator_reviewing_an_auto_approval_keeps_the_mark_and_records_a_first_disposition(
    monkeypatch, client, pick, staff,
):
    other = ConceptFactory(domain_id='Condition', vocabulary_id='SNOMED', concept_name='Other pick')
    kept = queue_row(source_code='KEPT')
    changed = queue_row(source_code='CHANGED')
    rank_as(monkeypatch, pick, 1.0)
    run(staff, 100)

    # The documented undo: back to proposed, then decided again.
    for mapping in (kept, changed):
        assert client.patch(f'/api/v1/code-mappings/{mapping.pk}/', {'status': 'proposed'}, format='json').status_code == 200
    assert client.patch(f'/api/v1/code-mappings/{kept.pk}/', {'status': 'approved'}, format='json').status_code == 200
    assert client.patch(f'/api/v1/code-mappings/{changed.pk}/',
                        {'destination_concept_id': other.pk, 'status': 'approved'}, format='json').status_code == 200

    kept.refresh_from_db()
    changed.refresh_from_db()
    assert (kept.suggestion_outcome, kept.suggestion_auto_approved) == ('accepted', True)
    assert (changed.suggestion_outcome, changed.suggestion_auto_approved) == ('overridden', True)


def test_a_new_suggestion_clears_the_mark_and_the_archive_keeps_it_with_the_review(monkeypatch, pick):
    where = dict(source_vocabulary_id='ICD10', domain_id='Condition', omop_table='condition')
    reviewed = row('REVIEWED', pick, suggestion_auto_approved=True, suggestion_outcome='rejected',
                   origin_system=AUTO_ORIGIN, **where)
    unreviewed = row('UNREVIEWED', pick, suggestion_auto_approved=True, origin_system=AUTO_ORIGIN, **where)
    rank_as(monkeypatch, pick, 0.5)

    suggestions.suggest_mappings('condition', strategies=['umls'], min_occurrences=1, resuggest=True)

    reviewed.refresh_from_db()
    unreviewed.refresh_from_db()
    assert (reviewed.suggestion_auto_approved, reviewed.suggestion_outcome) == (False, '')
    review = MappingSuggestionReview.objects.get(mapping=reviewed)
    assert (review.suggestion_outcome, review.suggestion_auto_approved) == ('rejected', True)
    assert unreviewed.suggestion_auto_approved is False, 'the next suggestion was not auto-approved'
    assert not MappingSuggestionReview.objects.filter(mapping=unreviewed).exists()


def test_the_archive_copies_the_mark_and_an_unmarked_review_stays_unmarked(pick):
    marked = row('M', pick, suggestion_auto_approved=True, suggestion_outcome='accepted')
    plain = row('P', pick, suggestion_outcome='accepted')

    archive_suggestion_review(marked)
    archive_suggestion_review(plain)

    assert MappingSuggestionReview.objects.get(mapping=marked).suggestion_auto_approved is True
    assert MappingSuggestionReview.objects.get(mapping=plain).suggestion_auto_approved is False


# --- the backfill ----------------------------------------------------------------

def test_the_backfill_marks_what_suggest_auto_approved_and_nothing_else(pick):
    marked = [
        row('ORIGIN', pick, status='approved', origin_system=AUTO_ORIGIN),
        row('ORIGIN-UPPER', pick, status='approved', origin_system='Suggest v0.4 auto-approved 95%'),
        row('REVIEWED', pick, status='approved', origin_system=AUTO_ORIGIN, suggestion_outcome='overridden'),
        # A curator's save replaced the stamp; the note Suggest appended is what is left.
        row('NOTE', pick, status='approved', origin_system='curator@example.test',
            notes='Jev selected this. Auto-approved by Suggest: 100% confidence, threshold 100%.'),
    ]
    left = [
        row('PLAIN', pick),
        row('CURATOR', pick, status='approved', origin_system='curator@example.test'),
        # Not Suggest's provenance, whatever else it says.
        row('IMPORT', pick, status='approved', origin_system='HT-One auto-approved 100%'),
        row('FALLBACK', pick, origin_system=AUTO_ORIGIN, suggestion_fallback=True),
        # Curator text quoting the phrase, with no suggestion behind it.
        row('QUOTED', None, notes='Auto-approved by Suggest: 100% (copied from elsewhere)', origin_system=''),
    ]
    before = {m.pk: m.updated_at for m in marked}

    migration = import_module('omop_core.migrations.0373_backfill_suggestion_auto_approved')
    migration.mark_auto_approved(apps, None)
    migration.mark_auto_approved(apps, None)   # a replay changes nothing

    for mapping in marked:
        mapping.refresh_from_db()
        assert mapping.suggestion_auto_approved is True, mapping.source_code
        assert mapping.updated_at == before[mapping.pk], 'not a change a reader of the list is sent'
    for mapping in left:
        mapping.refresh_from_db()
        assert mapping.suggestion_auto_approved is False, mapping.source_code
    migration.unmark_auto_approved(apps, None)


# --- the accuracy endpoints --------------------------------------------------------

def test_the_scores_do_not_change_when_unreviewed_auto_approvals_are_added(client, goal, pick):
    row('KEPT', goal, status='approved', suggestion_outcome='accepted')
    row('MOVED', goal, status='approved', suggestion_outcome='overridden')
    row('REJECTED', goal, status='rejected', suggestion_outcome='rejected')
    before_dashboard = client.get(DASHBOARD).data
    before_accuracy = client.get(ACCURACY).data

    for index in range(3):
        auto(f'AUTO-{index}', pick)

    dashboard = client.get(DASHBOARD).data
    accuracy = client.get(ACCURACY).data
    # Counted in the suggestions as they always were (an unreviewed row is one), and in no score.
    assert dashboard['overall']['suggestions'] == before_dashboard['overall']['suggestions'] + 3
    for key in ('reviewed', 'accepted', 'overridden', 'rejected', 'precision', 'recall', 'f1'):
        assert dashboard['overall'][key] == before_dashboard['overall'][key], key
        assert dashboard['models'][0][key] == before_dashboard['models'][0][key], key
        assert accuracy['overall'][key] == before_accuracy['overall'][key], key
    assert dashboard['overall']['precision'] == pytest.approx(1 / 3)
    assert dashboard['overall']['auto_approved']['total'] == 3


def test_a_reviewed_auto_approval_is_scored_as_any_reviewed_suggestion_and_counted_apart(client, goal, pick):
    auto('CONFIRMED', pick, 'accepted')
    auto('OVERRIDDEN', pick, 'overridden')
    auto('REJECTED', pick, 'rejected')
    auto('UNREVIEWED', pick)
    row('PLAIN', goal, status='approved', suggestion_outcome='accepted')

    data = client.get(DASHBOARD).data

    assert scores(data['overall']) == {
        'suggestions': 5, 'reviewed': 4, 'accepted': 2, 'overridden': 1, 'rejected': 1,
        'precision': 0.5, 'recall': 2 / 3, 'f1': pytest.approx(4 / 7),
    }
    expected = {
        'total': 4, 'reviewed': 3, 'accepted': 1, 'overridden': 1, 'rejected': 1,
        'unreviewed': 1, 'precision_of_reviewed': pytest.approx(1 / 3),
    }
    assert data['overall']['auto_approved'] == expected
    [model] = data['models']
    assert model['auto_approved'] == expected


def test_an_auto_approval_nobody_reviewed_has_no_precision_to_report(client, pick):
    auto('ONE', pick)

    auto_approved = client.get(DASHBOARD).data['overall']['auto_approved']

    assert auto_approved == {
        'total': 1, 'reviewed': 0, 'accepted': 0, 'overridden': 0, 'rejected': 0,
        'unreviewed': 1, 'precision_of_reviewed': None,
    }


def test_no_auto_approvals_is_a_block_of_zeros_not_a_missing_one(client, goal):
    row('PLAIN', goal, status='approved', suggestion_outcome='accepted')

    data = client.get(DASHBOARD).data

    assert data['overall']['auto_approved']['total'] == 0
    assert data['overall']['auto_approved']['precision_of_reviewed'] is None
    assert client.get(ACCURACY).data['overall']['auto_approved']['total'] == 0


def test_a_fallback_is_never_an_auto_approval_in_the_figures(client, pick):
    auto('REAL', pick, 'accepted')
    auto('FALLBACK', pick, 'rejected', suggestion_fallback=True)

    data = client.get(DASHBOARD).data

    assert data['overall']['auto_approved']['total'] == 1
    assert data['overall']['auto_approved']['rejected'] == 0
    assert data['fallbacks'] == {'suggestions': 1, 'reviewed': 1}


def test_a_review_archived_with_its_suggestion_still_counts(client, pick):
    current = auto('NOW', pick, 'accepted')
    MappingSuggestionReview.objects.create(
        mapping=current, source_vocabulary_id='VendorLab', source_code='OLD',
        suggested_target_concept_id=pick.pk, suggestion_model_version='v0.4',
        suggestion_outcome='rejected', suggestion_auto_approved=True)
    MappingSuggestionReview.objects.create(
        mapping=current, source_vocabulary_id='VendorLab', source_code='OLD-PLAIN',
        suggested_target_concept_id=pick.pk, suggestion_model_version='v0.4',
        suggestion_outcome='rejected')

    auto_approved = client.get(DASHBOARD).data['overall']['auto_approved']

    assert (auto_approved['total'], auto_approved['accepted'], auto_approved['rejected']) == (2, 1, 1)


def test_each_snapshot_of_the_accuracy_endpoint_carries_its_own_block(client, pick):
    auto('A', pick, 'accepted', source_vocabulary_id='VendorLab')
    auto('B', pick, 'overridden', source_vocabulary_id='OtherLab')
    auto('C', pick, source_vocabulary_id='OtherLab')

    data = client.get(ACCURACY).data

    assert data['overall']['auto_approved']['total'] == 3
    by_source = data['by_source_vocabulary']
    assert by_source['VendorLab']['auto_approved'] == {
        'total': 1, 'reviewed': 1, 'accepted': 1, 'overridden': 0, 'rejected': 0,
        'unreviewed': 0, 'precision_of_reviewed': 1.0,
    }
    assert by_source['OtherLab']['auto_approved']['total'] == 2
    assert by_source['OtherLab']['auto_approved']['unreviewed'] == 1


# --- the analytics page's block ---------------------------------------------------

def test_the_analytics_block_is_by_stored_confidence_and_leaves_the_surface_tables_alone(client, pick):
    auto('HI-KEPT', pick, 'accepted', suggestion_confidence=1.0)
    auto('HI-WRONG', pick, 'rejected', suggestion_confidence=0.95)
    auto('HI-NEW', pick, suggestion_confidence=0.9)
    auto('MID-WRONG', pick, 'overridden', suggestion_confidence=0.85)
    auto('LOW', pick, suggestion_confidence=0.5)
    auto('FALLBACK', pick, 'rejected', suggestion_confidence=1.0, suggestion_fallback=True)
    row('NOT-AUTO', pick, status='approved', suggestion_outcome='accepted')
    before = client.get(ANALYTICS).data['sections']

    data = client.get(ANALYTICS).data

    block = data['auto_approved']
    assert (block['key'], block['label']) == ('auto_approved', 'Auto-approved by Suggest')
    by_key = {r['key']: r for r in block['rows']}
    assert list(by_key) == ['overall', '90-100', '80-90', '70-80', 'below-70']
    assert by_key['overall'] == {
        'key': 'overall', 'label': 'Overall', 'total': 5, 'reviewed': 3, 'confirmed': 1,
        'reversed': 2, 'unreviewed': 2, 'precision_of_reviewed': pytest.approx(1 / 3)}
    assert (by_key['90-100']['total'], by_key['90-100']['confirmed'], by_key['90-100']['reversed'],
            by_key['90-100']['unreviewed']) == (3, 1, 1, 1)
    assert by_key['90-100']['precision_of_reviewed'] == 0.5
    assert (by_key['80-90']['total'], by_key['80-90']['reversed']) == (1, 1)
    assert by_key['70-80']['total'] == 0 and by_key['70-80']['precision_of_reviewed'] is None
    assert (by_key['below-70']['total'], by_key['below-70']['unreviewed']) == (1, 1)
    # Auto-approvals are not curator decisions: no surface table gains a row for them.
    assert data['sections'] == before
    assert all(r['total'] == 0 for section in data['sections'] for r in section['rows'])


# --- copying reference data ---------------------------------------------------------

def test_the_mark_travels_with_a_copied_code_mapping_and_an_older_payload_reads_false(pick):
    row('MARKED', pick, status='approved', origin_system=AUTO_ORIGIN, suggestion_auto_approved=True,
        suggestion_outcome='accepted')
    row('PLAIN', pick, status='approved')
    payload = read_payload('default', tables=('code_mappings',))
    by_code = {r['source_code']: r for r in payload['code_mappings']}
    assert by_code['MARKED']['suggestion_auto_approved'] is True
    assert by_code['PLAIN']['suggestion_auto_approved'] is False

    SourceCodeConceptMapping.objects.all().delete()
    apply_payload(payload, tables=('code_mappings',))
    assert SourceCodeConceptMapping.objects.get(source_code='MARKED').suggestion_auto_approved is True
    assert SourceCodeConceptMapping.objects.get(source_code='PLAIN').suggestion_auto_approved is False

    # A payload written before the column has no key: it reads as False, and a copy
    # overwrites the mark the target had, as it does the outcome it explains.
    for entry in payload['code_mappings']:
        del entry['suggestion_auto_approved']
    apply_payload(payload, tables=('code_mappings',))
    assert not SourceCodeConceptMapping.objects.filter(suggestion_auto_approved=True).exists()
    SourceCodeConceptMapping.objects.all().delete()
    apply_payload(payload, tables=('code_mappings',))
    assert SourceCodeConceptMapping.objects.count() == 2
    assert not SourceCodeConceptMapping.objects.filter(suggestion_auto_approved=True).exists()


# --- cost ----------------------------------------------------------------------------

def _queries(client, url):
    with CaptureQueriesContext(connection) as captured:
        assert client.get(url).status_code == 200
    return len(captured)


@pytest.mark.parametrize('url', [ACCURACY, DASHBOARD, ANALYTICS])
def test_the_number_of_queries_does_not_grow_with_the_rows(client, pick, url):
    for index in range(3):
        auto(f'SMALL-{index}', pick, ('', 'accepted', 'rejected')[index], decision_surface='concept2code',
             decision_confidence=0.95)
    small = _queries(client, url)

    for index in range(30):
        auto(f'BIG-{index}', pick, ('', 'accepted', 'overridden', 'rejected')[index % 4],
             decision_surface=('concept2code', 'code2concept')[index % 2], decision_confidence=0.85,
             suggestion_confidence=(1.0, 0.85, 0.5)[index % 3])

    assert _queries(client, url) == small
