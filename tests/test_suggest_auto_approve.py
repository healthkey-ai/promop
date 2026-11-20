"""Suggest approves a proposal a ranker is sure of, at the curator's threshold (#1532).

The threshold is a whole percent, 90 to 100 (or off), default 100, sticky per curator,
compared against the raw score. Only an account that may approve mappings gets it; an
analyst can run Suggest and never causes an approval; the inline dispatcher (no worker)
approves nothing.
"""
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from omop_core.mapping import suggestions
from omop_core.mapping.suggestions import suggestable_mappings
from omop_core.models import (
    ConceptMappingLock, ConditionOccurrence, GroupAccess, Organization, SourceCodeConceptMapping,
    SuggestRun,
)
from omop_core.services import suggest_auto_approve, suggest_jobs
from omop_core.services.suggest_jobs import FakeDispatcher, use_dispatcher
from patient_portal.api.views import code_mapping_list
from patient_portal.models import Identity
from tests.factories import (
    ConceptClassFactory, ConceptFactory, ConditionOccurrenceFactory, DomainFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

SUGGEST = '/api/v1/code-mappings/suggest/'
REFERENCE = '/api/v1/code-mappings/reference/'


class RunsNow(FakeDispatcher):
    """A dispatcher that is not the inline one (a worker exists) and runs the job at once."""

    def dispatch(self, run, params):
        super().dispatch(run, params)
        suggest_jobs.execute_run(str(run.id), params)


@pytest.fixture()
def pick():
    domain = DomainFactory(domain_id='Condition', domain_name='Condition')
    vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    klass = ConceptClassFactory(concept_class_id='Clinical Finding')
    return ConceptFactory(
        concept_id=9001, concept_name='Model pick', concept_code='9001', vocabulary=vocab,
        domain=domain, concept_class=klass, standard_concept='S',
    )


def queue_row(**kwargs):
    defaults = dict(
        source_vocabulary_id='ICD10', source_code='C90.0', domain_id='Condition',
        omop_table='condition', status='proposed', origin='import', occurrence_count=5,
    )
    defaults.update(kwargs)
    return SourceCodeConceptMapping.objects.create(**defaults)


def as_candidate(concept):
    return {
        'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
        'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
        'concept_class_id': concept.concept_class_id, 'domain_id': concept.domain_id,
        'umls_score': 1.0, 'retrieval': 'umls',
    }


def rank_as(monkeypatch, concept, confidence):
    """The ranker answers *concept* with *confidence* (None: it declines)."""
    candidate = as_candidate(concept)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([candidate], 'C1'))
    if confidence is None:
        monkeypatch.setattr(suggestions, 'rank_candidates_dispatch',
                            lambda *a, **kw: (None, 'no match', [], {}))
    else:
        monkeypatch.setattr(suggestions, 'rank_candidates_dispatch', lambda *a, **kw: (
            candidate, 'ranked', [{'concept_id': concept.concept_id, 'confidence': confidence}], {}))


def run(user, threshold, **kwargs):
    return suggestions.suggest_mappings(
        'condition', strategies=['umls'], min_occurrences=1,
        auto_approve_threshold=threshold, approve_as=user, **kwargs)


@pytest.fixture()
def staff():
    return Identity.objects.create_user(email='staff@example.test', is_staff=True)


def user_with_role(email, role):
    user = Identity.objects.create_user(email=email, is_staff=False)
    org, _ = Organization.objects.get_or_create(slug='aa-org', defaults={'name': 'AA Org'})
    GroupAccess.objects.create(identity=user, org=org, role=role)
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def params_for(**overrides):
    return {
        'tables': ['condition'], 'min_occurrences': 1, 'limit': 5, 'dry_run': False,
        'source_vocabulary_id': 'ICD10', 'strategies': ['umls'], 'lexical_limit': 25,
        'resuggest': False, 'ranking_model': 'anthropic', **overrides,
    }


def execute(user, **overrides):
    """Run a queued Suggest run as *user* and return its row."""
    run_row = SuggestRun.objects.create(source_vocabulary_id='ICD10', created_by=user)
    with use_dispatcher(FakeDispatcher()):
        suggest_jobs.execute_run(str(run_row.pk), params_for(**overrides))
    run_row.refresh_from_db()
    return run_row


