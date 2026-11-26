"""Reviewed answer-to-concept mappings for field choices (#1224)."""
import importlib

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from omop_core.models import (
    FieldChoice, FieldChoiceCode, FieldConceptMapping, FieldValueConceptMapping as Mapping,
    FieldValueConceptMappingRevision as Revision, Vocabulary,
)
from omop_core.services import field_value_mappings as fvm
from omop_core.services.field_curation_transfer import apply_payload, read_payload
from patient_portal.models import Identity
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def concept(code, vocab='SNOMED', domain='Observation', **kwargs):
    VocabularyFactory(vocabulary_id=vocab, vocabulary_name=vocab, vocabulary_version='2026-09')
    DomainFactory(domain_id=domain, domain_name=domain)
    return ConceptFactory(vocabulary_id=vocab, concept_code=code, domain_id=domain,
                          concept_name=f'{vocab} {code}', **kwargs)


@pytest.fixture
def choice():
    return FieldChoice.objects.create(field_name='disease_activity', display='Active')


@pytest.fixture
def users():
    return (Identity.objects.create_user(email='proposer@example.com', is_staff=True),
            Identity.objects.create_user(email='approver@example.com', is_staff=True))


# ── Rules ──────────────────────────────────────────────────────────────────

def test_propose_resolves_by_code_and_records_release_and_revision(choice, users):
    c = concept('55561003')
    m = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='55561003',
                    rationale='Active disease', user=users[0])
    assert (m.status, m.outcome, m.role, m.target_concept_id) == ('proposed', 'mapped', 'value_as_concept', c.pk)
    assert m.vocabulary_release == '2026-09'
    assert m.proposed_by == users[0]
    rev = m.revisions.get()
    assert (rev.revision, rev.action, rev.concept_id) == (1, 'proposed', c.pk)


@pytest.mark.parametrize('kwargs,why', [
    (dict(standard_concept=None), 'not a standard concept'),
    (dict(standard_concept='C'), 'not a standard concept'),
    (dict(invalid_reason='D'), 'is retired'),
])
def test_a_target_must_be_a_current_standard_concept(choice, kwargs, why):
    concept('1', **kwargs)
    with pytest.raises(fvm.ValueMappingError, match=why):
        fvm.propose(choice, vocabulary_id='SNOMED', concept_code='1')


def test_a_concept_not_loaded_is_refused():
    choice = FieldChoice.objects.create(field_name='disease_activity', display='X')
    with pytest.raises(fvm.ValueMappingError, match='not loaded'):
        fvm.propose(choice, vocabulary_id='SNOMED', concept_code='nope')
    with pytest.raises(fvm.ValueMappingError, match='by id, or by vocabulary and code'):
        fvm.propose(choice)


def test_a_withdrawn_vocabulary_and_non_answer_domains_are_refused(choice):
    concept('2')
    Vocabulary.objects.filter(pk='SNOMED').update(is_deprecated=True)
    with pytest.raises(fvm.ValueMappingError, match='withdrawn'):
        fvm.propose(choice, vocabulary_id='SNOMED', concept_code='2')
    Vocabulary.objects.filter(pk='SNOMED').update(is_deprecated=False)
    concept('3', vocab='UCUM', domain='Unit')
    with pytest.raises(fvm.ValueMappingError, match='not a clinical answer'):
        fvm.propose(choice, vocabulary_id='UCUM', concept_code='3')


def test_a_main_concept_must_fit_the_table_the_field_is_stored_in(choice):
    FieldConceptMapping.objects.create(field_name='disease_activity', omop_table='condition_occurrence')
    concept('4', domain='Observation')
    with pytest.raises(fvm.ValueMappingError, match='holds Condition concepts'):
        fvm.propose(choice, role='main_concept', vocabulary_id='SNOMED', concept_code='4')
    concept('5', domain='Condition')
    assert fvm.propose(choice, role='main_concept', vocabulary_id='SNOMED', concept_code='5')
    # As the value of the question the domain is not restricted that way.
    assert fvm.propose(choice, role='value_as_concept', vocabulary_id='SNOMED', concept_code='4')


