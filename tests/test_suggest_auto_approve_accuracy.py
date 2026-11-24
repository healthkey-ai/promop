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
from omop_core.models import MappingSuggestionReview, SourceCodeConceptMapping, SuggestRun
from omop_core.services.group_mapping_jobs import _write_group_suggestion
from omop_core.services import mapping_removal
from omop_core.services.mapping_browse import visible_rows
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
    """An auto-approved row, with *outcome* the first disposition a curator gave it.

    ``auto_approved_confidence`` is the score the approval saw (1.0 unless given);
    ``suggestion_confidence`` is the live one and may be set apart from it.
    """
    overrides.setdefault('auto_approved_confidence', overrides.get('suggestion_confidence', 1.0))
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


# --- a person changing an auto-approval in place is a review (#2856) --------------

@pytest.fixture
def other():
    return ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC', concept_name='Other albumin')


def detail(mapping):
    return f'/api/v1/code-mappings/{mapping.pk}/'


def test_moving_an_approved_auto_approval_to_another_concept_is_an_override(client, goal, other):
    mapping = auto('MOVE', goal)

    response = client.patch(detail(mapping), {'destination_concept_id': other.pk}, format='json')

    assert response.status_code == 200, response.data
    mapping.refresh_from_db()
    assert (mapping.status, mapping.target_concept_id) == ('approved', other.pk)
    assert (mapping.suggestion_outcome, mapping.suggestion_auto_approved) == ('overridden', True)
    assert mapping.suggested_target_concept_id == goal.pk, 'the suggestion stays what the model said'


def test_rejecting_an_approved_auto_approval_directly_is_a_rejection(client, goal):
    mapping = auto('REJECT', goal)

    response = client.patch(detail(mapping), {'status': 'rejected'}, format='json')

    assert response.status_code == 200, response.data
    mapping.refresh_from_db()
    assert (mapping.status, mapping.suggestion_outcome) == ('rejected', 'rejected')


def test_removing_the_destination_of_an_auto_approval_is_an_override(staff, goal):
    mapping = auto('CLEAR', goal)

    mapping_removal.clear_destination(mapping, staff)

    mapping.refresh_from_db()
    assert mapping.target_concept_id is None
    assert (mapping.suggestion_outcome, mapping.suggestion_auto_approved) == ('overridden', True)
    assert mapping.auto_approved_confidence == 1.0, 'kept with the flag, as the suggestion is'
    assert mapping.suggestion_confidence is None


def test_clear_destination_records_nothing_for_a_row_that_is_not_an_unreviewed_auto_approval(staff, goal):
    plain = row('PLAIN', goal, status='approved')
    reviewed = auto('REVIEWED', goal, 'accepted')
    proposed = row('PROPOSED', goal, suggestion_auto_approved=True)   # un-approved already

    for mapping in (plain, reviewed, proposed):
        mapping_removal.clear_destination(mapping, staff)
        mapping.refresh_from_db()

    assert (plain.suggestion_outcome, reviewed.suggestion_outcome, proposed.suggestion_outcome) == (
        '', 'accepted', '')


def test_a_silent_re_save_records_no_acceptance_and_a_set_outcome_is_immutable(client, goal, other):
    silent = auto('SILENT', goal)
    reviewed = auto('REVIEWED', goal, 'accepted')

    assert client.patch(detail(silent), {'notes': 'checked'}, format='json').status_code == 200
    assert client.patch(detail(silent), {'destination_concept_id': goal.pk, 'status': 'approved'},
                        format='json').status_code == 200
    assert client.patch(detail(reviewed), {'destination_concept_id': other.pk}, format='json').status_code == 200

    silent.refresh_from_db()
    reviewed.refresh_from_db()
    assert silent.suggestion_outcome == ''
    assert reviewed.suggestion_outcome == 'accepted', 'the first disposition is never rewritten'


def test_an_approved_row_that_was_not_auto_approved_is_unchanged(client, goal, other):
    mapping = row('HAND', goal, status='approved', origin_system='curator@example.test')

    assert client.patch(detail(mapping), {'destination_concept_id': other.pk}, format='json').status_code == 200

    mapping.refresh_from_db()
    assert mapping.suggestion_outcome == '', 'only a proposal that was un-approved first has ever recorded one'


def test_a_machine_write_is_no_review(staff, goal, other):
    from patient_portal.api.views import _upsert_source_code_mapping

    for code, kwargs in (
        ('NOT-RECORDED', {'record_decision': False}),
        ('PROPOSAL', {'machine_proposal': True}),
    ):
        mapping = auto(code, goal)
        _upsert_source_code_mapping(other, {'status': 'approved'}, staff, mapping=mapping, **kwargs)
        mapping.refresh_from_db()
        assert mapping.suggestion_outcome == '', code