# --- the threshold and the proposal ---------------------------------------------

@pytest.mark.parametrize('confidence, threshold, approved', [
    (0.99, 100, False),     # the default approves only a score of exactly 1.0
    (1.0, 100, True),
    (0.995, 100, False),    # not rounded: 99.5% is not 100%
    (0.996, 100, False),
    (0.90, 90, True),
    (0.89, 90, False),
    (0.895, 90, False),     # 89.5% does not round up into 90
    (0.95, 95, True),       # read as the number it was written as, not its binary value
    (0.9499999, 95, False),
    (0.944, 95, False),
    (0.949, 95, False),
])
def test_a_proposal_is_approved_at_or_above_the_threshold(monkeypatch, pick, staff, confidence, threshold, approved):
    row = queue_row()
    rank_as(monkeypatch, pick, confidence)

    [entry] = run(staff, threshold)

    row.refresh_from_db()
    assert row.target_concept_id == pick.pk, 'written as a proposal either way'
    assert row.status == ('approved' if approved else 'proposed')
    assert entry.get('auto_approved', False) is approved


@pytest.mark.parametrize('score', [float('nan'), float('inf'), float('-inf'), 'x', True, 1.5, -0.1, None])
def test_a_score_that_is_not_a_probability_never_qualifies_and_never_raises(pick, score):
    mapping = SimpleNamespace(suggestion_fallback=False, suggestion_confidence=score)
    assert suggest_auto_approve.qualifies(mapping, 90, concept=pick) is False
    assert suggest_auto_approve.percent_text(score) == '?'


def test_an_auto_approval_is_the_curators_approval_with_the_machine_recorded(monkeypatch, pick, staff):
    row = queue_row()
    rank_as(monkeypatch, pick, 1.0)

    run(staff, 100)

    row.refresh_from_db()
    assert row.status == 'approved'
    assert row.reviewer_id == staff.pk, 'the person whose run it is signed it off'
    assert row.reviewed_at is not None
    assert row.updated_by_id == staff.pk
    assert row.origin_system == 'suggest v0.4 auto-approved 100%'
    assert row.suggested_target_concept_id == pick.pk
    assert row.suggestion_confidence == 1.0
    # A machine decided it: no curator decision is recorded, and the suggestion
    # has no curator disposition to score.
    assert (row.decision_surface, row.decision_confidence) == ('', None)
    assert row.suggestion_outcome == ''
    assert row.proposed_by_id is None
    assert 'Auto-approved by Suggest: 100% confidence, threshold 100%.' in row.notes
    # An approval at the destination it names re-points what the code left at concept 0.
    assert row.deferred_repoint_concept_ids == [0]


def test_the_approval_moves_stored_rows_and_clears_provisional_confidence(
    monkeypatch, pick, staff, django_capture_on_commit_callbacks,
):
    person = PersonFactory()
    unresolved = ConditionOccurrenceFactory(
        person=person, condition_concept_id=0, condition_source_value='C90.0')
    provisional = ConditionOccurrenceFactory(
        person=PersonFactory(), condition_concept=pick, condition_source_value='C90.0', confidence=95)
    queue_row()
    rank_as(monkeypatch, pick, 1.0)

    with django_capture_on_commit_callbacks(execute=True):
        run(staff, 100)

    unresolved = ConditionOccurrence.objects.get(pk=unresolved.pk)
    provisional = ConditionOccurrence.objects.get(pk=provisional.pk)
    assert unresolved.condition_concept_id == pick.pk
    assert provisional.confidence is None, 'approval makes the provisional row real'


def test_a_declined_code_is_never_approved(monkeypatch, pick, staff):
    row = queue_row()
    rank_as(monkeypatch, pick, None)

    [entry] = run(staff, 90)

    row.refresh_from_db()
    assert row.status == 'proposed' and row.target_concept_id is None
    assert 'auto_approved' not in entry


