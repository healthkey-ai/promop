"""What is scored as the ranking model's answer, and what is not (#2743).

Two things were counted against the model that it never said. A fallback, the
first retrieval candidate written because no ranker could be reached, carried
a model version like any suggestion. And a proposal the reverse ranker moved
kept the older forward suggestion as the one to be scored against, so a
curator who approved exactly what the ranker proposed had "overridden" it.
"""
from importlib import import_module

import pytest
from django.apps import apps
from rest_framework.test import APIClient

from omop_core.mapping import suggestions
from omop_core.models import MappingSuggestionReview, SourceCodeConceptMapping, SuggestRun
from omop_core.services import reverse_suggest_jobs
from omop_core.services.group_mapping_jobs import _write_group_suggestion
from patient_portal.models import Identity
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db

DASHBOARD = '/api/v1/code-mappings/accuracy/dashboard/'
ANALYTICS = '/api/v1/code-mappings/accuracy/analytics/'
# What rank_candidates leaves when it falls back to the first candidate.
FALLBACK_NOTE = (
    'Best-match fallback (score 0.665). Ranking model unavailable, so this is the first '
    'candidate in retrieval fallback order. Retrieval scores do not establish clinical '
    'compatibility — review carefully. Details: JEV_API_KEY is not configured.'
)
# What it leaves when it may not fall back: nothing was chosen.
UNRESOLVED_NOTE = 'Ranking model unavailable; expanded search remains unresolved.'


@pytest.fixture
def curator():
    return Identity.objects.create_user(email='scoring-curator@example.test', is_staff=True)


@pytest.fixture
def client(curator):
    api = APIClient()
    api.force_authenticate(curator)
    return api


@pytest.fixture
def albumin_goal():
    return ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC', concept_name='Albumin goal')


@pytest.fixture
def albumin():
    return ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC', concept_name='Albumin')


def row(code, target=None, **overrides):
    """A proposed row; with *target*, one a forward Suggest run answered."""
    suggested = {} if target is None else {
        'target_concept': target, 'suggested_target_concept': target,
        'origin_system': 'suggest v0.4', 'suggestion_model_version': 'v0.4',
    }
    return SourceCodeConceptMapping.objects.create(**{
        'source_vocabulary_id': 'VendorLab', 'source_code': code, 'source_code_description': 'albumin',
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'status': 'proposed',
        'occurrence_count': 5, **suggested, **overrides,
    })


def detail(mapping):
    return f'/api/v1/code-mappings/{mapping.pk}/'


def candidate(concept):
    return {
        'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
        'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
        'concept_class_id': concept.concept_class_id, 'domain_id': concept.domain_id,
        'umls_score': 1.0, 'retrieval': 'umls',
    }


def forward_run(monkeypatch, concept, note, **kwargs):
    """One forward Suggest run whose ranker hands back *concept* with *note*."""
    chosen = candidate(concept)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([chosen, candidate(ConceptFactory())], 'C1'))
    monkeypatch.setattr(suggestions, 'rank_candidates_dispatch', lambda *a, **kw: (
        chosen, note, None if suggestions.is_fallback_note(note) else [
            {'concept_id': concept.pk, 'confidence': 0.9}], {},
    ))
    return suggestions.suggest_mappings('measurement', strategies=['umls'], min_occurrences=1, **kwargs)


def ranked(mapping=None, confidence=0.9, **overrides):
    """A reverse-preview candidate a ranker supported."""
    return {'mapping_id': mapping.pk if mapping else None, 'confidence': confidence,
            'verdict': 'supported', 'declined': False, **overrides}


