"""A concept decision about one entry of a field's lookup (#1225).

The decision is held by an answer row made when a curator first decides
something for the entry, in its own context. It is not an answer a patient is
offered and it does not copy the lookup.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    EcogStatus, FieldChoice, FieldConceptMapping, GroupAccess, Her2Status,
)
from omop_core.services import field_value_mappings as fvm
from omop_core.services import option_sources
from omop_core.services.field_descriptor import get_all_field_descriptors
from omop_core.services.write_descriptor import _field_choice_options
from patient_portal.models import Identity
from tests.factories import ConceptFactory, DomainFactory, OrganizationFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

LOOKUP = '/api/v1/field-choices/lookup/'
ANSWER = '/api/v1/field-choices/lookup/answer/'
CHOICES = '/api/v1/field-choices/'


def as_role(role):
    user = Identity.objects.create_user(email=f'lookup-entry-{role}@example.org', is_staff=role == 'staff')
    if role != 'staff':
        GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


def concept(code):
    VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED', vocabulary_version='2026-09')
    DomainFactory(domain_id='Observation', domain_name='Observation')
    return ConceptFactory(vocabulary_id='SNOMED', concept_code=code, domain_id='Observation',
                          concept_name=f'SNOMED {code}')


@pytest.fixture
def her2():
    Her2Status.objects.create(code='pos', title='Positive')
    Her2Status.objects.create(code='neg', title='Negative')


def make(field='her2_status', code='pos', client=None):
    response = (client or as_role('analyst')).post(ANSWER, {'field_name': field, 'code': code}, format='json')
    assert response.status_code == 200, response.data
    return FieldChoice.objects.get(pk=response.data['choice_id'])


# ── Making the row ─────────────────────────────────────────────────────────

def test_the_row_is_made_once_in_the_lookups_own_context(her2):
    first, second = make(), make()
    assert first.pk == second.pk
    assert (first.field_name, first.context_key, first.display) == ('her2_status', 'lookup:her2-status', 'Positive')
    assert FieldChoice.objects.count() == 1


def test_a_code_valued_field_stores_the_entrys_code():
    EcogStatus.objects.create(code='1', title='Restricted in strenuous activity')
    row = make('ecog_performance_status', '1')
    assert row.display == 'Restricted in strenuous activity' and row.stored_value == '1'


def test_a_title_valued_field_stores_the_title(her2):
    assert make().stored_value == 'Positive'


def test_only_mapping_curators_make_it(her2):
    for role, expected in (('doctor', 403), ('patient', 403), ('org_admin', 200), ('analyst', 200)):
        response = as_role(role).post(ANSWER, {'field_name': 'her2_status', 'code': 'pos'}, format='json')
        assert response.status_code == expected, role


@pytest.mark.parametrize('body,status', (
    ({'field_name': 'hemoglobin_g_dl', 'code': 'x'}, 404),   # no lookup
    ({'field_name': 'her2_status', 'code': 'nope'}, 404),    # no such entry
    ({'field_name': 'her2_status'}, 400),
    ({}, 400),
))
def test_a_field_or_entry_that_is_not_there_makes_nothing(her2, body, status):
    assert as_role('analyst').post(ANSWER, body, format='json').status_code == status
    assert FieldChoice.objects.count() == 0


# ── It is not an answer ────────────────────────────────────────────────────

def test_it_is_not_listed_offered_or_counted(her2):
    make()
    FieldChoice.objects.create(field_name='her2_status', display='Equivocal')
    staff = as_role('staff')
    listed = [c['display'] for c in staff.get(CHOICES, {'field_name': 'her2_status'}).json()]
    assert listed == ['Equivocal']
    assert [c['display'] for c in staff.get(CHOICES, {'field_name': 'her2_status', 'disease': 'Other'}).json()] \
        == ['Equivocal']
    assert list(_field_choice_options().get('her2_status', [])) == [('Equivocal', None, 'Equivocal')]
    descriptor = next(d for d in get_all_field_descriptors() if d['field_name'] == 'her2_status')
    assert [c['display'] for c in descriptor['choices']] == ['Equivocal']
    assert fvm.coverage('her2_status')['her2_status']['choices'] == 1
    from omop_core.services import field_choices
    assert [c.display for c in field_choices.offered('her2_status')] == ['Equivocal']


def test_nobody_can_change_it_as_an_answer(her2):
    row = make()
    admin = as_role('org_admin')
    assert admin.patch(f'{CHOICES}{row.pk}/', {'display': 'Positive!'}, format='json').status_code == 400
    assert admin.patch(f'{CHOICES}{row.pk}/', {'retired': True}, format='json').status_code == 400
    body = {'code': '1', 'vocabulary_id': 'SNOMED', 'is_primary': True}
    assert admin.post(f'{CHOICES}{row.pk}/codes/', body, format='json').status_code == 400
    row.refresh_from_db()
    assert row.display == 'Positive' and row.retired_at is None and row.codes.count() == 0


def test_nobody_can_make_one_by_hand():
    response = as_role('org_admin').post(CHOICES, {'field_name': 'her2_status', 'display': 'X',
                                                    'context_key': 'lookup:her2-status'}, format='json')
    assert response.status_code == 400 and 'context_key' in response.data
    assert FieldChoice.objects.count() == 0


# ── Deciding ───────────────────────────────────────────────────────────────

def decide(row, code='1', approve=True):
    concept(code)
    analyst = as_role('analyst')
    r = analyst.post(f'{CHOICES}{row.pk}/value-mappings/', {'vocabulary_id': 'SNOMED', 'concept_code': code,
                                                            'rationale': 'ok'}, format='json')
    assert r.status_code == 201, r.data
    if approve:
        a = as_role('org_admin').post(f"{CHOICES}{row.pk}/value-mappings/{r.data['id']}/approve/", {}, format='json')
        assert a.status_code == 200, a.data
    return r.data['id']


def test_a_curator_proposes_and_an_approver_approves_through_the_answer_endpoints(her2):
    row = make()
    decide(row)
    body = as_role('doctor').get(LOOKUP, {'field_name': 'her2_status'}).json()
    positive = next(o for o in body['options'] if o['title'] == 'Positive')
    negative = next(o for o in body['options'] if o['title'] == 'Negative')
    assert positive['choice_id'] == row.pk
    assert [m['status'] for m in positive['value_mappings']] == ['approved']
    assert negative['choice_id'] is None and negative['value_mappings'] == []
    assert body['orphaned'] == []


def test_the_approved_concept_is_what_a_stored_value_writes_and_reads_back(her2):
    row = make()
    decide(row, '55561003')
    c = ConceptFactory._meta.model.objects.get(concept_code='55561003')
    assert fvm.answer_concept_ids(['her2_status'])['her2_status']['Positive'] == c.pk
    assert fvm.answer_values_by_concept(['her2_status'])['her2_status'][c.pk] == 'Positive'


def test_a_code_valued_field_reads_back_the_code_the_tab_stores():
    EcogStatus.objects.create(code='1', title='Restricted in strenuous activity')
    row = make('ecog_performance_status', '1')
    decide(row, '722')
    c = ConceptFactory._meta.model.objects.get(concept_code='722')
    ids = fvm.answer_concept_ids(['ecog_performance_status'])['ecog_performance_status']
    assert ids['1'] == c.pk and ids['Restricted in strenuous activity'] == c.pk
    assert fvm.answer_values_by_concept(['ecog_performance_status'])['ecog_performance_status'][c.pk] == '1'


def test_a_renamed_entry_leaves_its_decision_listed_as_orphaned(her2):
    row = make()
    decide(row)
    Her2Status.objects.filter(code='pos').update(title='Positive (3+)')
    body = as_role('doctor').get(LOOKUP, {'field_name': 'her2_status'}).json()
    assert {o['title'] for o in body['options']} == {'Positive (3+)', 'Negative'}
    renamed = next(o for o in body['options'] if o['title'] == 'Positive (3+)')
    assert renamed['choice_id'] is None
    assert [o['choice_id'] for o in body['orphaned']] == [row.pk]
    assert body['orphaned'][0]['title'] == 'Positive'


def test_an_enforced_lookup_still_takes_decisions(her2):
    FieldConceptMapping.objects.update_or_create(
        field_name='her2_status', defaults={'status': 'approved', 'value_vocabulary': 'Her2Status',
                                            'concept': concept('1'), 'omop_table': 'Observation'})
    row = make()
    assert row.context_key == 'lookup:her2-status'
    assert as_role('staff').get(LOOKUP, {'field_name': 'her2_status'}).json()['source']['enforced'] is True


def test_the_lookup_listing_costs_the_same_however_many_entries_are_decided(her2):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client = as_role('doctor')
    concept('9')

    def queries():
        with CaptureQueriesContext(connection) as captured:
            assert client.get(LOOKUP, {'field_name': 'her2_status'}).status_code == 200
        return len(captured)

    row = make()
    fvm.propose(row, vocabulary_id='SNOMED', concept_code='9')
    one = queries()
    for i in range(6):
        Her2Status.objects.create(code=f'x{i}', title=f'Entry {i}')
        other = make(code=f'x{i}')
        fvm.propose(other, vocabulary_id='SNOMED', concept_code='9')
    assert queries() == one


def test_a_decision_about_an_entry_travels_with_the_curation_payload(her2):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    row = make()
    decide(row, '55561003')
    payload = read_payload('default')
    carried = next(r for r in payload['choices'] if r['display'] == 'Positive')
    assert carried['context_key'] == 'lookup:her2-status' and carried['value_mappings']
    FieldChoice.objects.all().delete()
    apply_payload(payload)
    back = FieldChoice.objects.get(display='Positive', context_key='lookup:her2-status')
    assert back.value_mappings.get(role='value_as_concept').status == 'approved'
    # and it is still not an answer on the instance it arrived at
    assert [c['display'] for c in as_role('staff').get(CHOICES, {'field_name': 'her2_status'}).json()] == []


# ── A curated answer speaks over a lookup entry ────────────────────────────

def _decided_answer(field, display, code):
    row = FieldChoice.objects.create(field_name=field, display=display)
    fvm.approve(fvm.propose(row, vocabulary_id='SNOMED', concept_code=code))
    return row


def test_a_lookup_decision_does_not_void_or_override_a_curated_answer_with_the_same_spelling(her2):
    x, y = concept('100'), concept('200')
    _decided_answer('her2_status', 'Positive', '100')
    decide(make(), '200')
    assert fvm.answer_concept_ids(['her2_status'])['her2_status']['Positive'] == x.pk
    assert y.pk not in fvm.answer_concept_ids(['her2_status'])['her2_status'].values()


def test_a_lookup_decision_fills_the_spellings_no_curated_answer_uses(her2):
    concept('100')
    _decided_answer('her2_status', 'Equivocal', '100')
    y = concept('200')
    decide(make(), '200')
    ids = fvm.answer_concept_ids(['her2_status'])['her2_status']
    assert ids['Positive'] == y.pk and 'Equivocal' in ids


def test_a_curated_answer_keeps_the_readback_of_a_concept_a_lookup_entry_shares(her2):
    x = concept('100')
    _decided_answer('her2_status', 'HER2 positive', '100')
    decide(make(), '100')
    assert fvm.answer_values_by_concept(['her2_status'])['her2_status'][x.pk] == 'HER2 positive'


def test_an_undecided_lookup_row_changes_neither_direction(her2):
    x = concept('100')
    _decided_answer('her2_status', 'Positive', '100')
    before = (fvm.answer_concept_ids(['her2_status']), fvm.answer_values_by_concept(['her2_status']))
    make(code='neg')
    assert (fvm.answer_concept_ids(['her2_status']), fvm.answer_values_by_concept(['her2_status'])) == before
    assert before[0]['her2_status'] == {'Positive': x.pk}


# ── Other review findings ──────────────────────────────────────────────────

def test_a_row_that_holds_decisions_cannot_be_deleted_even_by_an_approver(her2):
    row = make()
    decide(row)
    response = as_role('org_admin').delete(f'{CHOICES}{row.pk}/')
    assert response.status_code == 400 and 'Withdraw or reject' in response.data['detail']
    assert FieldChoice.objects.filter(pk=row.pk).exists() and row.value_mappings.count() == 1


def test_an_entry_whose_title_cannot_fit_makes_nothing():
    Her2Status.objects.create(code='long', title='x' * 201)
    response = as_role('analyst').post(ANSWER, {'field_name': 'her2_status', 'code': 'long'}, format='json')
    assert response.status_code == 404 and 'too long' in response.data['detail']
    assert FieldChoice.objects.count() == 0


@pytest.mark.parametrize('kind,expected', (('concept', True), ('string', False)))
def test_the_listing_says_whether_the_fields_mapping_writes_a_concept(her2, kind, expected):
    FieldConceptMapping.objects.update_or_create(
        field_name='her2_status', defaults={'status': 'approved', 'value_kind': kind})
    source = as_role('doctor').get(LOOKUP, {'field_name': 'her2_status'}).json()['source']
    assert source['writes_concepts'] is expected