def test_a_proposal_nothing_ranked_is_never_approved(monkeypatch, pick, staff):
    """A UMLS shortcut or a retrieval fallback carries no score a ranker stood behind."""
    row = queue_row()
    candidate = as_candidate(pick)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([candidate], 'C1'))
    monkeypatch.setattr(suggestions, 'rank_candidates_dispatch', lambda *a, **kw: (candidate, 'ranked', [], {}))

    run(staff, 90)

    row.refresh_from_db()
    assert row.target_concept_id == pick.pk and row.suggestion_confidence is None
    assert row.status == 'proposed'
    row.suggestion_confidence, row.suggestion_fallback = 1.0, True
    assert suggest_auto_approve.qualifies(row, 90, concept=pick) is False


def test_no_threshold_or_no_approver_approves_nothing(monkeypatch, pick, staff):
    queue_row()
    queue_row(source_code='C90.1')
    rank_as(monkeypatch, pick, 1.0)

    run(staff, None)
    suggestions.suggest_mappings('condition', strategies=['umls'], min_occurrences=1,
                                 auto_approve_threshold=90, approve_as=None, resuggest=True)

    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()


def test_a_dry_run_approves_nothing(monkeypatch, pick, staff):
    queue_row()
    rank_as(monkeypatch, pick, 1.0)

    run(staff, 90, dry_run=True)

    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()


def test_a_refused_approval_leaves_the_proposal(monkeypatch, pick, staff):
    """Another curator holds the destination concept (#2390): review only."""
    other = Identity.objects.create_user(email='other@example.test', is_staff=True)
    ConceptMappingLock.objects.create(concept=pick, locked_by=other)
    row = queue_row()
    rank_as(monkeypatch, pick, 1.0)

    [entry] = run(staff, 90)

    row.refresh_from_db()
    assert (row.status, row.target_concept_id) == ('proposed', pick.pk)
    assert 'auto_approved' not in entry
    assert row.origin_system == 'suggest v0.4'


def test_a_curator_cannot_auto_approve_what_they_proposed(monkeypatch, pick, staff, settings):
    """The two-person rule (#2726): the proposer's own run cannot sign off their proposal."""
    settings.MAPPING_SEPARATE_APPROVER = True   # off by default under DEBUG, on in production
    row = queue_row(
        target_concept=pick, suggested_target_concept=pick, proposed_by=staff,
        origin_system='suggest v0.3', suggestion_model_version='v0.3')
    rank_as(monkeypatch, pick, 1.0)

    [entry] = run(staff, 90, resuggest=True)

    row.refresh_from_db()
    assert (row.status, row.proposed_by_id) == ('proposed', staff.pk)
    assert 'auto_approved' not in entry and 'auto_approve_error' not in entry


def test_replace_re_answers_a_proposal_and_never_an_auto_approved_row(monkeypatch, pick, staff):
    replaceable = queue_row(
        source_code='C90.1', target_concept=pick, suggested_target_concept=pick,
        origin_system='suggest v0.3', suggestion_model_version='v0.3')
    rank_as(monkeypatch, pick, 0.95)

    [entry] = run(staff, 95, resuggest=True)

    replaceable.refresh_from_db()
    assert entry['auto_approved'] is True and replaceable.status == 'approved'
    assert replaceable.pk not in {m.pk for m in suggestable_mappings('condition', resuggest=True, min_occurrences=1)}


def test_an_unexpected_failure_leaves_that_row_proposed_and_the_run_goes_on(monkeypatch, pick, staff):
    first = queue_row(occurrence_count=9)
    second = queue_row(source_code='C90.1')
    rank_as(monkeypatch, pick, 1.0)
    from patient_portal.api import views
    real = views._upsert_source_code_mapping

    def flaky(concept, data, user, mapping=None, **kwargs):
        if mapping is not None and mapping.pk == first.pk:
            mapping.status = 'approved'          # a half-applied approval, rolled back with the block
            mapping.save(update_fields=['status'])
            raise RuntimeError('boom with a source value in it')
        return real(concept, data, user, mapping=mapping, **kwargs)

    monkeypatch.setattr(views, '_upsert_source_code_mapping', flaky)
    outcomes = []

    results = run(staff, 100, on_auto_approve=outcomes.append)

    first.refresh_from_db(), second.refresh_from_db()
    assert (first.status, second.status) == ('proposed', 'approved')
    assert outcomes == ['error', 'approved']
    assert [r.get('auto_approve_error', False) for r in results] == [True, False]
    assert len(results) == 2


