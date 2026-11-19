"""The two-person rule for code mappings: a proposer cannot approve (#2726)."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from omop_core.mapping import axis_variants
from omop_core.models import (
    GroupAccess, Organization, SourceCodeAxisVariant, SourceCodeConceptMapping, SuggestRun,
)
from omop_core.services import mapping_approval
from omop_core.services.group_mapping_jobs import _apply_group_action
from patient_portal.models import Identity
from tests.factories import ConceptFactory

LIST = '/api/v1/code-mappings/'


def detail(mapping):
    return f'/api/v1/code-mappings/{mapping.pk}/'


def make_staff(name):
    return Identity.objects.create_user(email=f'{name}@example.com', password='x', is_staff=True)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture(autouse=True)
def rule_on(settings):
    """The rule is off under DEBUG, which the test runs set; these tests want it."""
    settings.MAPPING_SEPARATE_APPROVER = True


@pytest.fixture
def alice():
    return make_staff('alice')


@pytest.fixture
def bob():
    return make_staff('bob')


@pytest.fixture
def lab_concept():
    return ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')


@pytest.fixture
def other_concept():
    return ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')


def propose(user, concept, code='A01', **extra):
    response = client_for(user).post(LIST, {
        'source_vocabulary_id': 'VendorLab', 'source_code': code,
        'source_code_description': 'Albumin', 'destination_concept_id': concept.pk,
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'status': 'proposed', **extra,
    }, format='json')
    assert response.status_code == 201, response.data
    return SourceCodeConceptMapping.objects.get(pk=response.data['mapping_id'])


def approve(user, mapping, **extra):
    return client_for(user).patch(detail(mapping), {'status': 'approved', **extra}, format='json')


@pytest.mark.django_db
def test_the_proposer_cannot_approve_but_another_approver_can(alice, bob, lab_concept):
    mapping = propose(alice, lab_concept)
    assert mapping.proposed_by == alice

    refused = approve(alice, mapping)
    assert refused.status_code == 403
    assert refused.data['detail'] == mapping_approval.SELF_APPROVAL_MESSAGE
    assert refused.data['code'] == 'self_approval'
    assert refused.data['detail'].startswith('You proposed this mapping')
    mapping.refresh_from_db()
    assert (mapping.status, mapping.reviewer) == ('proposed', None)

    accepted = approve(bob, mapping)
    assert accepted.status_code == 200, accepted.data
    mapping.refresh_from_db()
    assert (mapping.status, mapping.reviewer, mapping.proposed_by) == ('approved', bob, alice)


@pytest.mark.django_db
def test_creating_a_mapping_already_approved_is_a_self_approval(alice, lab_concept):
    response = client_for(alice).post(LIST, {
        'source_vocabulary_id': 'VendorLab', 'source_code': 'A01',
        'destination_concept_id': lab_concept.pk, 'domain_id': 'Measurement',
        'omop_table': 'measurement', 'status': 'approved',
    }, format='json')
    assert response.status_code == 403
    assert not SourceCodeConceptMapping.objects.filter(source_code='A01').exists()


@pytest.mark.django_db
def test_an_analyst_proposes_and_an_admin_approves(bob, lab_concept):
    org = Organization.objects.create(name='Org', slug='org')
    analyst = Identity.objects.create_user(email='analyst@example.com', password='x')
    GroupAccess.objects.create(identity=analyst, org=org, role='analyst')
    mapping = propose(analyst, lab_concept)
    assert mapping.proposed_by == analyst
    assert approve(bob, mapping).status_code == 200


@pytest.mark.django_db
def test_somebody_elses_note_fix_does_not_free_the_proposer(alice, bob, lab_concept):
    mapping = propose(alice, lab_concept)
    fixed = client_for(bob).patch(detail(mapping), {'notes': 'typo fixed'}, format='json')
    assert fixed.status_code == 200, fixed.data
    mapping.refresh_from_db()
    assert mapping.updated_by == bob and mapping.proposed_by == alice
    assert approve(alice, mapping).status_code == 403


@pytest.mark.django_db
def test_moving_the_destination_makes_the_editor_the_proposer(alice, bob, lab_concept, other_concept):
    mapping = propose(alice, lab_concept)

    # Bob edits and approves in one save: he is both sides of it.
    both = approve(bob, mapping, destination_concept_id=other_concept.pk)
    assert both.status_code == 403
    mapping.refresh_from_db()
    assert (mapping.status, mapping.target_concept_id, mapping.proposed_by) == (
        'proposed', lab_concept.pk, alice)

    # Bob proposes the new destination; now Alice, who did not, may approve it.
    moved = client_for(bob).patch(
        detail(mapping), {'destination_concept_id': other_concept.pk, 'status': 'proposed'},
        format='json')
    assert moved.status_code == 200, moved.data
    mapping.refresh_from_db()
    assert mapping.proposed_by == bob
    assert approve(bob, mapping).status_code == 403
    assert approve(alice, mapping).status_code == 200


@pytest.mark.django_db
def test_reopening_a_rejected_mapping_proposes_it(alice, bob, lab_concept):
    mapping = propose(alice, lab_concept)
    assert client_for(bob).patch(detail(mapping), {'status': 'rejected'}, format='json').status_code == 200
    reopened = client_for(bob).patch(detail(mapping), {'status': 'proposed'}, format='json')
    assert reopened.status_code == 200, reopened.data
    mapping.refresh_from_db()
    assert mapping.proposed_by == bob
    assert approve(bob, mapping).status_code == 403


@pytest.mark.django_db
def test_unapproving_does_not_make_the_unapprover_the_proposer(alice, bob, lab_concept):
    mapping = propose(alice, lab_concept)
    assert approve(bob, mapping).status_code == 200
    assert client_for(bob).patch(detail(mapping), {'status': 'proposed'}, format='json').status_code == 200
    mapping.refresh_from_db()
    assert mapping.proposed_by == alice
    assert approve(bob, mapping).status_code == 200


@pytest.mark.django_db
def test_a_machine_proposal_has_no_proposer_and_anyone_may_approve_it(alice, lab_concept):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='M01', target_concept=lab_concept,
        domain_id='Measurement', omop_table='measurement', origin_system='suggest v0.1',
        status='proposed',
    )
    assert mapping.proposed_by is None
    assert approve(alice, mapping).status_code == 200


@pytest.mark.django_db
def test_a_curator_who_replaces_a_machine_destination_proposes_it(alice, bob, lab_concept, other_concept):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='M01', target_concept=lab_concept,
        domain_id='Measurement', omop_table='measurement', origin_system='suggest v0.1',
        status='proposed',
    )
    assert client_for(alice).patch(
        detail(mapping), {'destination_concept_id': other_concept.pk}, format='json').status_code == 200
    mapping.refresh_from_db()
    assert mapping.proposed_by == alice
    assert approve(alice, mapping).status_code == 403
    assert approve(bob, mapping).status_code == 200


def test_the_default_is_on_without_debug_and_the_variable_overrides_it():
    from promop.settings import separate_approver_default as default
    assert default(False, None) is True and default(False, '') is True
    assert default(True, None) is False
    for off in ('false', '0', 'No', ' off '):
        assert default(False, off) is False
    for on in ('true', '1', 'yes'):
        assert default(True, on) is True


@pytest.mark.django_db
def test_the_switch_turns_the_rule_off(alice, lab_concept, settings):
    settings.MAPPING_SEPARATE_APPROVER = False
    mapping = propose(alice, lab_concept)
    assert approve(alice, mapping).status_code == 200
    row = client_for(alice).get(LIST, {'search': 'A01'}).data
    assert row and all(entry['proposed_by_id'] is None for entry in row)
    assert client_for(alice).get(LIST + 'reference/').data['separate_approver'] is False


@pytest.mark.django_db
def test_the_row_payload_names_the_proposer_for_the_page(alice, lab_concept):
    mapping = propose(alice, lab_concept)
    entries = client_for(alice).get(LIST, {'search': 'A01'}).data
    assert [entry['proposed_by_id'] for entry in entries if entry['mapping_id'] == mapping.pk] == [alice.pk]
    assert client_for(alice).get(LIST + 'reference/').data['separate_approver'] is True


@pytest.mark.django_db
def test_a_group_approve_skips_the_actors_own_proposals(alice, bob, lab_concept):
    mine = propose(alice, lab_concept, code='G01')
    theirs = propose(bob, lab_concept, code='G02')
    machine = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='G03', target_concept=lab_concept,
        domain_id='Measurement', omop_table='measurement', status='proposed',
    )
    run = SuggestRun.objects.create(source_vocabulary_id='VendorLab', total=3)
    ids = [mine.pk, theirs.pk, machine.pk]
    SourceCodeConceptMapping.objects.filter(pk__in=ids).update(locked_by=alice)
    done = _apply_group_action(
        ids, 'approve', lab_concept.pk, run=run, actor=alice,
        selected_targets={str(pk): lab_concept.pk for pk in ids})
    assert done == 2
    statuses = dict(SourceCodeConceptMapping.objects.filter(pk__in=ids).values_list('pk', 'status'))
    assert statuses == {mine.pk: 'proposed', theirs.pk: 'approved', machine.pk: 'approved'}


def make_variant(proposer=None):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='V01', domain_id='Measurement',
        omop_table='measurement', status='proposed')
    return SourceCodeAxisVariant.objects.create(
        mapping=mapping, axis_key='unit=mg/dL', axis_values={}, units=['mg/dL'],
        proposed_by=proposer)


@pytest.mark.django_db
def test_an_axis_variant_destination_follows_the_same_rule(alice, bob, lab_concept):
    variant = make_variant()
    axis_variants.set_destination(variant, lab_concept, approve=False, user=alice)
    variant.refresh_from_db()
    assert variant.proposed_by == alice

    with pytest.raises(mapping_approval.SelfApprovalError):
        axis_variants.set_destination(variant, lab_concept, approve=True, user=alice)
    variant.refresh_from_db()
    assert variant.status == 'proposed' and variant.reviewer is None

    axis_variants.set_destination(variant, lab_concept, approve=True, user=bob)
    variant.refresh_from_db()
    assert (variant.status, variant.reviewer, variant.proposed_by) == ('approved', bob, alice)


@pytest.mark.django_db
def test_a_suggested_axis_variant_has_no_proposer(alice, lab_concept):
    variant = make_variant()
    axis_variants.set_destination(variant, lab_concept, approve=False, user=alice, machine=True)
    variant.refresh_from_db()
    assert variant.proposed_by is None
    axis_variants.set_destination(variant, lab_concept, approve=True, user=alice)
    variant.refresh_from_db()
    assert (variant.status, variant.reviewer) == ('approved', alice)


@pytest.mark.django_db
def test_approving_a_variant_with_a_new_destination_in_one_step_is_refused(alice, bob, lab_concept, other_concept):
    variant = make_variant()
    axis_variants.set_destination(variant, lab_concept, approve=False, user=bob)
    with pytest.raises(mapping_approval.SelfApprovalError):
        axis_variants.set_destination(variant, other_concept, approve=True, user=alice)


def upload(user, body, vocabulary='VendorLab'):
    return client_for(user).post('/api/v1/code-mappings/upload/', {
        'file': SimpleUploadedFile('codes.csv', body.encode(), content_type='text/csv'),
        'source_vocabulary_id': vocabulary, 'provenance': 'file',
    }, format='multipart')


@pytest.mark.django_db
def test_a_file_cannot_create_an_approved_row_for_its_uploader(alice, lab_concept):
    response = upload(alice, f'source code,source description,destination concept ID,state\nU01,Alb,{lab_concept.pk},approved\n')
    assert response.status_code == 400
    assert response.data['errors'][0]['row'] == 2
    assert 'You proposed this mapping' in response.data['errors'][0]['detail']
    assert not SourceCodeConceptMapping.objects.filter(source_code='U01').exists()


@pytest.mark.django_db
def test_a_file_proposal_is_the_uploaders_and_another_admin_may_approve_it(alice, bob, lab_concept):
    body = f'source code,source description,destination concept ID\nU01,Alb,{lab_concept.pk}\n'
    assert upload(alice, body).status_code == 201
    row = SourceCodeConceptMapping.objects.get(source_code='U01')
    assert row.proposed_by == alice
    approving = f'source code,source description,destination concept ID,state\nU01,Alb,{lab_concept.pk},approved\n'
    assert upload(alice, approving).status_code == 400
    assert upload(bob, approving).status_code == 201
    row.refresh_from_db()
    assert (row.status, row.reviewer, row.proposed_by) == ('approved', bob, alice)


# -- review follow-ups ---------------------------------------------------------

@pytest.mark.django_db
def test_a_machine_proposal_names_nobody_so_the_person_who_ran_it_may_approve(alice, lab_concept):
    from patient_portal.api.views import _upsert_source_code_mapping
    row, _ = _upsert_source_code_mapping(lab_concept, {
        'source_vocabulary_id': 'VendorLab', 'source_code': 'R01', 'domain_id': 'Measurement',
        'omop_table': 'measurement', 'status': 'proposed',
    }, alice, machine_proposal=True)
    assert row.proposed_by is None
    assert approve(alice, row).status_code == 200


@pytest.mark.django_db
def test_reverse_suggest_auto_proposal_records_no_proposer(alice, lab_concept):
    from omop_core.services import reverse_suggest_jobs as jobs
    candidate = {'source_vocabulary_id': 'VendorLab', 'source_code': 'RS1',
                 'source_code_description': 'Albumin', 'verdict': 'supported', 'declined': False}
    from unittest import mock
    with mock.patch('omop_core.mapping.reverse_catalog.refresh_catalog_candidate', return_value=candidate):
        result = jobs._propose_new_row(candidate, lab_concept, alice)
    row = SourceCodeConceptMapping.objects.get(pk=result['mapping_id'])
    assert row.proposed_by is None
    assert approve(alice, row).status_code == 200


@pytest.mark.django_db
def test_a_machine_repoint_keeps_the_starters_own_proposal_but_not_anothers(alice, bob, lab_concept, other_concept):
    from patient_portal.api.views import _upsert_source_code_mapping
    mine = propose(alice, lab_concept, code='K01')
    theirs = propose(bob, lab_concept, code='K02')
    for row in (mine, theirs):
        _upsert_source_code_mapping(other_concept, {'status': 'proposed'}, alice, mapping=row,
                                    record_decision=False)
        row.refresh_from_db()
    assert mine.proposed_by == alice          # not laundered
    assert approve(alice, mine).status_code == 403
    assert theirs.proposed_by is None         # bob's pick is no longer the destination
    assert approve(alice, theirs).status_code == 200


@pytest.mark.django_db
def test_an_upload_row_without_a_destination_has_no_proposer(alice):
    assert upload(alice, 'source code,source description\nQ01,Alb\n').status_code == 201
    assert SourceCodeConceptMapping.objects.get(source_code='Q01').proposed_by is None


@pytest.mark.django_db
def test_a_group_suggestion_written_over_a_row_clears_a_stale_proposer(alice, lab_concept):
    from omop_core.services.group_mapping_jobs import _write_group_suggestion
    row = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='S01', domain_id='Measurement',
        omop_table='measurement', status='proposed', proposed_by=alice, locked_by=alice)
    run = SuggestRun.objects.create(source_vocabulary_id='VendorLab', total=1)
    proposal = {'concept': {'concept_id': lab_concept.pk}, 'strategy': 'lexical',
                'note': 'n', 'confidence': 0.9}
    _write_group_suggestion([row.pk], proposal, run=run, actor=alice)
    row.refresh_from_db()
    assert row.target_concept_id == lab_concept.pk and row.proposed_by is None


@pytest.mark.django_db
def test_group_approve_with_a_foreign_destination_skips_instead_of_failing(alice, bob, lab_concept, other_concept):
    row = propose(bob, lab_concept, code='G10')
    run = SuggestRun.objects.create(source_vocabulary_id='VendorLab', total=1)
    SourceCodeConceptMapping.objects.filter(pk=row.pk).update(locked_by=alice)
    done = _apply_group_action(
        [row.pk], 'approve', other_concept.pk, run=run, actor=alice,
        selected_targets={str(row.pk): lab_concept.pk})
    assert done == 0
    row.refresh_from_db()
    assert row.status == 'proposed'
    run.refresh_from_db()
    assert run.activity == [{'action': 'skipped', 'reason': 'self_approval', 'count': 1}]


@pytest.mark.django_db
def test_a_service_identity_is_not_a_person_and_machine_reads_hide_the_proposer(alice, lab_concept):
    service = Identity.objects.create_user(email='svc@example.com', password='x')
    service.issuer = 'urn:service'   # what a service token authenticates as
    assert not mapping_approval.is_person(service)
    assert mapping_approval.is_person(alice)
    from patient_portal.api.views import _proposer_id, _without_source_samples
    mapping = propose(alice, lab_concept, code='H01')
    assert _proposer_id(mapping) == alice.pk
    _without_source_samples([mapping])
    assert _proposer_id(mapping) is None


@pytest.mark.django_db
def test_clearing_a_variant_destination_proposes_nothing(alice, lab_concept):
    variant = make_variant()
    axis_variants.set_destination(variant, lab_concept, approve=False, user=alice)
    axis_variants.set_destination(variant, None, approve=False, user=alice)
    variant.refresh_from_db()
    assert variant.target_concept_id is None and variant.proposed_by is None


def apply_from_preview(user, concept, row, verdict, **extra):
    """Concept to Code's apply, naming a saved preview whose candidate has *verdict*."""
    run = SuggestRun.objects.create(
        direction='reverse', state=SuggestRun.SUCCESS, source_vocabulary_id=row.source_vocabulary_id,
        activity=[{'concept': {'concept_id': concept.pk}, 'candidates': [{
            'mapping_id': row.pk, 'source_code': row.source_code,
            'source_vocabulary_id': row.source_vocabulary_id, 'verdict': verdict,
            'declined': extra.pop('declined', False),
            'source_code_description': row.source_code_description}]}])
    row.refresh_from_db()
    return client_for(user).post(
        f'/api/v1/concept-to-code/{concept.pk}/mappings/{row.pk}/',
        {'expected_updated_at': row.updated_at.isoformat(), 'run_id': str(run.pk), **extra}, format='json')