def test_no_equivalent_needs_a_reason_and_has_no_target(choice):
    concept('6')
    with pytest.raises(fvm.ValueMappingError, match='Say why'):
        fvm.propose(choice, outcome='no_equivalent')
    with pytest.raises(fvm.ValueMappingError, match='has no target'):
        fvm.propose(choice, outcome='no_equivalent', concept_code='6', rationale='x')
    m = fvm.propose(choice, outcome='no_equivalent', rationale='Local grading with no standard code')
    assert (m.outcome, m.target_concept_id, m.concept_code) == ('no_equivalent', None, '')


def test_a_recipe_answer_needs_its_own_question_and_only_a_recipe_may_have_one(choice):
    concept('7')
    concept('8', domain='Measurement')
    with pytest.raises(fvm.ValueMappingError, match='question concept'):
        fvm.propose(choice, role='context_recipe', vocabulary_id='SNOMED', concept_code='7')
    with pytest.raises(fvm.ValueMappingError, match='Only an answer with its own question'):
        fvm.propose(choice, vocabulary_id='SNOMED', concept_code='7',
                    question_vocabulary_id='SNOMED', question_concept_code='8')
    m = fvm.propose(choice, role='context_recipe', vocabulary_id='SNOMED', concept_code='7',
                    question_vocabulary_id='SNOMED', question_concept_code='8')
    assert m.question_concept.concept_code == '8'


def test_a_second_proposal_replaces_the_open_one_and_keeps_its_revisions(choice):
    concept('10')
    concept('11')
    first = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='10')
    second = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='11')
    assert first.pk == second.pk
    assert Mapping.objects.filter(choice=choice).count() == 1
    assert list(second.revisions.values_list('revision', 'action', 'concept_code')) == [
        (1, 'proposed', '10'), (2, 'edited', '11')]


# ── Review ─────────────────────────────────────────────────────────────────

def test_approval_makes_it_the_decision_in_force(choice, users):
    c = concept('20')
    m = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='20', user=users[0])
    m = fvm.approve(m, user=users[1])
    assert (m.status, m.reviewer, m.reviewed_at is not None) == ('approved', users[1], True)
    assert fvm.in_force(choice).target_concept_id == c.pk
    assert [r.action for r in m.revisions.all()] == ['proposed', 'approved']
    assert m.revision == 2


def test_a_new_approval_supersedes_the_old_one_which_stays_in_the_history(choice, users):
    concept('30')
    concept('31')
    old = fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='30'), user=users[1])
    pending = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='31')
    assert fvm.in_force(choice).pk == old.pk          # still in force while a change waits
    fvm.approve(pending, user=users[1])
    old.refresh_from_db()
    assert old.status == 'superseded'
    assert fvm.in_force(choice).pk == pending.pk
    assert [r.action for r in old.revisions.all()] == ['proposed', 'approved', 'superseded']


def test_proposing_what_is_already_in_force_is_a_conflict(choice):
    concept('40')
    fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='40'))
    with pytest.raises(fvm.ConflictError):
        fvm.propose(choice, vocabulary_id='SNOMED', concept_code='40')


def test_approval_rechecks_the_target_and_refuses_an_unresolved_proposal(choice):
    c = concept('50')
    m = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='50')
    type(c).objects.filter(pk=c.pk).update(invalid_reason='D')      # retired since it was proposed
    with pytest.raises(fvm.ValueMappingError, match='is retired'):
        fvm.approve(m)
    unresolved = Mapping.objects.create(choice=choice, role='main_concept', vocabulary_id='SNOMED',
                                        concept_code='999', source_resolution='unresolved')
    with pytest.raises(fvm.ValueMappingError, match='no concept in this instance'):
        fvm.approve(unresolved)


def test_reject_and_state_errors(choice):
    concept('60')
    m = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='60')
    m = fvm.reject(m, rationale='Wrong concept')
    assert (m.status, m.rationale) == ('rejected', 'Wrong concept')
    with pytest.raises(fvm.ConflictError):
        fvm.approve(m)
    with pytest.raises(fvm.ConflictError):
        fvm.reject(m)
    # A new proposal after a rejection is a new row.
    again = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='60')
    assert again.pk != m.pk


def test_withdrawing_an_approval_leaves_the_answer_unmapped(choice):
    concept('70')
    m = fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='70'))
    fvm.reject(m)
    assert fvm.in_force(choice) is None