def test_eligibility_is_checked_again_for_every_row(monkeypatch, pick, staff):
    """An account that loses the right partway through a run approves nothing more."""
    first = queue_row(occurrence_count=9)
    second = queue_row(source_code='C90.1')
    rank_as(monkeypatch, pick, 1.0)
    outcomes = []

    def demote(outcome):
        outcomes.append(outcome)
        Identity.objects.filter(pk=staff.pk).update(is_staff=False)

    run(staff, 100, on_auto_approve=demote)

    first.refresh_from_db(), second.refresh_from_db()
    assert (first.status, second.status) == ('approved', 'proposed')
    assert outcomes == ['approved']


# --- the threshold is validated, role-gated, sticky and can be turned off ---------

@pytest.mark.parametrize('value', [89, 101, 0, -5, 95.5, 'abc', True, False, [90], '89', 'of', 'off!'])
def test_a_threshold_outside_90_to_100_or_off_is_a_400(staff, value):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(staff).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': value}, format='json')
    assert response.status_code == 400, response.data
    assert 'auto_approve_threshold' in response.data
    assert dispatcher.calls == []
    staff.refresh_from_db()
    assert staff.suggest_auto_approve_threshold is None and staff.suggest_auto_approve_off is False, \
        'a refused request stores nothing'


@pytest.mark.parametrize('value, stored', [(90, 90), (100, 100), ('95', 95), (97.0, 97)])
def test_a_valid_threshold_reaches_the_run_and_is_remembered(staff, value, stored):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(staff).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': value}, format='json')
    assert response.status_code == 202, response.data
    [(run_id, params)] = dispatcher.calls
    assert params['auto_approve_threshold'] == stored
    selection = SuggestRun.objects.get(pk=run_id).selection
    assert (selection['auto_approve_state'], selection['auto_approve_threshold']) == ('on', stored)
    assert response.data['auto_approve'] == {'state': 'on', 'threshold': stored, 'reason': ''}
    staff.refresh_from_db()
    assert staff.suggest_auto_approve_threshold == stored and staff.suggest_auto_approve_off is False


def test_the_threshold_is_sticky_between_runs(staff):
    client = client_for(staff)
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 92}, format='json')
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10'}, format='json')
    assert [params['auto_approve_threshold'] for _id, params in dispatcher.calls] == [92, 92]
    with use_dispatcher(FakeDispatcher()):      # a deployment with a worker queue
        reference = client.get(REFERENCE).data['suggest_auto_approve']
    assert reference == {'allowed': True, 'available': True, 'unavailable_reason': '', 'threshold': 92,
                         'off': False, 'min': 90, 'max': 100}
    # It is this curator's, not the deployment's.
    other = Identity.objects.create_user(email='other@example.test', is_staff=True)
    assert client_for(other).get(REFERENCE).data['suggest_auto_approve']['threshold'] == 100


def test_an_approver_who_never_chose_gets_the_default(staff):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client_for(staff).post(SUGGEST, {'source_vocabulary_id': 'ICD10'}, format='json')
    assert dispatcher.calls[0][1]['auto_approve_threshold'] == 100
    assert dispatcher.calls[0][1]['auto_approve_state'] == 'on'


@pytest.mark.parametrize('spelling', ['off', 'OFF', ' Off '])
def test_off_is_a_choice_that_sticks_and_keeps_the_number(staff, spelling):
    client = client_for(staff)
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 93}, format='json')
        off = client.post(SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': spelling}, format='json')
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10'}, format='json')
    assert off.status_code == 202, off.data
    states = [(p['auto_approve_state'], p['auto_approve_threshold']) for _id, p in dispatcher.calls]
    assert states == [('on', 93), ('off', None), ('off', None)]
    reference = client.get(REFERENCE).data['suggest_auto_approve']
    assert (reference['off'], reference['threshold']) == (True, 93), 'the number returns when it is turned on'
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 91}, format='json')
    assert dispatcher.calls[0][1]['auto_approve_state'] == 'on'
    staff.refresh_from_db()
    assert (staff.suggest_auto_approve_off, staff.suggest_auto_approve_threshold) == (False, 91)