# --- the approval-time confidence ---------------------------------------------------

def test_approval_stores_the_score_it_saw_and_a_later_save_does_not_move_the_band(
    monkeypatch, client, pick, staff,
):
    queued = queue_row(source_code='BAND')
    rank_as(monkeypatch, pick, 1.0)
    run(staff, 90)
    queued.refresh_from_db()
    assert (queued.suggestion_auto_approved, queued.auto_approved_confidence) == (True, 1.0)

    # A curator's save that moves the destination clears the live confidence.
    moved = ConceptFactory(domain_id='Condition', vocabulary_id='SNOMED', concept_name='Moved')
    assert client.patch(detail(queued), {'destination_concept_id': moved.pk}, format='json').status_code == 200
    queued.refresh_from_db()
    assert queued.suggestion_confidence is None
    assert queued.suggestion_outcome == 'overridden'

    by_key = {r['key']: r for r in client.get(ANALYTICS).data['auto_approved']['rows']}
    assert (by_key['90-100']['total'], by_key['90-100']['reversed']) == (1, 1)
    assert by_key['overall']['total'] == 1


def test_a_row_with_no_approval_time_confidence_is_in_overall_and_in_no_band(client, pick):
    auto('NULL', pick, auto_approved_confidence=None, suggestion_confidence=None)
    # Auto-approved, then its live confidence changed: the band is the approval's.
    auto('LIVE-LOW', pick, auto_approved_confidence=0.95, suggestion_confidence=0.2)

    by_key = {r['key']: r for r in client.get(ANALYTICS).data['auto_approved']['rows']}

    assert by_key['overall']['total'] == 2
    assert by_key['90-100']['total'] == 1
    assert by_key['below-70']['total'] == 0


def test_the_archive_carries_the_approval_time_confidence(pick):
    reviewed = row('R', pick, suggestion_auto_approved=True, auto_approved_confidence=0.93,
                   suggestion_outcome='overridden')

    archive_suggestion_review(reviewed)

    assert MappingSuggestionReview.objects.get(mapping=reviewed).auto_approved_confidence == 0.93


def test_a_forward_run_clears_the_approval_time_confidence_with_the_flag(monkeypatch, pick):
    where = dict(source_vocabulary_id='ICD10', domain_id='Condition', omop_table='condition')
    mapping = row('F', pick, suggestion_auto_approved=True, auto_approved_confidence=0.93,
                  origin_system=AUTO_ORIGIN, **where)
    rank_as(monkeypatch, pick, 0.5)

    suggestions.suggest_mappings('condition', strategies=['umls'], min_occurrences=1, resuggest=True)

    mapping.refresh_from_db()
    assert (mapping.suggestion_auto_approved, mapping.auto_approved_confidence) == (False, None)


def test_the_confidence_travels_with_a_copied_mapping_and_an_older_payload_reads_null(pick):
    row('MARKED', pick, status='approved', suggestion_auto_approved=True, auto_approved_confidence=0.97)
    payload = read_payload('default', tables=('code_mappings',))
    assert payload['code_mappings'][0]['auto_approved_confidence'] == 0.97

    SourceCodeConceptMapping.objects.all().delete()
    apply_payload(payload, tables=('code_mappings',))
    assert SourceCodeConceptMapping.objects.get().auto_approved_confidence == 0.97

    del payload['code_mappings'][0]['auto_approved_confidence']
    SourceCodeConceptMapping.objects.all().delete()
    apply_payload(payload, tables=('code_mappings',))
    assert SourceCodeConceptMapping.objects.get().auto_approved_confidence is None


# --- group Suggest and the reverse restamp replace the suggestion ------------------

def _group_write(members, concept, curator):
    run_row = SuggestRun.objects.create(source_vocabulary_id='VendorLab', total=len(members))
    _write_group_suggestion([m.pk for m in members], {
        'concept': {'concept_id': concept.pk}, 'strategy': 'lexical', 'note': 'ranked', 'confidence': 0.9,
    }, run=run_row, actor=curator)


def test_a_group_suggestion_clears_the_mark_of_an_unreviewed_auto_approval(staff, goal, other):
    member = row('G1', None, locked_by=staff, suggestion_auto_approved=True, auto_approved_confidence=0.95)

    _group_write([member], other, staff)

    member.refresh_from_db()
    assert member.suggested_target_concept_id == other.pk
    assert (member.suggestion_auto_approved, member.auto_approved_confidence) == (False, None)
    assert not MappingSuggestionReview.objects.filter(mapping=member).exists()