def test_target_state_notices_a_concept_retired_after_approval(choice):
    c = concept('80')
    m = fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='80'))
    assert fvm.target_state(m) == 'ok'
    type(c).objects.filter(pk=c.pk).update(invalid_reason='U')
    m = Mapping.objects.get(pk=m.pk)
    assert fvm.target_state(m) == 'retired'


def test_database_holds_one_approved_and_one_open_proposal_and_a_target(choice):
    c = concept('90')
    Mapping.objects.create(choice=choice, status='approved', target_concept=c)
    with pytest.raises(IntegrityError), transaction.atomic():
        Mapping.objects.create(choice=choice, status='approved', target_concept=c)
    Mapping.objects.create(choice=choice, status='proposed')
    with pytest.raises(IntegrityError), transaction.atomic():
        Mapping.objects.create(choice=choice, status='proposed')
    with pytest.raises(IntegrityError), transaction.atomic():
        Mapping.objects.create(choice=choice, role='main_concept', status='approved')       # mapped, no target
    with pytest.raises(IntegrityError), transaction.atomic():
        Mapping.objects.create(choice=choice, role='main_concept', outcome='no_equivalent',
                               target_concept=c)
    with pytest.raises(IntegrityError), transaction.atomic():
        Mapping.objects.create(choice=choice, role='main_concept', question_concept=c)


def test_coverage_counts_answers_by_best_state_and_leaves_out_retired():
    from omop_core.services import field_choices
    a, b, c, d, gone = (FieldChoice.objects.create(field_name='stage', display=n)
                        for n in ('A', 'B', 'C', 'D', 'Gone'))
    concept('100')
    fvm.approve(fvm.propose(a, vocabulary_id='SNOMED', concept_code='100'))
    fvm.approve(fvm.propose(b, outcome='no_equivalent', rationale='local'))
    fvm.propose(c, vocabulary_id='SNOMED', concept_code='100')
    field_choices.set_retired(gone, True)
    assert fvm.coverage('stage') == {'stage': {
        'choices': 4, 'mapped': 1, 'no_equivalent': 1, 'proposed': 1, 'unmapped': 1}}
    assert d.pk


# ── Migration 0368 ─────────────────────────────────────────────────────────

def _carry():
    module = importlib.import_module('omop_core.migrations.0368_propose_value_mappings_from_codes')
    module.propose_from_primary_codes(apps, None)
    return module


def test_primary_codes_become_proposals_and_never_approvals():
    resolved = FieldChoice.objects.create(field_name='f', display='Resolved')
    missing = FieldChoice.objects.create(field_name='f', display='Missing')
    retired = FieldChoice.objects.create(field_name='f', display='Retired')
    nonstd = FieldChoice.objects.create(field_name='f', display='NonStd')
    plain = FieldChoice.objects.create(field_name='f', display='Plain')
    two = FieldChoice.objects.create(field_name='f', display='Two')
    c = concept('200')
    concept('201', invalid_reason='D')
    concept('202', standard_concept=None)
    FieldChoiceCode.objects.create(choice=resolved, vocabulary_id='SNOMED', code='200', is_primary=True)
    FieldChoiceCode.objects.create(choice=missing, vocabulary_id='SNOMED', code='nope', is_primary=True)
    FieldChoiceCode.objects.create(choice=retired, vocabulary_id='SNOMED', code='201', is_primary=True)
    FieldChoiceCode.objects.create(choice=nonstd, vocabulary_id='SNOMED', code='202', is_primary=True)
    FieldChoiceCode.objects.create(choice=plain, vocabulary_id='SNOMED', code='200')   # not primary
    FieldChoiceCode.objects.create(choice=two, vocabulary_id='SNOMED', code='200', is_primary=True)
    FieldChoiceCode.objects.create(choice=two, vocabulary_id='LOCAL', code='x', is_primary=True)
    _carry()
    by = {m.choice.display: m for m in Mapping.objects.select_related('choice')}
    assert set(by) == {'Resolved', 'Missing', 'Retired', 'NonStd', 'Two'}
    assert {m.status for m in by.values()} == {'proposed'}
    assert [(n, by[n].source_resolution) for n in ('Resolved', 'Missing', 'Retired', 'NonStd')] == [
        ('Resolved', 'resolved'), ('Missing', 'unresolved'), ('Retired', 'invalid'),
        ('NonStd', 'non_standard')]
    assert by['Resolved'].target_concept_id == c.pk and by['Missing'].target_concept_id is None
    assert by['Two'].vocabulary_id == 'SNOMED'                  # the first primary, by id
    assert by['Resolved'].revisions.get().action == 'migrated'
    before = Mapping.objects.count()
    _carry()                                                    # a replay changes nothing
    assert Mapping.objects.count() == before