def test_a_run_with_auto_approval_off_approves_nothing(monkeypatch, pick, staff):
    row = queue_row()
    rank_as(monkeypatch, pick, 1.0)
    with use_dispatcher(RunsNow()):
        started = client_for(staff).post(SUGGEST, {
            'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 'off', 'strategies': ['umls'],
        }, format='json')
    row.refresh_from_db()
    assert row.status == 'proposed' and row.target_concept_id == pick.pk
    run_row = SuggestRun.objects.get(pk=started.data['run_id'])
    assert (run_row.selection['auto_approve_state'], run_row.selection['auto_approved']) == ('off', 0)


def test_a_dry_run_carries_no_threshold(staff):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(staff).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 93, 'dry_run': True},
            format='json')
    assert dispatcher.calls[0][1]['auto_approve_threshold'] is None
    assert response.data['auto_approve'] == {'state': 'disabled', 'threshold': None, 'reason': 'dry_run'}


def test_the_inline_dispatcher_approves_nothing_and_says_why(monkeypatch, pick, staff):
    """No worker: an approval's re-point would run inside the request."""
    row = queue_row()
    rank_as(monkeypatch, pick, 1.0)
    client = client_for(staff)
    with use_dispatcher(suggest_jobs.InlineDispatcher()):
        started = client.post(SUGGEST, {
            'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 90, 'strategies': ['umls'],
        }, format='json')
        reference = client.get(REFERENCE).data['suggest_auto_approve']
    assert started.status_code == 202, started.data
    assert started.data['auto_approve'] == {'state': 'disabled', 'threshold': None, 'reason': 'no_worker'}
    assert started.data['auto_approved'] == 0
    row.refresh_from_db()
    assert row.status == 'proposed' and row.target_concept_id == pick.pk
    assert (reference['allowed'], reference['available']) == (True, False)
    assert 'worker' in reference['unavailable_reason']
    staff.refresh_from_db()
    assert staff.suggest_auto_approve_threshold == 90, 'their choice is still remembered'


def test_an_org_admin_may_auto_approve():
    admin = user_with_role('admin@example.test', 'org_admin')
    assert suggest_auto_approve.eligible(admin) is True
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(admin).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 94}, format='json')
    assert response.status_code == 202, response.data
    assert dispatcher.calls[0][1]['auto_approve_threshold'] == 94


def test_the_database_refuses_a_threshold_outside_90_to_100(staff):
    from django.db import IntegrityError, transaction
    for value in (89, 101, 0):
        with pytest.raises(IntegrityError), transaction.atomic():
            Identity.objects.filter(pk=staff.pk).update(suggest_auto_approve_threshold=value)


def test_an_analyst_runs_suggest_but_the_setting_is_ignored():
    analyst = user_with_role('analyst@example.test', 'analyst')
    assert suggest_auto_approve.eligible(analyst) is False
    client = client_for(analyst)
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client.post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 90}, format='json')
        refused = client.post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 50}, format='json')
        off = client.post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 'off'}, format='json')
    assert response.status_code == 202, response.data
    assert dispatcher.calls[0][1]['auto_approve_threshold'] is None
    assert response.data['auto_approve']['reason'] == 'not_approver'
    assert refused.status_code == 400, 'out of range is wrong on any account'
    assert off.status_code == 202
    analyst.refresh_from_db()
    assert (analyst.suggest_auto_approve_threshold, analyst.suggest_auto_approve_off) == (None, False)
    reference = client.get(REFERENCE).data['suggest_auto_approve']
    assert reference['allowed'] is False


@pytest.mark.parametrize('role', ['doctor', 'patient'])
def test_a_doctor_or_patient_cannot_start_suggest_at_all(role):
    """Unchanged: Suggest is a mapping write, and only the analyst's, an admin's or staff's."""
    user = user_with_role(f'{role}@example.test', role)
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(user).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 95}, format='json')
    assert response.status_code == 403
    assert dispatcher.calls == []
    assert suggest_auto_approve.eligible(user) is False