def from_preview(client, concept, mapping, verdict='supported', **body):
    """Concept to Code's apply, naming a saved preview that lists *mapping* with *verdict*."""
    run = SuggestRun.objects.create(
        direction='reverse', state=SuggestRun.SUCCESS, source_vocabulary_id=mapping.source_vocabulary_id,
        activity=[{'concept': {'concept_id': concept.pk}, 'candidates': [{
            'mapping_id': mapping.pk, 'source_code': mapping.source_code,
            'source_vocabulary_id': mapping.source_vocabulary_id, 'verdict': verdict, 'declined': False,
            'source_code_description': mapping.source_code_description}]}])
    mapping.refresh_from_db()
    return client.post(f'/api/v1/concept-to-code/{concept.pk}/mappings/{mapping.pk}/', {
        'expected_updated_at': mapping.updated_at.isoformat(), 'run_id': str(run.pk), **body}, format='json')


def scores(client):
    overall = client.get(DASHBOARD).data['overall'] or {}
    return {key: overall.get(key, 0) for key in ('accepted', 'overridden', 'rejected')}


# --- fallbacks: marked where they are written ---------------------------------

def test_a_forward_fallback_is_marked_and_a_real_pick_takes_the_mark_off(monkeypatch, albumin_goal, albumin):
    queued = row('ALB')

    forward_run(monkeypatch, albumin_goal, FALLBACK_NOTE)

    queued.refresh_from_db()
    assert queued.target_concept_id == queued.suggested_target_concept_id == albumin_goal.pk
    assert queued.suggestion_model_version == suggestions.SUGGESTION_MODEL_VERSION
    assert queued.suggestion_fallback is True

    forward_run(monkeypatch, albumin, 'ranked', resuggest=True)

    queued.refresh_from_db()
    assert queued.target_concept_id == albumin.pk
    assert queued.suggestion_fallback is False


@pytest.mark.parametrize(('note', 'fallback'), [
    (FALLBACK_NOTE, True),
    ('Jev selected this concept with high confidence.', False),
    ('Unique exact standard concept/name-or-synonym match; no ranker call used.', False),
])
def test_a_group_suggestion_marks_every_member_it_writes_a_fallback_to(curator, albumin_goal, note, fallback):
    members = [row(code, locked_by=curator) for code in ('ALB-1', 'ALB-2')]
    run = SuggestRun.objects.create(source_vocabulary_id='VendorLab', total=2)

    _write_group_suggestion([m.pk for m in members], {
        'concept': {'concept_id': albumin_goal.pk}, 'strategy': 'lexical', 'note': note, 'confidence': None,
    }, run=run, actor=curator)

    for member in members:
        member.refresh_from_db()
        assert member.suggested_target_concept_id == albumin_goal.pk
        assert member.suggestion_fallback is fallback


# --- fallbacks: recorded, never scored ---------------------------------------

def test_a_reviewed_fallback_keeps_its_outcome_and_is_in_no_score(client, albumin_goal, albumin):
    """HealthTree's 1,531 "albumin" rows: one unanswered ranking, one correction."""
    fallbacks = [row(f'ALB-{n}', albumin_goal, suggestion_fallback=True, notes=FALLBACK_NOTE) for n in range(3)]
    unreviewed = row('ALB-LATER', albumin_goal, suggestion_fallback=True)
    real = row('TP', albumin)
    for mapping in fallbacks:
        assert client.patch(detail(mapping), {'destination_concept_id': albumin.pk, 'status': 'approved'},
                            format='json').status_code == 200
    assert client.patch(detail(real), {'status': 'approved'}, format='json').status_code == 200

    data = client.get(DASHBOARD).data

    for mapping in fallbacks:
        mapping.refresh_from_db()
        assert mapping.suggestion_outcome == 'overridden', 'what happened is still on the row'
        assert mapping.suggestion_fallback is True
    assert (data['overall']['accepted'], data['overall']['overridden'], data['overall']['suggestions']) == (1, 0, 1)
    assert (data['overall']['precision'], data['overall']['recall']) == (1.0, 1.0)
    assert [(m['model_version'], m['reviewed']) for m in data['models']] == [('v0.4', 1)]
    assert data['fallbacks'] == {'suggestions': 4, 'reviewed': 3}
    unreviewed.refresh_from_db()
    assert unreviewed.suggestion_outcome == ''

    legacy = client.get('/api/v1/code-mappings/accuracy/').data
    assert legacy['overall']['review_totals'] == {'approved': 1, 'rejected': 0, 'overridden': 0}
    assert legacy['fallbacks'] == {'suggestions': 4, 'reviewed': 3}