# ── API ────────────────────────────────────────────────────────────────────

@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='vm-staff@example.com', is_staff=True))
    return client


@pytest.fixture
def reader():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='vm-reader@example.com'))
    return client


def url(choice, *tail):
    return f'/api/v1/field-choices/{choice.pk}/value-mappings/' + ''.join(f'{t}/' for t in tail)


def test_propose_approve_and_read_back_through_the_api(staff, choice):
    c = concept('300')
    r = staff.post(url(choice), {'vocabulary_id': 'SNOMED', 'concept_code': '300',
                                 'rationale': 'ok'}, format='json')
    assert r.status_code == 201, r.data
    body = r.json()
    assert (body['status'], body['target']['concept_id'], body['target_state']) == ('proposed', c.pk, None)
    r = staff.post(url(choice, body['id'], 'approve'), {}, format='json')
    assert r.status_code == 200, r.data
    assert (r.json()['status'], r.json()['target_state']) == ('approved', 'ok')
    listed = staff.get(url(choice)).json()
    assert [m['id'] for m in listed] == [body['id']]
    revs = staff.get(url(choice, body['id'], 'revisions')).json()
    assert [x['action'] for x in revs] == ['proposed', 'approved']


def test_the_choice_list_carries_mappings_only_for_those_who_can_see_them(staff, reader, choice):
    concept('310')
    fvm.propose(choice, vocabulary_id='SNOMED', concept_code='310')
    assert staff.get('/api/v1/field-choices/?field_name=disease_activity').json()[0]['value_mappings']
    assert 'value_mappings' not in reader.get('/api/v1/field-choices/?field_name=disease_activity').json()[0]
    assert 'value_mappings' in staff.get(f'/api/v1/field-choices/{choice.pk}/').json()
    assert 'value_mappings' not in reader.get(f'/api/v1/field-choices/{choice.pk}/').json()


def test_permissions(reader, choice):
    concept('320')
    assert reader.get(url(choice)).status_code == 403
    assert reader.post(url(choice), {'vocabulary_id': 'SNOMED', 'concept_code': '320'},
                       format='json').status_code == 403
    m = fvm.propose(choice, vocabulary_id='SNOMED', concept_code='320')
    assert reader.post(url(choice, m.pk, 'approve'), {}, format='json').status_code == 403
    assert reader.get(url(choice, m.pk, 'revisions')).status_code == 403
    assert reader.get('/api/v1/field-choices/answer-coverage/').status_code == 403


def test_a_curator_who_is_not_an_approver_can_propose_but_not_approve(choice):
    from omop_core.models import GroupAccess, Organization
    org = Organization.objects.create(name='Org', slug='org')
    user = Identity.objects.create_user(email='analyst-vm@example.com')
    GroupAccess.objects.create(identity=user, org=org, role='analyst')
    client = APIClient()
    client.force_authenticate(user)
    concept('330')
    r = client.post(url(choice), {'vocabulary_id': 'SNOMED', 'concept_code': '330'}, format='json')
    assert r.status_code == 201
    assert client.post(url(choice, r.json()['id'], 'approve'), {}, format='json').status_code == 403
    assert client.post(url(choice, r.json()['id'], 'reject'), {}, format='json').status_code == 403