def test_a_service_credential_is_a_machine_and_never_auto_approves():
    service = Identity.objects.create_user(email='svc@example.test', is_staff=True)
    service.issuer = 'urn:service'
    assert suggest_auto_approve.eligible(service) is False
    assert suggest_auto_approve.plan(service, 90)['state'] == 'disabled'


# --- the run records what it did ------------------------------------------------

def test_a_run_records_its_threshold_and_what_it_approved(monkeypatch, pick, staff):
    queue_row()
    queue_row(source_code='C90.1')
    rank_as(monkeypatch, pick, 0.92)
    client = client_for(staff)
    with use_dispatcher(RunsNow()):
        started = client.post(SUGGEST, {
            'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 90, 'strategies': ['umls'],
        }, format='json')
    assert started.status_code == 202, started.data

    run_row = SuggestRun.objects.get(pk=started.data['run_id'])
    assert run_row.state == SuggestRun.SUCCESS, run_row.error
    assert run_row.selection['auto_approve_threshold'] == 90
    assert run_row.selection['auto_approved'] == 2
    polled = client.get(f'/api/v1/code-mappings/suggest-runs/{run_row.pk}/').data
    assert (polled['auto_approve_threshold'], polled['auto_approved']) == (90, 2)
    assert polled['auto_approve'] == {'state': 'on', 'threshold': 90, 'reason': ''}
    assert SourceCodeConceptMapping.objects.filter(status='approved').count() == 2