@pytest.mark.django_db
@pytest.mark.parametrize(('verdict', 'declined', 'approver_allowed'), [
    ('supported', False, True),     # a ranker chose it: a machine proposal
    ('review', False, False),       # retrieved only (ranking_model 'none'): the curator's pick
    ('supported', True, False),     # the ranker declined it
])
def test_concept_to_code_apply_is_a_machine_proposal_only_when_a_ranker_chose_it(
        alice, lab_concept, verdict, declined, approver_allowed):
    target = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')
    row = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='P01', source_code_description='Albumin',
        domain_id='Measurement', omop_table='measurement', status='proposed',
        target_concept=lab_concept, origin_system='suggest v0.1')
    moved = apply_from_preview(alice, target, row, verdict, declined=declined, status='proposed')
    assert moved.status_code == 200, moved.data
    row.refresh_from_db()
    assert row.target_concept_id == target.pk
    assert (row.proposed_by is None) is approver_allowed
    again = apply_from_preview(alice, target, row, verdict, declined=declined, status='approved')
    assert again.status_code == (200 if approver_allowed else 403), again.data


@pytest.mark.django_db
def test_reverse_suggest_auto_proposal_of_an_unranked_candidate_is_the_curators(alice, lab_concept):
    from unittest import mock
    from omop_core.services import reverse_suggest_jobs as jobs
    candidate = {'source_vocabulary_id': 'VendorLab', 'source_code': 'RS2',
                 'source_code_description': 'Albumin', 'verdict': 'review', 'declined': False}
    with mock.patch('omop_core.mapping.reverse_catalog.refresh_catalog_candidate', return_value=candidate):
        result = jobs._propose_new_row(candidate, lab_concept, alice)
    row = SourceCodeConceptMapping.objects.get(pk=result['mapping_id'])
    assert row.proposed_by == alice
    assert approve(alice, row).status_code == 403


@pytest.mark.django_db
def test_re_approving_an_approved_variant_is_not_an_approval(alice, bob, lab_concept):
    variant = make_variant()
    axis_variants.set_destination(variant, lab_concept, approve=False, user=alice)
    axis_variants.set_destination(variant, lab_concept, approve=True, user=bob)
    variant.refresh_from_db()
    axis_variants.set_destination(variant, lab_concept, approve=True, user=alice)   # retry: no-op
    variant.refresh_from_db()
    assert variant.status == 'approved' and variant.proposed_by == alice


@pytest.mark.django_db
def test_an_upload_that_reopens_a_rejected_row_proposes_it(alice, bob, lab_concept):
    body = f'source code,source description,destination concept ID\nW01,Alb,{lab_concept.pk}\n'
    assert upload(bob, body).status_code == 201
    row = SourceCodeConceptMapping.objects.get(source_code='W01')
    SourceCodeConceptMapping.objects.filter(pk=row.pk).update(status='rejected')
    reopen = f'source code,source description,destination concept ID,state\nW01,Alb,{lab_concept.pk},proposed\n'
    assert upload(alice, reopen).status_code == 201
    row.refresh_from_db()
    assert (row.status, row.proposed_by) == ('proposed', alice)