def test_a_group_suggestion_archives_a_review_with_its_mark_and_starts_clean(staff, goal, other):
    member = row('G2', None, locked_by=staff, suggestion_auto_approved=True,
                 auto_approved_confidence=0.95, suggestion_outcome='overridden')

    _group_write([member], other, staff)

    member.refresh_from_db()
    assert (member.suggestion_outcome, member.suggestion_auto_approved, member.auto_approved_confidence) == (
        '', False, None)
    review = MappingSuggestionReview.objects.get(mapping=member)
    assert (review.suggestion_outcome, review.suggestion_auto_approved, review.auto_approved_confidence) == (
        'overridden', True, 0.95)


def test_a_reverse_proposal_clears_the_mark_and_the_archive_carries_it(staff, goal, other):
    from patient_portal.api.views import _upsert_source_code_mapping

    unreviewed = row('REV-1', goal, suggestion_auto_approved=True, auto_approved_confidence=0.9)
    reviewed = row('REV-2', goal, suggestion_auto_approved=True, auto_approved_confidence=0.9,
                   suggestion_outcome='rejected')

    for mapping in (unreviewed, reviewed):
        _upsert_source_code_mapping(other, {}, staff, mapping=mapping, record_decision=False,
                                    machine_proposal=True)
        mapping.refresh_from_db()
        assert mapping.suggested_target_concept_id == other.pk
        assert (mapping.suggestion_auto_approved, mapping.auto_approved_confidence) == (False, None)

    assert reviewed.suggestion_outcome == ''
    review = MappingSuggestionReview.objects.get(mapping=reviewed)
    assert (review.suggestion_outcome, review.suggestion_auto_approved, review.auto_approved_confidence) == (
        'rejected', True, 0.9)
    assert not MappingSuggestionReview.objects.filter(mapping=unreviewed).exists()


# --- the list filter and the analytics agree ---------------------------------------

def test_the_list_filter_finds_a_flagged_row_whatever_its_stamp_and_a_stamped_row_the_backfill_missed(goal):
    row('FLAG', goal, status='approved', origin_system='curator@example.test', suggestion_auto_approved=True)
    row('STAMP', goal, status='approved', origin_system=AUTO_ORIGIN)
    row('NEITHER', goal, status='approved', origin_system='curator@example.test')

    found = set(visible_rows(SourceCodeConceptMapping.objects.all(), {'auto_approved': '1'})
                .values_list('source_code', flat=True))

    assert found == {'FLAG', 'STAMP'}


# --- the backfill, narrowed ----------------------------------------------------------

def test_the_backfill_note_branch_skips_a_row_whose_suggestion_was_replaced_and_copies_the_confidence(
    pick, goal,
):
    note = 'Auto-approved by Suggest: 100% confidence, threshold 100%.'
    held = row('HELD', pick, status='approved', origin_system='curator@example.test', notes=note,
               suggestion_confidence=0.96)
    stamped = row('STAMPED', pick, status='approved', origin_system=AUTO_ORIGIN, suggestion_confidence=0.91)
    stamped_cleared = row('STAMPED-CLEARED', pick, status='approved', origin_system=AUTO_ORIGIN,
                          suggestion_confidence=None)
    replaced = row('REPLACED', goal, status='approved', origin_system='curator@example.test', notes=note,
                   target_concept=pick, suggested_target_concept=goal)
    proposed = row('UNAPPROVED', pick, status='proposed', origin_system='curator@example.test', notes=note)
    no_version = row('NO-VERSION', pick, status='approved', origin_system='curator@example.test',
                     notes=note, suggestion_model_version='')

    migration = import_module('omop_core.migrations.0373_backfill_suggestion_auto_approved')
    migration.mark_auto_approved(apps, None)

    for mapping in (held, stamped, stamped_cleared, replaced, proposed, no_version):
        mapping.refresh_from_db()
    assert (held.suggestion_auto_approved, held.auto_approved_confidence) == (True, 0.96)
    assert (stamped.suggestion_auto_approved, stamped.auto_approved_confidence) == (True, 0.91)
    assert (stamped_cleared.suggestion_auto_approved, stamped_cleared.auto_approved_confidence) == (True, None)
    for mapping in (replaced, proposed, no_version):
        assert mapping.suggestion_auto_approved is False, mapping.source_code
        assert mapping.auto_approved_confidence is None
