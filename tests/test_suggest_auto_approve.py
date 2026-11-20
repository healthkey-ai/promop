"""Suggest approves a proposal a ranker is sure of, at the curator's threshold (#1532).

The threshold is a whole percent, 90 to 100, default 100, and sticky per curator.
Only an account that may approve mappings gets it; an analyst can run Suggest and
never causes an approval.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.mapping import suggestions
from omop_core.mapping.suggestions import suggestable_mappings
from omop_core.models import (
    ConceptMappingLock, ConditionOccurrence, GroupAccess, Organization, SourceCodeConceptMapping,
    SuggestRun,
)
from omop_core.services import suggest_auto_approve, suggest_jobs
from omop_core.services.suggest_jobs import FakeDispatcher, use_dispatcher
from patient_portal.models import Identity
from tests.factories import (
    ConceptClassFactory, ConceptFactory, ConditionOccurrenceFactory, DomainFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

SUGGEST = '/api/v1/code-mappings/suggest/'
REFERENCE = '/api/v1/code-mappings/reference/'


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


# --- the threshold and the proposal ---------------------------------------------

@pytest.mark.parametrize('confidence, threshold, approved', [
    (0.99, 100, False),     # the default approves only a score that rounds to 100
    (1.0, 100, True),
    (0.996, 100, True),     # 99.6% rounds to 100, as a provisional row's percent does
    (0.90, 90, True),
    (0.89, 90, False),
    (0.95, 95, True),
    (0.944, 95, False),     # 94.4% rounds to 94
    (0.949, 95, True),      # 94.9% rounds to 95, the percent a provisional row would carry
])
def test_a_proposal_is_approved_at_or_above_the_threshold(monkeypatch, pick, staff, confidence, threshold, approved):
    row = queue_row()
    rank_as(monkeypatch, pick, confidence)

    [entry] = run(staff, threshold)

    row.refresh_from_db()
    assert row.target_concept_id == pick.pk, 'written as a proposal either way'
    assert row.status == ('approved' if approved else 'proposed')
    assert entry.get('auto_approved', False) is approved


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


def test_replace_re_answers_a_proposal_and_never_an_auto_approved_row(monkeypatch, pick, staff):
    replaceable = queue_row(
        source_code='C90.1', target_concept=pick, suggested_target_concept=pick,
        origin_system='suggest v0.3', suggestion_model_version='v0.3')
    rank_as(monkeypatch, pick, 0.95)

    [entry] = run(staff, 95, resuggest=True)

    replaceable.refresh_from_db()
    assert entry['auto_approved'] is True and replaceable.status == 'approved'
    assert replaceable.pk not in {m.pk for m in suggestable_mappings('condition', resuggest=True, min_occurrences=1)}


# --- the threshold is validated, role-gated and sticky ----------------------------

@pytest.mark.parametrize('value', [89, 101, 0, -5, 95.5, 'abc', True, [90], '89'])
def test_a_threshold_outside_90_to_100_is_a_400(staff, value):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(staff).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': value}, format='json')
    assert response.status_code == 400, response.data
    assert 'auto_approve_threshold' in response.data
    assert dispatcher.calls == []
    staff.refresh_from_db()
    assert staff.suggest_auto_approve_threshold is None, 'a refused request stores nothing'


@pytest.mark.parametrize('value, stored', [(90, 90), (100, 100), ('95', 95), (97.0, 97)])
def test_a_valid_threshold_reaches_the_run_and_is_remembered(staff, value, stored):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(staff).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': value}, format='json')
    assert response.status_code == 202, response.data
    [(run_id, params)] = dispatcher.calls
    assert params['auto_approve_threshold'] == stored
    assert SuggestRun.objects.get(pk=run_id).selection['auto_approve_threshold'] == stored
    staff.refresh_from_db()
    assert staff.suggest_auto_approve_threshold == stored


def test_the_threshold_is_sticky_between_runs(staff):
    client = client_for(staff)
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 92}, format='json')
        client.post(SUGGEST, {'source_vocabulary_id': 'ICD10'}, format='json')
    assert [params['auto_approve_threshold'] for _id, params in dispatcher.calls] == [92, 92]
    reference = client.get(REFERENCE).data['suggest_auto_approve']
    assert reference == {'allowed': True, 'threshold': 92, 'min': 90, 'max': 100}
    # It is this curator's, not the deployment's.
    other = Identity.objects.create_user(email='other@example.test', is_staff=True)
    assert client_for(other).get(REFERENCE).data['suggest_auto_approve']['threshold'] == 100


def test_an_approver_who_never_set_one_gets_the_default(staff):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client_for(staff).post(SUGGEST, {'source_vocabulary_id': 'ICD10'}, format='json')
    assert dispatcher.calls[0][1]['auto_approve_threshold'] == 100


def test_a_dry_run_carries_no_threshold_and_stores_the_choice_only_when_a_run_starts(staff):
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        client_for(staff).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 93, 'dry_run': True},
            format='json')
    assert dispatcher.calls[0][1]['auto_approve_threshold'] is None


@pytest.mark.parametrize('value', [89, 101, 0])
def test_the_database_refuses_a_threshold_outside_90_to_100(staff, value):
    from django.db import IntegrityError, transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        Identity.objects.filter(pk=staff.pk).update(suggest_auto_approve_threshold=value)


@pytest.mark.parametrize('role', ['org_admin'])
def test_an_org_admin_may_auto_approve(role):
    admin = user_with_role('admin@example.test', role)
    assert suggest_auto_approve.eligible(admin) is True
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client_for(admin).post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 94}, format='json')
    assert response.status_code == 202, response.data
    assert dispatcher.calls[0][1]['auto_approve_threshold'] == 94


def test_an_analyst_runs_suggest_but_the_setting_is_ignored():
    analyst = user_with_role('analyst@example.test', 'analyst')
    assert suggest_auto_approve.eligible(analyst) is False
    client = client_for(analyst)
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        response = client.post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 90}, format='json')
        refused = client.post(
            SUGGEST, {'source_vocabulary_id': 'ICD10', 'auto_approve_threshold': 50}, format='json')
    assert response.status_code == 202, response.data
    assert dispatcher.calls[0][1]['auto_approve_threshold'] is None
    assert refused.status_code == 400, 'out of range is wrong on any account'
    analyst.refresh_from_db()
    assert analyst.suggest_auto_approve_threshold is None
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
    assert suggest_auto_approve.run_threshold(service, 90) is None


# --- the run records what it did ------------------------------------------------

def test_a_run_records_its_threshold_and_what_it_approved(monkeypatch, pick, staff):
    queue_row()
    queue_row(source_code='C90.1')
    rank_as(monkeypatch, pick, 0.92)
    client = client_for(staff)
    with use_dispatcher(suggest_jobs.InlineDispatcher()):
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
    assert SourceCodeConceptMapping.objects.filter(status='approved').count() == 2


def test_a_queued_run_rechecks_who_started_it(monkeypatch, pick):
    """The params outlive the request; an account that cannot approve gets nothing approved."""
    analyst = user_with_role('analyst2@example.test', 'analyst')
    queue_row()
    rank_as(monkeypatch, pick, 1.0)
    run_row = SuggestRun.objects.create(source_vocabulary_id='ICD10', created_by=analyst)
    params = {
        'tables': ['condition'], 'min_occurrences': 1, 'limit': 5, 'dry_run': False,
        'source_vocabulary_id': 'ICD10', 'strategies': ['umls'], 'lexical_limit': 25,
        'resuggest': False, 'ranking_model': 'anthropic', 'auto_approve_threshold': 90,
    }

    suggest_jobs.execute_run(str(run_row.pk), params)

    run_row.refresh_from_db()
    assert run_row.state == SuggestRun.SUCCESS, run_row.error
    assert run_row.selection['auto_approved'] == 0
    assert run_row.selection['auto_approve_threshold'] is None
    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()