def test_api_errors_are_400_or_409_with_a_reason(staff, choice):
    concept('340')
    r = staff.post(url(choice), {'vocabulary_id': 'SNOMED', 'concept_code': 'nope'}, format='json')
    assert r.status_code == 400 and 'not loaded' in r.json()['detail']
    r = staff.post(url(choice), {'role': 'bogus', 'concept_code': 'x'}, format='json')
    assert r.status_code == 400
    r = staff.post(url(choice), {'vocabulary_id': 'SNOMED', 'concept_code': '340'}, format='json')
    mid = r.json()['id']
    staff.post(url(choice, mid, 'approve'), {}, format='json')
    assert staff.post(url(choice, mid, 'approve'), {}, format='json').status_code == 409
    assert staff.post(url(choice, mid, 'nonsense'), {}, format='json').status_code == 404
    assert staff.post(url(choice, 999999, 'approve'), {}, format='json').status_code == 404
    other = FieldChoice.objects.create(field_name='disease_activity', display='Other')
    assert staff.post(url(other, mid, 'approve'), {}, format='json').status_code == 404


def test_answer_coverage_endpoint_and_descriptor(staff, choice):
    from omop_core.services.field_descriptor import get_all_field_descriptors
    concept('350')
    fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='350'))
    body = staff.get('/api/v1/field-choices/answer-coverage/?field_name=disease_activity').json()
    assert body['disease_activity']['mapped'] == 1
    d = next(x for x in get_all_field_descriptors() if x['field_name'] == 'disease_activity')
    assert d['answer_coverage']['mapped'] == 1


def test_list_response_carries_the_figures_the_answers_column_shows(staff, choice):
    """The Field Mappings list reads ``answer_coverage`` from its own response:
    counted over the answers still offered, null for a field with none."""
    from django.utils import timezone
    concept('351')
    fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='351'))
    FieldChoice.objects.create(field_name=choice.field_name, display='Open one')
    FieldChoice.objects.create(field_name=choice.field_name, display='Gone', retired_at=timezone.now())
    body = staff.get('/api/v1/field-mappings/').json()
    by_name = {d['field_name']: d for d in body}
    assert by_name[choice.field_name]['answer_coverage'] == {
        'choices': 2, 'mapped': 1, 'no_equivalent': 0, 'proposed': 0, 'unmapped': 1}
    assert by_name['weight']['answer_coverage'] is None


def test_list_query_count_does_not_grow_with_the_choices(staff):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    concept('360')

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert staff.get('/api/v1/field-choices/?field_name=stage').status_code == 200
        return len(ctx)

    first = FieldChoice.objects.create(field_name='stage', display='First')
    fvm.propose(first, vocabulary_id='SNOMED', concept_code='360')
    small = count()
    for n in range(8):
        extra = FieldChoice.objects.create(field_name='stage', display=f'More {n}')
        fvm.propose(extra, vocabulary_id='SNOMED', concept_code='360')
    assert count() == small


# ── Transfer ───────────────────────────────────────────────────────────────

def _decided():
    choice = FieldChoice.objects.create(field_name='stage', display='III', sort_order=3)
    concept('400')
    approved = fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='400',
                                       rationale='checked'))
    fvm.propose(choice, role='main_concept', outcome='no_equivalent', rationale='none')
    return choice, approved


def test_round_trip_carries_the_decision_in_force_and_the_open_proposal_by_code(users):
    choice, approved = _decided()
    payload = read_payload('default')
    row = next(r for r in payload['choices'] if r['display'] == 'III')
    assert {(m['role'], m['status']) for m in row['value_mappings']} == {
        ('value_as_concept', 'approved'), ('main_concept', 'proposed')}
    assert 'reviewer' not in row['value_mappings'][0]

    FieldChoice.objects.all().delete()
    apply_payload(payload)

    back = FieldChoice.objects.get(display='III')
    got = {(m.role, m.status): m for m in back.value_mappings.all()}
    assert got[('value_as_concept', 'approved')].target_concept.concept_code == '400'
    assert got[('value_as_concept', 'approved')].reviewer is None
    assert got[('main_concept', 'proposed')].outcome == 'no_equivalent'
    assert got[('value_as_concept', 'approved')].revisions.get().action == 'transferred'


def test_transfer_keeps_an_approval_as_a_proposal_when_the_concept_is_not_loaded_here():
    choice, approved = _decided()
    payload = read_payload('default')
    FieldChoice.objects.all().delete()
    type(approved.target_concept).objects.all().delete()
    stats = apply_payload(payload)
    back = FieldChoice.objects.get(display='III').value_mappings.get(role='value_as_concept')
    assert (back.status, back.target_concept_id, back.source_resolution) == ('proposed', None, 'unresolved')
    assert back.concept_code == '400'
    assert any('not loaded' in w for w in stats.warnings)