def test_a_dashboard_with_only_fallbacks_scores_nothing_and_says_how_many(client, albumin_goal):
    row('ALB', albumin_goal, suggestion_fallback=True)

    data = client.get(DASHBOARD).data

    assert data['models'] == [] and data['overall'] is None
    assert data['fallbacks'] == {'suggestions': 1, 'reviewed': 0}


def test_a_replaced_fallbacks_review_is_archived_as_one(monkeypatch, client, albumin_goal, albumin):
    reviewed = row('ALB', albumin_goal, suggestion_fallback=True, suggestion_outcome='rejected')

    forward_run(monkeypatch, albumin, 'ranked', resuggest=True)

    reviewed.refresh_from_db()
    assert (reviewed.suggestion_outcome, reviewed.suggestion_fallback) == ('', False)
    review = MappingSuggestionReview.objects.get(mapping=reviewed)
    assert (review.suggestion_outcome, review.suggestion_fallback) == ('rejected', True)
    assert scores(client) == {'accepted': 0, 'overridden': 0, 'rejected': 0}
    assert client.get(DASHBOARD).data['fallbacks'] == {'suggestions': 1, 'reviewed': 1}


def test_analytics_counts_the_decision_and_scores_only_the_models_answers(client, albumin_goal, albumin):
    common = {'status': 'approved', 'decision_surface': 'concept2code', 'decision_confidence': 0.95}
    row('ALB-1', albumin_goal, suggestion_fallback=True, suggestion_outcome='overridden', **common)
    row('ALB-2', albumin_goal, suggestion_fallback=True, suggestion_outcome='overridden', **common)
    row('TP', albumin, suggestion_outcome='accepted', **common)

    data = client.get(ANALYTICS).data

    section = next(s for s in data['sections'] if s['key'] == 'concept2code')
    for key in ('overall', '90-100'):
        line = next(r for r in section['rows'] if r['key'] == key)
        assert (line['total'], line['approved'], line['scored']) == (3, 3, 1)
        assert (line['precision'], line['recall']) == (1.0, 1.0)
    assert data['unscored_fallbacks'] == 2


def test_the_backfill_marks_stored_fallbacks_and_nothing_else(albumin_goal):
    marked = [
        row('FALLBACK', albumin_goal, notes=FALLBACK_NOTE),
        row('FALLBACK-REVIEWED', albumin_goal, notes=FALLBACK_NOTE, status='approved',
            suggestion_outcome='overridden'),
    ]
    left = [
        # Curator text that merely quotes the phrase is not a fallback's note.
        row('QUOTED', albumin_goal, notes='Checked: this is not a Best-match fallback.'),
        row('RANKED', albumin_goal, notes='Jev selected this concept with high confidence.'),
        # Nothing was chosen by that run, so the suggestion on the row is an earlier, real one.
        row('UNRESOLVED', albumin_goal, notes=UNRESOLVED_NOTE),
        # A note with no stored suggestion beside it describes no suggestion.
        row('NO-SUGGESTION', notes=FALLBACK_NOTE),
        row('BLANK', albumin_goal),
    ]
    before = {m.pk: m.updated_at for m in marked}

    migration = import_module('omop_core.migrations.0360_backfill_suggestion_fallback')
    migration.mark_fallbacks(apps, None)
    migration.mark_fallbacks(apps, None)   # a replay changes nothing

    for mapping in marked:
        mapping.refresh_from_db()
        assert mapping.suggestion_fallback is True, mapping.source_code
        assert mapping.updated_at == before[mapping.pk], 'not a change a reader of the list is sent'
    for mapping in left:
        mapping.refresh_from_db()
        assert mapping.suggestion_fallback is False, mapping.source_code
    migration.unmark_fallbacks(apps, None)