def test_a_failed_run_still_says_what_it_approved(monkeypatch, pick, staff):
    """Counts are written as each row is approved, not at the end."""
    queue_row()
    rank_as(monkeypatch, pick, 1.0)
    real = suggestions.suggest_mappings

    def killed_after_the_work(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('worker lost')

    monkeypatch.setattr(suggestions, 'suggest_mappings', killed_after_the_work)

    run_row = execute(staff, auto_approve_threshold=100, auto_approve_state='on')

    assert run_row.state == SuggestRun.FAILURE
    assert run_row.selection['auto_approved'] == 1
    assert SourceCodeConceptMapping.objects.filter(status='approved').count() == 1


def test_a_queued_run_rechecks_who_started_it(monkeypatch, pick):
    """The params outlive the request; an account that cannot approve gets nothing approved."""
    analyst = user_with_role('analyst2@example.test', 'analyst')
    queue_row()
    rank_as(monkeypatch, pick, 1.0)

    run_row = execute(analyst, auto_approve_threshold=90, auto_approve_state='on')

    assert run_row.state == SuggestRun.SUCCESS, run_row.error
    assert run_row.selection['auto_approved'] == 0
    assert (run_row.selection['auto_approve_state'], run_row.selection['auto_approve_reason']) == (
        'disabled', 'not_approver')
    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()


@pytest.mark.parametrize('tampered', [50, 101, 'abc', 95.5, True, [90], float('nan')])
def test_a_tampered_threshold_in_the_params_approves_nothing(monkeypatch, pick, staff, tampered):
    queue_row()
    rank_as(monkeypatch, pick, 1.0)

    run_row = execute(staff, auto_approve_threshold=tampered, auto_approve_state='on')

    assert run_row.state == SuggestRun.SUCCESS, run_row.error
    assert run_row.selection['auto_approve_state'] == 'disabled'
    assert run_row.selection['auto_approve_reason'] == 'invalid'
    assert run_row.selection['auto_approved'] == 0
    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()


def test_a_queued_run_on_a_deployment_that_lost_its_worker_approves_nothing(monkeypatch, pick, staff):
    queue_row()
    rank_as(monkeypatch, pick, 1.0)
    run_row = SuggestRun.objects.create(source_vocabulary_id='ICD10', created_by=staff)

    with use_dispatcher(suggest_jobs.InlineDispatcher()):
        suggest_jobs.execute_run(str(run_row.pk), params_for(auto_approve_threshold=90, auto_approve_state='on'))

    run_row.refresh_from_db()
    assert run_row.selection['auto_approve_reason'] == 'no_worker'
    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()


# --- finding what Suggest approved, and undoing it ---------------------------------

@pytest.fixture()
def browse(staff):
    def get(**params):
        request = APIRequestFactory().get('/api/v1/code-mappings/', {'browse': '1', **params})
        force_authenticate(request, user=staff)
        return code_mapping_list(request)
    return get


def test_the_list_can_be_narrowed_to_what_suggest_approved_by_itself(monkeypatch, pick, staff, browse):
    ours = queue_row(source_code='AUTO-1')
    queue_row(source_code='AUTO-2')
    queue_row(source_code='HAND', source_vocabulary_id='ICD10')
    rank_as(monkeypatch, pick, 1.0)
    run(staff, 100, mapping_ids=[ours.pk, SourceCodeConceptMapping.objects.get(source_code='AUTO-2').pk])
    # A curator's own approval, and a plain suggestion, are not what the filter is for.
    hand = SourceCodeConceptMapping.objects.get(source_code='HAND')
    hand.status, hand.target_concept, hand.origin_system = 'approved', pick, 'staff@example.test'
    hand.save()

    everything = browse(source='ICD10', order_0='source_code').data
    assert sorted(r['source_code'] for r in everything['results']) == ['AUTO-1', 'AUTO-2', 'HAND']
    filtered = browse(source='ICD10', auto_approved='1', order_0='source_code').data
    assert [r['source_code'] for r in filtered['results']] == ['AUTO-1', 'AUTO-2']
    assert len(browse(source='ICD10', auto_approved='0').data['results']) == 3
    assert browse(source='ICD10', auto_approved='yes').status_code == 400

    # The group members endpoint narrows by the same rule (it reads visible_rows).
    from omop_core.services.mapping_browse import visible_rows
    rows = visible_rows(SourceCodeConceptMapping.objects.all(), {'source': 'ICD10', 'auto_approved': '1'})
    assert sorted(rows.values_list('source_code', flat=True)) == ['AUTO-1', 'AUTO-2']


def test_an_auto_approval_can_be_undone_by_an_approver_and_not_by_an_analyst(monkeypatch, pick, staff):
    row = queue_row()
    rank_as(monkeypatch, pick, 1.0)
    run(staff, 100)
    row.refresh_from_db()
    assert row.status == 'approved'
    analyst = user_with_role('analyst3@example.test', 'analyst')

    refused = client_for(analyst).patch(
        f'/api/v1/code-mappings/{row.pk}/', {'status': 'proposed'}, format='json')
    assert refused.status_code == 400

    undone = client_for(staff).patch(
        f'/api/v1/code-mappings/{row.pk}/', {'status': 'proposed'}, format='json')
    assert undone.status_code == 200, undone.data
    row.refresh_from_db()
    assert row.status == 'proposed' and row.reviewer_id is None
    assert row.origin_system == 'suggest v0.4 auto-approved 100%', 'still findable for review'


def test_a_wrong_auto_approval_is_corrected_by_a_second_approver_moving_the_destination(
    monkeypatch, pick, staff, settings, django_capture_on_commit_callbacks,
):
    """The documented undo: propose another destination, a second approver approves it."""
    settings.MAPPING_SEPARATE_APPROVER = True
    better = ConceptFactory(
        concept_id=9002, concept_name='Better pick', concept_code='9002', vocabulary=pick.vocabulary,
        domain=pick.domain, concept_class=pick.concept_class, standard_concept='S')
    stored = ConditionOccurrenceFactory(
        person=PersonFactory(), condition_concept_id=0, condition_source_value='C90.0')
    row = queue_row()
    rank_as(monkeypatch, pick, 1.0)
    with django_capture_on_commit_callbacks(execute=True):
        run(staff, 100)
    assert ConditionOccurrence.objects.get(pk=stored.pk).condition_concept_id == pick.pk
    second = Identity.objects.create_user(email='second@example.test', is_staff=True)

    proposed = client_for(staff).patch(
        f'/api/v1/code-mappings/{row.pk}/',
        {'destination_concept_id': better.pk, 'status': 'proposed'}, format='json')
    assert proposed.status_code == 200, proposed.data
    with django_capture_on_commit_callbacks(execute=True):
        approved = client_for(second).patch(
            f'/api/v1/code-mappings/{row.pk}/', {'status': 'approved'}, format='json')
    assert approved.status_code == 200, approved.data

    assert ConditionOccurrence.objects.get(pk=stored.pk).condition_concept_id == better.pk