def test_transfer_supersedes_what_the_source_no_longer_has_in_force():
    choice, approved = _decided()
    payload = read_payload('default')
    row = next(r for r in payload['choices'] if r['display'] == 'III')
    row['value_mappings'] = []                      # the source un-approved and dropped both
    apply_payload(payload)
    states = {m.role: m.status for m in choice.value_mappings.all()}
    assert states == {'value_as_concept': 'superseded', 'main_concept': 'rejected'}


def test_transfer_reads_a_payload_from_before_value_mappings():
    choice, approved = _decided()
    payload = read_payload('default')
    for row in payload['choices']:
        row.pop('value_mappings', None)
    apply_payload(payload)
    assert choice.value_mappings.filter(status='approved').count() == 1


# ── Review findings ────────────────────────────────────────────────────────

def test_list_query_count_stays_flat_for_approved_mappings_too(staff):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    concept('500')

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert staff.get('/api/v1/field-choices/?field_name=stage').status_code == 200
        return len(ctx)

    first = FieldChoice.objects.create(field_name='stage', display='First')
    fvm.approve(fvm.propose(first, vocabulary_id='SNOMED', concept_code='500'))
    small = count()
    for n in range(8):
        extra = FieldChoice.objects.create(field_name='stage', display=f'More {n}')
        fvm.approve(fvm.propose(extra, vocabulary_id='SNOMED', concept_code='500'))
    assert count() == small
    body = staff.get('/api/v1/field-choices/?field_name=stage').json()
    assert all(c['value_mappings'][0]['target_state'] == 'ok' for c in body)


def test_deleting_an_answer_with_an_approved_mapping_is_for_approvers(staff, choice):
    from omop_core.models import GroupAccess, Organization
    concept('510')
    fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='510'))
    org = Organization.objects.create(name='Org', slug='org-del')
    analyst = Identity.objects.create_user(email='analyst-del@example.com')
    GroupAccess.objects.create(identity=analyst, org=org, role='analyst')
    client = APIClient()
    client.force_authenticate(analyst)
    assert client.delete(f'/api/v1/field-choices/{choice.pk}/').status_code == 403
    assert Mapping.objects.filter(choice=choice).exists()
    assert staff.delete(f'/api/v1/field-choices/{choice.pk}/').status_code == 204


def test_a_curator_who_is_not_an_approver_cannot_delete_an_answer_even_with_only_a_proposal(staff, choice):
    from omop_core.models import GroupAccess, Organization
    concept('511')
    fvm.propose(choice, vocabulary_id='SNOMED', concept_code='511')
    org = Organization.objects.create(name='Org', slug='org-del2')
    analyst = Identity.objects.create_user(email='analyst-del2@example.com')
    GroupAccess.objects.create(identity=analyst, org=org, role='analyst')
    client = APIClient()
    client.force_authenticate(analyst)
    assert client.delete(f'/api/v1/field-choices/{choice.pk}/').status_code == 403
    assert Mapping.objects.filter(choice=choice).exists()
    assert staff.delete(f'/api/v1/field-choices/{choice.pk}/').status_code == 204


def test_a_non_object_body_is_a_400_not_a_500(staff, choice):
    r = staff.post(url(choice), [1, 2], format='json')
    assert r.status_code == 400


def test_transfer_keeps_the_sources_open_proposal_when_its_approval_cannot_apply():
    choice = FieldChoice.objects.create(field_name='stage', display='III')
    concept('400')
    concept('401')
    fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='400'))
    fvm.propose(choice, vocabulary_id='SNOMED', concept_code='401')
    payload = read_payload('default')
    FieldChoice.objects.all().delete()
    Mapping.objects.all().delete()
    type(concept('400')).objects.filter(concept_code='400').delete()
    apply_payload(payload)
    back = FieldChoice.objects.get(display='III').value_mappings.get()
    assert (back.status, back.concept_code) == ('proposed', '401')