# --- a reverse ranker's pick is the suggestion that is scored ---------------------

def test_approving_what_reverse_suggest_proposed_is_an_acceptance(client, curator, albumin_goal, albumin):
    stale = row('ALB', albumin_goal, suggestion_confidence=0.6)

    result = reverse_suggest_jobs._repoint_proposed_row(ranked(stale), albumin, curator)

    stale.refresh_from_db()
    assert result['auto_proposed'] is True
    assert stale.target_concept_id == stale.suggested_target_concept_id == albumin.pk
    assert stale.suggestion_model_version == suggestions.SUGGESTION_MODEL_VERSION
    # No person chose it, so Suggest's Replace may still re-answer it.
    assert stale.origin_system == suggestions.SUGGESTION_PROVENANCE
    assert (stale.decision_surface, stale.proposed_by_id, stale.suggestion_outcome) == ('', None, '')

    assert client.patch(detail(stale), {'status': 'approved'}, format='json').status_code == 200

    stale.refresh_from_db()
    assert stale.suggestion_outcome == 'accepted'
    assert scores(client) == {'accepted': 1, 'overridden': 0, 'rejected': 0}


def test_approving_something_else_after_a_reverse_proposal_is_an_override(client, curator, albumin_goal, albumin):
    stale = row('ALB', albumin_goal, suggestion_confidence=0.6)
    reverse_suggest_jobs._repoint_proposed_row(ranked(stale), albumin, curator)
    third = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')

    assert client.patch(detail(stale), {'destination_concept_id': third.pk, 'status': 'approved'},
                        format='json').status_code == 200

    stale.refresh_from_db()
    assert stale.suggestion_outcome == 'overridden'
    assert stale.suggested_target_concept_id == albumin.pk, 'scored against what the ranker last proposed'


def test_a_reverse_pick_replaces_a_fallback_even_when_it_names_the_same_concept(curator, albumin_goal):
    fallback = row('ALB', albumin_goal, suggestion_fallback=True)
    other = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')
    fallback.target_concept = other          # a curator-less row parked elsewhere since
    fallback.save(update_fields=['target_concept'])

    reverse_suggest_jobs._repoint_proposed_row(ranked(fallback), albumin_goal, curator)

    fallback.refresh_from_db()
    assert fallback.suggested_target_concept_id == albumin_goal.pk
    assert fallback.suggestion_fallback is False


def test_a_new_row_a_ranker_chose_is_a_scored_suggestion(monkeypatch, client, curator, albumin):
    fresh = {'source_vocabulary_id': 'VendorLab', 'source_code': 'NEW', 'source_code_description': 'albumin'}
    monkeypatch.setattr('omop_core.mapping.reverse_catalog.refresh_catalog_candidate', lambda *a: fresh)

    result = reverse_suggest_jobs._propose_new_row(ranked(**fresh), albumin, curator)

    created = SourceCodeConceptMapping.objects.get(pk=result['mapping_id'])
    assert created.suggested_target_concept_id == albumin.pk
    assert created.suggestion_model_version == suggestions.SUGGESTION_MODEL_VERSION
    assert created.origin_system == suggestions.SUGGESTION_PROVENANCE
    assert (created.suggestion_fallback, created.suggestion_outcome) == (False, '')
    assert client.patch(detail(created), {'status': 'rejected'}, format='json').status_code == 200
    created.refresh_from_db()
    assert created.suggestion_outcome == 'rejected'
    assert scores(client) == {'accepted': 0, 'overridden': 0, 'rejected': 1}


def test_a_new_row_nobody_ranked_is_not_a_suggestion(monkeypatch, client, curator, albumin):
    fresh = {'source_vocabulary_id': 'VendorLab', 'source_code': 'NEW', 'source_code_description': 'albumin'}
    monkeypatch.setattr('omop_core.mapping.reverse_catalog.refresh_catalog_candidate', lambda *a: fresh)

    result = reverse_suggest_jobs._propose_new_row(ranked(verdict='review', **fresh), albumin, curator)

    created = SourceCodeConceptMapping.objects.get(pk=result['mapping_id'])
    assert created.suggested_target_concept_id is None
    assert (created.suggestion_model_version, created.origin_system) == ('', '')
    assert client.patch(detail(created), {'status': 'approved'}, format='json').status_code == 200
    created.refresh_from_db()
    assert created.suggestion_outcome == ''
    assert client.get(DASHBOARD).data['overall'] is None