def test_transfer_leaves_a_valid_local_approval_when_the_incoming_one_cannot_apply():
    choice = FieldChoice.objects.create(field_name='stage', display='III')
    local = concept('600')
    fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='600'))
    payload = {'choices': [{
        'field_name': 'stage', 'display': 'III', 'sort_order': 0, 'codes': [], 'code': choice.code,
        'value_mappings': [{'role': 'value_as_concept', 'outcome': 'mapped', 'status': 'approved',
                            'vocabulary_id': 'SNOMED', 'concept_code': 'not-loaded-here'}],
    }]}
    stats = apply_payload(payload)
    states = {(m.status, m.concept_code) for m in choice.value_mappings.all()}
    assert states == {('approved', '600'), ('proposed', 'not-loaded-here')}
    assert any('not loaded' in w for w in stats.warnings)
    assert fvm.in_force(choice).target_concept_id == local.pk


def test_transfer_skips_malformed_rows_with_a_warning_instead_of_failing():
    choice = FieldChoice.objects.create(field_name='stage', display='III')
    concept('700')
    stats = apply_payload({'choices': [{
        'field_name': 'stage', 'display': 'III', 'sort_order': 0, 'codes': [], 'code': choice.code,
        'value_mappings': [
            {'role': 'x' * 40, 'outcome': 'mapped', 'status': 'approved'},
            {'role': 'value_as_concept', 'status': 'approved'},
            {'role': 'value_as_concept', 'outcome': 'no_equivalent', 'status': 'proposed',
             'concept_code': '700', 'vocabulary_id': 'SNOMED'},
            {'role': 'value_as_concept', 'outcome': 'mapped', 'status': 'proposed',
             'vocabulary_id': 'SNOMED', 'concept_code': '700', 'question_concept_code': 'q'},
        ],
    }]})
    assert len([w for w in stats.warnings if 'skipped' in w]) == 4
    assert not choice.value_mappings.exists()


def test_a_transferred_row_has_no_reviewer_and_no_review_date():
    choice, approved = _decided()
    payload = read_payload('default')
    FieldChoice.objects.all().delete()
    apply_payload(payload)
    row = FieldChoice.objects.get(display='III').value_mappings.get(status='approved')
    assert (row.reviewer, row.reviewed_at) == (None, None)
    apply_payload(payload)                         # in place: still none
    row.refresh_from_db()
    assert (row.reviewer, row.reviewed_at) == (None, None)


# ── Resolving carried-over proposals after a vocabulary load ───────────────

def test_proposals_carried_over_before_the_vocabulary_load_resolve_afterwards():
    ok = FieldChoice.objects.create(field_name='f', display='Ok')
    old = FieldChoice.objects.create(field_name='f', display='Old')
    absent = FieldChoice.objects.create(field_name='f', display='Absent')
    for ch, code in ((ok, '800'), (old, '801'), (absent, '802')):
        FieldChoiceCode.objects.create(choice=ch, vocabulary_id='SNOMED', code=code, is_primary=True)
    _carry()                                       # no concepts loaded yet
    assert set(Mapping.objects.values_list('source_resolution', flat=True)) == {'unresolved'}
    c = concept('800')
    concept('801', invalid_reason='D')
    assert fvm.resolve_unresolved(dry_run=True) == {
        'resolved': 1, 'invalid': 1, 'non_standard': 0, 'still_unresolved': 1}
    assert Mapping.objects.filter(target_concept__isnull=False).count() == 0
    assert fvm.resolve_unresolved() == {
        'resolved': 1, 'invalid': 1, 'non_standard': 0, 'still_unresolved': 1}
    got = {m.choice.display: m for m in Mapping.objects.select_related('choice')}
    assert (got['Ok'].source_resolution, got['Ok'].target_concept_id) == ('resolved', c.pk)
    assert got['Ok'].vocabulary_release == '2026-09'
    assert got['Old'].source_resolution == 'invalid' and got['Absent'].source_resolution == 'unresolved'
    assert [r.action for r in got['Ok'].revisions.all()] == ['migrated', 'resolved']
    revisions = Revision.objects.count()
    fvm.resolve_unresolved()                       # a replay changes nothing
    assert Revision.objects.count() == revisions
    fvm.approve(got['Ok'])                         # and the resolved one can now be approved
    with pytest.raises(fvm.ValueMappingError, match='is retired'):
        fvm.approve(got['Old'])


def test_the_resolver_command_runs_and_never_touches_an_approved_mapping(choice):
    from django.core.management import call_command
    from io import StringIO
    concept('810')
    approved = fvm.approve(fvm.propose(choice, vocabulary_id='SNOMED', concept_code='810'))
    out = StringIO()
    call_command('resolve_field_value_mappings', '--dry-run', stdout=out)
    assert 'Would resolve 0' in out.getvalue()
    call_command('resolve_field_value_mappings', stdout=out)
    approved.refresh_from_db()
    assert approved.status == 'approved' and approved.revision == 2


# ── Target search (#1225) ──────────────────────────────────────────────────

def targets(client, choice, **params):
    return client.get(url(choice, 'targets'), params)


def test_the_target_search_lists_only_what_a_proposal_would_accept(staff, choice):
    ok = concept('600')
    concept('601', standard_concept=None)
    concept('602', standard_concept='C')
    concept('603', invalid_reason='D')
    concept('604', vocab='UCUM', domain='Unit')
    r = targets(staff, choice, q='SNOMED 60')
    assert r.status_code == 200, r.data
    assert [c['concept_id'] for c in r.json()['results']] == [ok.pk]
    # Whatever it lists, a proposal accepts.
    assert fvm.propose(choice, concept_id=ok.pk).target_concept_id == ok.pk
    # A concept is also found by exact code and by OMOP id.
    assert [c['concept_id'] for c in targets(staff, choice, q='600').json()['results']] == [ok.pk]
    assert [c['concept_id'] for c in targets(staff, choice, q=str(ok.pk)).json()['results']] == [ok.pk]


def test_the_target_search_leaves_out_a_withdrawn_vocabulary(staff, choice):
    concept('610')
    concept('611', vocab='OLDVOC')
    Vocabulary.objects.filter(pk='OLDVOC').update(is_deprecated=True)
    r = targets(staff, choice, q='610').json()
    assert r['results'] and r['rules']['excluded_vocabularies'] == ['OLDVOC']
    assert targets(staff, choice, q='611').json()['results'] == []
    assert [c['vocabulary_id'] for c in targets(staff, choice, q='SNOMED 61', vocabulary_id='SNOMED')
            .json()['results']] == ['SNOMED']


def test_a_main_concept_search_is_held_to_the_domain_of_the_fields_table(staff, choice):
    FieldConceptMapping.objects.create(field_name='disease_activity', omop_table='condition_occurrence')
    concept('620', domain='Observation')
    cond = concept('621', domain='Condition')
    r = targets(staff, choice, q='SNOMED 62', role='main_concept').json()
    assert [c['concept_id'] for c in r['results']] == [cond.pk]
    assert r['rules']['required_domain'] == 'Condition'
    # As the value of the question, and as a question concept, the domain is free.
    assert len(targets(staff, choice, q='SNOMED 62').json()['results']) == 2
    assert len(targets(staff, choice, q='SNOMED 62', role='context_recipe',
                       question='1').json()['results']) == 2
    # And what is listed for the role is what a proposal for that role accepts.
    assert fvm.propose(choice, role='main_concept', concept_id=cond.pk)


def test_the_target_search_refuses_what_it_cannot_search(staff, reader, choice):
    assert targets(staff, choice, q='ab').status_code == 400
    assert targets(staff, choice, q='abc', role='nonsense').status_code == 400
    assert targets(staff, choice, q='abc', question='1').status_code == 400   # not a recipe
    assert staff.get('/api/v1/field-choices/999999/value-mappings/targets/', {'q': 'abc'}).status_code == 404
    assert targets(reader, choice, q='abc').status_code == 403
    assert APIClient().get(url(choice, 'targets'), {'q': 'abc'}).status_code in (401, 403)


def test_the_target_search_query_count_is_flat(staff, choice, django_assert_max_num_queries):
    for i in range(30):
        concept(f'63{i:02d}')
    with django_assert_max_num_queries(12):
        assert len(targets(staff, choice, q='SNOMED 63').json()['results']) == 25