@pytest.mark.parametrize(('status', 'outcome'), [('approved', 'accepted'), ('rejected', 'rejected')])
def test_deciding_a_ranker_chosen_candidate_in_one_step_is_scored_against_it(
        client, albumin_goal, albumin, status, outcome):
    """Concept to Code's Approve on a preview candidate moves the proposal and decides it at once."""
    stale = row('ALB', albumin_goal)

    response = from_preview(client, albumin, stale, status=status)

    assert response.status_code == 200, response.data
    stale.refresh_from_db()
    assert (stale.status, stale.target_concept_id) == (status, albumin.pk)
    assert stale.suggested_target_concept_id == albumin.pk
    assert stale.suggestion_outcome == outcome
    assert stale.decision_surface == 'concept2code'
    assert scores(client)[outcome] == 1 and sum(scores(client).values()) == 1


def test_a_candidate_the_curator_picked_unranked_still_overrides_the_stored_suggestion(
        client, albumin_goal, albumin):
    stale = row('ALB', albumin_goal)

    assert from_preview(client, albumin, stale, verdict='review', status='approved').status_code == 200

    stale.refresh_from_db()
    assert stale.suggested_target_concept_id == albumin_goal.pk
    assert stale.suggestion_outcome == 'overridden'


def test_proposing_a_ranker_chosen_candidate_keeps_the_curators_provenance(client, albumin_goal, albumin):
    """The curator clicked it, so Replace must not re-answer it; it is still what the ranker proposed."""
    stale = row('ALB', albumin_goal)

    assert from_preview(client, albumin, stale, status='proposed').status_code == 200

    stale.refresh_from_db()
    assert stale.origin_system == suggestions.CURATOR_PROVENANCE
    assert stale.suggested_target_concept_id == albumin.pk
    assert stale.pk not in {m.pk for m in suggestions.suggestable_mappings(
        'measurement', resuggest=True, min_occurrences=1)}
    assert client.patch(detail(stale), {'status': 'approved'}, format='json').status_code == 200
    stale.refresh_from_db()
    assert stale.suggestion_outcome == 'accepted'


def test_a_review_already_on_the_row_is_archived_not_rescored(client, curator, albumin_goal, albumin):
    """Approved against the forward suggestion, un-approved, then re-proposed by the reverse ranker."""
    reviewed = row('ALB', albumin_goal, suggestion_outcome='accepted', suggestion_confidence=0.6)

    reverse_suggest_jobs._repoint_proposed_row(ranked(reviewed), albumin, curator)

    reviewed.refresh_from_db()
    assert (reviewed.suggestion_outcome, reviewed.suggested_target_concept_id) == ('', albumin.pk)
    review = MappingSuggestionReview.objects.get(mapping=reviewed)
    assert (review.suggestion_outcome, review.suggested_target_concept_id) == ('accepted', albumin_goal.pk)

    third = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')
    assert client.patch(detail(reviewed), {'destination_concept_id': third.pk, 'status': 'approved'},
                        format='json').status_code == 200

    assert scores(client) == {'accepted': 1, 'overridden': 1, 'rejected': 0}


def test_a_reverse_pick_the_row_already_holds_changes_nothing(client, curator, albumin):
    """Approving in place a proposal the ranker made earlier is not a second proposal."""
    held = row('ALB', albumin, suggestion_outcome='rejected')

    assert from_preview(client, albumin, held, status='proposed').status_code == 200

    held.refresh_from_db()
    assert held.suggestion_outcome == 'rejected'
    assert not MappingSuggestionReview.objects.exists()
