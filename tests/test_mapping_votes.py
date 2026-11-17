"""Public voting on the wearable mapping proposals (#2678).

Who may share, what a stranger with the link can read and send, that nothing
is a vote until the emailed link is confirmed, and what curators see after.
The public endpoints are an unauthenticated write surface that sends email,
so each limit and each "says nothing" rule has a test of its own.
"""
import csv
import datetime
import hashlib
import io
import json
import logging
import re
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from omop_core.models import (
    GroupAccess, MappingVote, MappingVoteEmail, MappingVoteShare, MappingVoteSubmission,
    Organization, SourceCodeConceptMapping,
)
from omop_core.services import mapping_votes
from patient_portal.models import AuditEvent, Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

SHARES = '/api/v1/code-mappings/vote-shares/'
PUBLIC = '/api/v1/public/mapping-votes/'
VOTER = 'voter@example.test'
LINK = re.compile(r'/vote/confirm/([A-Za-z0-9_-]+)')


@pytest.fixture(autouse=True)
def fresh_throttles():
    """The throttles count in the process cache, which outlives a test."""
    cache.clear()
    yield
    cache.clear()


def with_role(role, name=None):
    user = Identity.objects.create_user(email=f'{name or role}-votes@example.test')
    org, _ = Organization.objects.get_or_create(slug='votes-org', defaults={'name': 'Votes Org'})
    GroupAccess.objects.create(identity=user, org=org, role=role)
    return user


def client_for(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def analyst():
    return client_for(with_role('analyst'))


@pytest.fixture
def doctor():
    """Reads every mapping page, writes none."""
    return client_for(with_role('doctor'))


@pytest.fixture
def anonymous():
    return APIClient()


@pytest.fixture
def many_submissions():
    """Lift the per-address submit throttle, for a test that is about the
    limits counted in the database."""
    with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {
            'mapping_vote_submit': '1000/minute', 'mapping_vote_confirm': '1000/minute'}):
        yield


def snomed(code):
    """A destination outside LOINC: the list looks up a LOINC row's example
    units one row at a time on an empty test table, which is not the tally's."""
    return ConceptFactory(concept_code=code, concept_name=f'Concept for {code}', vocabulary=VocabularyFactory(
        vocabulary_id='SNOMED', vocabulary_name='SNOMED'))


def loinc(code, name):
    return ConceptFactory(concept_code=code, concept_name=name)


def proposal(code, concept=None, **kwargs):
    """A global wearable mapping that is up for the vote unless *kwargs* say otherwise."""
    return SourceCodeConceptMapping.objects.create(source_code=code, **{
        'source_vocabulary_id': 'Apple', 'status': 'proposed', 'domain_id': 'Measurement',
        'omop_table': 'measurement', 'category': 'Activity', 'metric_key': code.lower(),
        'target_concept': concept or loinc(f'L-{code}', f'Concept for {code}'), **kwargs})


def make_share(**kwargs):
    return mapping_votes.create_share(None, kwargs.pop('title', 'HL7 PHR working group review'), **kwargs)


def page_url(share):
    return f'{PUBLIC}{share.token}/'


def submit(client, share, votes, email=VOTER, **extra):
    return client.post(f'{PUBLIC}{share.token}/submit/', {'email': email, 'votes': votes},
                       format='json', **extra)


def emailed_token(index=-1):
    return LINK.search(mail.outbox[index].body).group(1)


def confirm_url(token):
    return f'{PUBLIC}confirm/{token}/'


def cast(client, share, votes, email=VOTER):
    """Submit and confirm: the whole of a voter's part."""
    before = len(mail.outbox)
    assert submit(client, share, votes, email=email).status_code == 202
    assert len(mail.outbox) == before + 1
    response = client.post(confirm_url(emailed_token()))
    assert response.status_code == 200, response.content
    return response.json()


def yes(mapping, comment=''):
    return {'mapping': mapping.pk, 'choice': 'yes', 'comment': comment}


def no(mapping, comment=''):
    return {'mapping': mapping.pk, 'choice': 'no', 'comment': comment}


# ── Sharing ──────────────────────────────────────────────────────────────

def test_a_mapping_writer_creates_a_share_and_gets_its_public_link(analyst, settings):
    settings.APP_BASE_URL = 'https://promop.example.test'

    response = analyst.post(SHARES, {'title': '  HL7 review  ', 'note': 'Please vote by Friday.'}, format='json')

    assert response.status_code == 201, response.content
    share = MappingVoteShare.objects.get()
    assert share.title == 'HL7 review' and share.note == 'Please vote by Friday.'
    assert len(share.token) >= 43
    data = response.json()
    assert data['url'] == f'https://promop.example.test/vote/{share.token}'
    assert data['active'] is True and data['voters'] == 0 and data['expires_at'] is None
    assert response['Cache-Control'] == 'no-store'


def test_two_shares_never_get_the_same_token(analyst):
    for _ in range(2):
        assert analyst.post(SHARES, {'title': 'Review'}, format='json').status_code == 201
    first, second = MappingVoteShare.objects.values_list('token', flat=True)
    assert first != second


@pytest.mark.parametrize('body', [
    {}, {'title': '   '}, {'title': 'x' * 201}, {'title': 'ok', 'note': 'n' * 2001},
    {'title': 'ok', 'expires_at': '2000-01-01'}, {'title': 'ok', 'expires_at': 'soon'},
    {'title': 'ok', 'expires_at': '2099-01-01T00:00:00'}, {'title': 7},
])
def test_a_share_needs_a_title_and_a_future_expiry(analyst, body):
    assert analyst.post(SHARES, body, format='json').status_code == 400
    assert not MappingVoteShare.objects.exists()


def test_an_expiry_date_runs_to_the_end_of_that_day(analyst):
    day = (timezone.now() + datetime.timedelta(days=3)).date()

    response = analyst.post(SHARES, {'title': 'Review', 'expires_at': day.isoformat()}, format='json')

    assert response.status_code == 201
    expires = MappingVoteShare.objects.get().expires_at
    assert expires.date() == day and (expires.hour, expires.minute) == (23, 59)


def test_a_read_only_role_cannot_create_or_revoke_and_gets_no_link(doctor, analyst):
    share = make_share()

    assert doctor.post(SHARES, {'title': 'Mine'}, format='json').status_code == 403
    assert doctor.post(f'{SHARES}{share.pk}/revoke/').status_code == 403
    assert doctor.get(f'{SHARES}{share.pk}/votes-export/').status_code == 403
    listed = doctor.get(SHARES)

    assert listed.status_code == 200
    assert [set(entry) for entry in listed.json()['results']] == [{
        'id', 'title', 'note', 'created_at', 'expires_at', 'revoked_at', 'active', 'voters', 'votes'}]
    assert share.token not in listed.content.decode()
    assert MappingVoteShare.objects.get().revoked_at is None
    assert 'url' in analyst.get(SHARES).json()['results'][0]


@pytest.mark.parametrize(('method', 'path'), [
    ('get', SHARES), ('post', SHARES), ('post', f'{SHARES}1/revoke/'),
    ('get', f'{SHARES}1/votes-export/'), ('get', '/api/v1/code-mappings/1/votes/'),
])
def test_the_curator_endpoints_need_a_session(anonymous, method, path):
    assert getattr(anonymous, method)(path).status_code in (401, 403)


def test_a_patient_reaches_none_of_the_curator_endpoints():
    patient = client_for(with_role('patient'))
    share = make_share()
    mapping = proposal('STEPS')

    assert patient.get(SHARES).status_code == 403
    assert patient.post(SHARES, {'title': 'x'}, format='json').status_code == 403
    assert patient.post(f'{SHARES}{share.pk}/revoke/').status_code == 403
    assert patient.get(f'{SHARES}{share.pk}/votes-export/').status_code == 403
    assert patient.get(f'/api/v1/code-mappings/{mapping.pk}/votes/').status_code == 403


def test_the_share_list_counts_confirmed_voters_not_votes(analyst, anonymous, many_submissions):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    cast(anonymous, share, [yes(steps), no(sleep)])
    cast(anonymous, share, [yes(steps)], email='second@example.test')
    submit(anonymous, share, [yes(steps)], email='pending@example.test')

    entry = analyst.get(SHARES).json()['results'][0]

    assert entry['voters'] == 2 and entry['votes'] == 3


def test_revoking_keeps_confirmed_votes_and_drops_pending_ones(analyst, anonymous):
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps)])
    submit(anonymous, share, [no(steps)], email='late@example.test')
    token = emailed_token()

    response = analyst.post(f'{SHARES}{share.pk}/revoke/')

    assert response.status_code == 200 and response.json()['active'] is False
    assert response.json()['voters'] == 1
    assert MappingVote.objects.count() == 1
    assert not MappingVoteSubmission.objects.exists()
    assert anonymous.post(confirm_url(token)).status_code == 404
    assert analyst.post(f'{SHARES}999999/revoke/').status_code == 404


# ── The public page ──────────────────────────────────────────────────────

def test_the_public_page_lists_the_proposals_by_category_in_export_order(anonymous):
    share = make_share(note='Vote by Friday.')
    ucum = ConceptFactory(concept_code='/min', concept_name='per minute',
                          vocabulary=VocabularyFactory(vocabulary_id='UCUM', vocabulary_name='UCUM'))
    proposal('SleepRecord', source_vocabulary_id='HealthConnect', category='Sleep', metric_key='sleep')
    proposal('HKSteps', loinc('55423-8', 'Number of steps'), metric_key='steps',
             measure_class='Aggregate over interval', source_code_description='Step count',
             destination_unit_concept=ucum)
    proposal('Steps', source_vocabulary_id='HealthConnect', metric_key='steps')
    proposal('active_kcal', source_vocabulary_id='OpenWearables', metric_key='active_energy')

    response = anonymous.get(page_url(share))

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'title', 'note', 'expires_at', 'comment_max_length', 'groups'}
    assert data['title'] == 'HL7 PHR working group review' and data['note'] == 'Vote by Friday.'
    assert data['comment_max_length'] == 1000
    assert [(group['category'], [entry['source_code'] for entry in group['proposals']])
            for group in data['groups']] == [
        ('Activity', ['active_kcal', 'HKSteps', 'Steps']), ('Sleep', ['SleepRecord'])]
    steps = data['groups'][0]['proposals'][1]
    assert steps == {
        'id': SourceCodeConceptMapping.objects.get(source_code='HKSteps').pk,
        'metric_key': 'steps', 'measure_class': 'Aggregate over interval', 'source': 'HealthKit',
        'source_code': 'HKSteps', 'description': 'Step count',
        'destination_vocabulary': 'LOINC', 'destination_code': '55423-8',
        'destination_name': 'Number of steps', 'unit': '/min',
    }


def test_the_public_payload_is_reference_data_only(anonymous, analyst):
    """The exact keys, so a field added to the row never reaches a stranger
    by accident; and nothing a curator or another voter wrote."""
    share = make_share()
    org = Organization.objects.create(slug='secret-hospital', name='Secret Hospital')
    reviewer = Identity.objects.create_user(email='reviewer-secret@example.test')
    steps = proposal(
        'STEPS', notes='CURATOR-NOTE-TEXT', origin_system='suggest-secret', occurrence_count=4242,
        reviewer=reviewer, source_metadata={'fhir': {'id': 'PATIENT-METADATA'}},
        source_unit_evidence=[{'unit': 'EVIDENCE-UNIT', 'source': 'patient-data'}])
    proposal('SCOPED', organization=org)
    cast(anonymous, share, [yes(steps, 'OTHER-VOTER-COMMENT')], email='other-voter@example.test')

    response = anonymous.get(page_url(share))

    [group] = response.json()['groups']
    [entry] = group['proposals']
    assert set(entry) == {
        'id', 'metric_key', 'measure_class', 'source', 'source_code', 'description',
        'destination_vocabulary', 'destination_code', 'destination_name', 'unit'}
    text = response.content.decode()
    for secret in ('CURATOR-NOTE-TEXT', 'suggest-secret', '4242', 'reviewer-secret', 'PATIENT-METADATA',
                   'EVIDENCE-UNIT', 'Secret Hospital', 'secret-hospital', 'SCOPED',
                   'OTHER-VOTER-COMMENT', 'other-voter@example.test', 'yes'):
        assert secret not in text


def test_only_global_wearable_proposals_with_a_loaded_destination_are_votable(anonymous):
    share = make_share()
    org = Organization.objects.create(slug='hospital', name='Hospital')
    open_one = proposal('OPEN')
    proposal('APPROVED', status='approved')
    proposal('REJECTED', status='rejected')
    proposal('SCOPED', organization=org)
    proposal('NOT-WEARABLE', source_vocabulary_id='ICD10CM')
    SourceCodeConceptMapping.objects.create(
        source_code='NO-DESTINATION', source_vocabulary_id='Apple', status='proposed')
    SourceCodeConceptMapping.objects.create(
        source_code='UNLOADED', source_vocabulary_id='Apple', status='proposed', target_concept_id=77_777_777)

    listed = [entry['id'] for group in anonymous.get(page_url(share)).json()['groups']
              for entry in group['proposals']]

    assert listed == [open_one.pk]


def test_the_page_follows_the_proposals_as_they_change(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    def codes():
        return [entry['source_code'] for group in anonymous.get(page_url(share)).json()['groups']
                for entry in group['proposals']]

    assert codes() == ['STEPS']
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(status='approved')
    proposal('SLEEP')
    assert codes() == ['SLEEP']


def test_unknown_revoked_and_expired_links_are_one_answer(anonymous, analyst):
    proposal('STEPS')
    revoked = make_share()
    mapping_votes.revoke_share(revoked)
    expired = make_share()
    MappingVoteShare.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
    tokens = ['never-issued-token', revoked.token, expired.token, 'x' * 500]

    pages = [anonymous.get(f'{PUBLIC}{token}/') for token in tokens]
    submissions = [anonymous.post(f'{PUBLIC}{token}/submit/', {'email': VOTER, 'votes': []}, format='json')
                   for token in tokens]

    for response in pages + submissions:
        assert response.status_code == 404
        assert response.json() == {'detail': 'This link is not available.'}
        assert response['Cache-Control'] == 'no-store'
    assert len({response.content for response in pages + submissions}) == 1
    assert not mail.outbox


def test_the_public_endpoints_set_no_cookie_and_are_not_cached(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    page = anonymous.get(page_url(share))
    submitted = submit(anonymous, share, [yes(steps)])
    summary = anonymous.get(confirm_url(emailed_token()))
    confirmed = anonymous.post(confirm_url(emailed_token()))

    for response in (page, submitted, summary, confirmed):
        assert response.status_code in (200, 202)
        assert not response.cookies
        assert 'Set-Cookie' not in response
        assert response['Cache-Control'] == 'no-store'
        assert response['Referrer-Policy'] == 'no-referrer'
    assert not anonymous.session.keys()


def test_a_signed_in_visitor_with_a_stale_session_is_still_just_a_visitor():
    """The pages authenticate nobody: a session cookie causes no CSRF refusal
    and earns nothing."""
    user = with_role('doctor', 'visitor')
    user.set_password('a-long-password-for-the-test')
    user.save()
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    share = make_share()
    steps = proposal('STEPS')

    assert client.get(page_url(share)).status_code == 200
    assert submit(client, share, [yes(steps)]).status_code == 202
    assert client.post(confirm_url(emailed_token())).status_code == 200


def test_a_flagged_session_does_not_lock_the_public_pages():
    user = with_role('doctor', 'flagged')
    user.must_change_password = True
    user.save()
    client = APIClient()
    client.force_login(user)
    share = make_share()
    proposal('STEPS')

    assert client.get(page_url(share)).status_code == 200
    assert client.get(SHARES).status_code == 403


def test_the_endpoints_are_on_v1_only():
    """The legacy /api/ router has none of them: there the path falls through
    to the page shell, as any unknown path does."""
    from django.urls import resolve

    for path in ('public/mapping-votes/some-token/', 'public/mapping-votes/some-token/submit/',
                 'public/mapping-votes/confirm/some-token/', 'code-mappings/vote-shares/',
                 'code-mappings/vote-shares/1/revoke/', 'code-mappings/vote-shares/1/votes-export/',
                 'code-mappings/1/votes/'):
        assert resolve(f'/api/v1/{path}').url_name.startswith('v1-'), path
        assert resolve(f'/api/{path}').url_name == 'home', path


def test_the_public_page_costs_the_same_queries_at_any_size(anonymous):
    share = make_share()
    proposal('FIRST')
    with CaptureQueriesContext(connection) as small:
        assert anonymous.get(page_url(share)).status_code == 200
    for index in range(25):
        proposal(f'NEXT-{index}', category='Sleep' if index % 2 else 'Activity')
    AuditEvent.objects.all().delete()
    cache.clear()
    with CaptureQueriesContext(connection) as large:
        assert anonymous.get(page_url(share)).status_code == 200

    assert len(large) == len(small)


def test_tokens_stay_out_of_the_audit_trail(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    anonymous.get(page_url(share))
    submit(anonymous, share, [yes(steps)])
    token = emailed_token()
    anonymous.get(confirm_url(token))
    anonymous.post(confirm_url(token))

    paths = list(AuditEvent.objects.values_list('path', flat=True))

    assert paths, 'the requests are still audited'
    assert not any(share.token in path or token in path for path in paths)
    assert f'{PUBLIC}:token/' in paths and f'{PUBLIC}confirm/:confirm_token/' in paths
    assert not AuditEvent.objects.filter(user_email__isnull=False).exists()


def test_tokens_stay_out_of_error_reports():
    from promop.sentry import redact_path
    token = 'kJ8_x-' + 'A1b2' * 10

    assert redact_path(f'{PUBLIC}{token}/submit/') == f'{PUBLIC}:id/submit/'
    assert redact_path(f'{PUBLIC}confirm/{token}/') == f'{PUBLIC}confirm/:id/'
    assert redact_path('/api/v1/code-mappings/wearables-export/') == '/api/v1/code-mappings/wearables-export/'


# ── Submitting ───────────────────────────────────────────────────────────

def test_a_submission_is_not_a_vote(anonymous, analyst):
    share = make_share()
    steps = proposal('STEPS')

    response = submit(anonymous, share, [yes(steps, 'Looks right')])

    assert response.status_code == 202
    assert response.json() == {
        'detail': 'Check your email to confirm your votes. They are not recorded until you confirm.'}
    assert not MappingVote.objects.exists()
    pending = MappingVoteSubmission.objects.get()
    assert pending.email == VOTER and pending.share == share
    assert pending.payload == [{
        'mapping': steps.pk, 'choice': 'yes', 'comment': 'Looks right', 'concept': steps.target_concept_id}]
    assert pending.expires_at - pending.created_at == datetime.timedelta(hours=48)
    # Nothing a curator reads has moved.
    assert analyst.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json() == {
        'tally': {'yes': 0, 'no': 0, 'comments': 0}, 'emails_included': True, 'votes': []}
    assert analyst.get(SHARES).json()['results'][0]['voters'] == 0
    [row] = analyst.get('/api/v1/code-mappings/', {'browse': '1', 'source': 'OpenWearables'}).json()['results']
    assert row['votes'] == {'yes': 0, 'no': 0, 'comments': 0}
    assert 'Looks right' not in analyst.get(f'{SHARES}{share.pk}/votes-export/').content.decode('utf-8-sig')


def test_the_confirmation_email_carries_the_link_and_nothing_the_voter_typed(anonymous, settings):
    settings.APP_BASE_URL = 'https://promop.example.test'
    share = make_share(title='HL7 review')
    steps, sleep, energy = proposal('STEPS'), proposal('SLEEP'), proposal('ENERGY')

    submit(anonymous, share, [
        yes(steps, 'VOTER-TEXT-ONE'), no(sleep), {'mapping': energy.pk, 'comment': 'VOTER-TEXT-TWO'},
    ], email='  Voter@Example.TEST ')

    [message] = mail.outbox
    assert message.to == [VOTER]
    assert message.subject == 'Confirm your votes on the wearable mapping proposals'
    token = emailed_token()
    assert f'https://promop.example.test/vote/confirm/{token}' in message.body
    assert 'HL7 review' in message.body
    assert '2 votes and 2 comments' in message.body
    assert 'expires in 48 hours' in message.body
    assert 'VOTER-TEXT' not in message.body and 'VOTER-TEXT' not in message.subject
    assert not message.alternatives if hasattr(message, 'alternatives') else True
    assert len(token) >= 43


def test_only_the_hash_of_the_confirmation_token_is_stored(anonymous):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    token = emailed_token()

    pending = MappingVoteSubmission.objects.get()

    assert pending.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(list(MappingVoteSubmission.objects.values(
        'email', 'payload', 'token_hash')), default=str)
    assert MappingVoteEmail.objects.get().email_key != VOTER
    assert '@' not in MappingVoteEmail.objects.get().email_key


def test_a_new_and_a_returning_address_get_the_same_answer(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps)], email='returning@example.test')

    first_time = submit(anonymous, share, [yes(steps)], email='new@example.test')
    returning = submit(anonymous, share, [no(steps)], email='returning@example.test')

    assert first_time.status_code == returning.status_code == 202
    assert first_time.content == returning.content
    assert dict(first_time.items()) == dict(returning.items())
    assert len(mail.outbox) == 3


@pytest.mark.parametrize('email', [
    '', '   ', 'not-an-email', 'a@b', 'two@example.test, three@example.test', None, 5, ['a@example.test'],
    'x' * 250 + '@example.test', 'new\nline@example.test',
])
def test_an_invalid_email_is_refused(anonymous, email):
    share = make_share()
    steps = proposal('STEPS')

    response = submit(anonymous, share, [yes(steps)], email=email)

    assert response.status_code == 400
    assert response.json() == {'detail': 'Enter a valid email address.'}
    assert not MappingVoteSubmission.objects.exists() and not mail.outbox


def test_a_submission_needs_a_choice_or_a_comment_somewhere(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    for votes in ([], None, 'yes', [{'mapping': steps.pk}], [{'mapping': steps.pk, 'choice': '', 'comment': '  \n '}]):
        response = submit(anonymous, share, votes)
        assert response.status_code == 400, votes
        assert 'at least one proposal' in response.json()['detail']
    assert not MappingVoteSubmission.objects.exists() and not mail.outbox


def test_entries_that_say_nothing_are_dropped_from_a_submission(anonymous):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')

    assert submit(anonymous, share, [yes(steps), {'mapping': sleep.pk, 'choice': '', 'comment': ''}]).status_code == 202

    assert [vote['mapping'] for vote in MappingVoteSubmission.objects.get().payload] == [steps.pk]


@pytest.mark.parametrize('choice', ['maybe', 'YES', 'approve', 1, True, ['yes']])
def test_a_choice_is_yes_no_or_blank(anonymous, choice):
    share = make_share()
    steps = proposal('STEPS')

    response = submit(anonymous, share, [{'mapping': steps.pk, 'choice': choice}])

    assert response.status_code == 400
    assert not MappingVoteSubmission.objects.exists()


def test_an_id_that_is_not_on_the_page_is_refused_without_saying_which(anonymous):
    """A mapping that exists but is not votable and one that never existed
    read the same, so the endpoint cannot be used to probe the mapping table."""
    share = make_share()
    steps = proposal('STEPS')
    approved = proposal('APPROVED', status='approved')
    hospital = proposal('SCOPED', organization=Organization.objects.create(slug='h', name='H'))
    lab = proposal('LAB', source_vocabulary_id='ICD10CM')

    answers = [submit(anonymous, share, [yes(steps), {'mapping': other, 'choice': 'no'}])
               for other in (approved.pk, hospital.pk, lab.pk, 987_654_321)]

    for response in answers:
        assert response.status_code == 400
    assert len({response.content for response in answers}) == 1
    for value in (approved.pk, hospital.pk, lab.pk, 987_654_321):
        assert str(value) not in answers[0].content.decode()
    assert not MappingVoteSubmission.objects.exists() and not mail.outbox


@pytest.mark.parametrize('entry', [
    'not-an-object', 7, None, {'choice': 'yes'}, {'mapping': '12', 'choice': 'yes'},
    {'mapping': True, 'choice': 'yes'}, {'mapping': 1.5, 'choice': 'yes'},
    {'mapping': -1, 'choice': 'yes'}, {'mapping': 2 ** 70, 'choice': 'yes'},
])
def test_a_malformed_vote_is_refused(anonymous, entry):
    share = make_share()
    steps = proposal('STEPS')
    proposal('SLEEP')

    assert submit(anonymous, share, [yes(steps), entry]).status_code == 400
    assert not MappingVoteSubmission.objects.exists()


def test_a_proposal_is_voted_on_once_per_submission(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    proposal('SLEEP')

    assert submit(anonymous, share, [yes(steps), no(steps)]).status_code == 400


def test_more_votes_than_proposals_is_refused(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    response = submit(anonymous, share, [yes(steps), {'mapping': 5, 'choice': 'no'}, {'mapping': 6, 'choice': 'no'}])

    assert response.status_code == 400
    assert 'more votes' in response.json()['detail']


def test_a_comment_is_stripped_cleaned_and_capped(anonymous):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')

    too_long = submit(anonymous, share, [yes(steps, 'x' * 1001)])
    not_text = submit(anonymous, share, [{'mapping': steps.pk, 'comment': {'html': '<b>'}}])
    accepted = submit(anonymous, share, [
        yes(steps, '  line one\r\nline\x00 two\x07\x1b[31m \u202egnp.exe  '),
        {'mapping': sleep.pk, 'comment': 'y' * 1000},
    ])

    assert too_long.status_code == 400 and not_text.status_code == 400
    assert accepted.status_code == 202
    first, second = MappingVoteSubmission.objects.get().payload
    assert first['comment'] == 'line one\nline two[31m gnp.exe'
    assert len(second['comment']) == 1000


def test_an_oversized_body_is_refused_before_it_is_read(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    body = json.dumps({'email': VOTER, 'votes': [yes(steps)], 'padding': 'p' * (201 * 1024)})

    response = anonymous.post(f'{PUBLIC}{share.token}/submit/', body, content_type='application/json')

    assert response.status_code == 413
    assert not MappingVoteSubmission.objects.exists() and not mail.outbox


def test_a_form_post_is_not_a_submission(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    response = anonymous.post(f'{PUBLIC}{share.token}/submit/', {'email': VOTER, 'votes': str(steps.pk)})

    assert response.status_code == 400
    assert not MappingVoteSubmission.objects.exists()


def test_a_failed_send_answers_the_same_and_leaves_nothing_to_confirm(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    with mock.patch.object(mapping_votes, 'send_mail', side_effect=OSError('smtp down')):
        response = submit(anonymous, share, [yes(steps)])

    assert response.status_code == 202
    assert response.json() == {'detail': mapping_votes.SUBMITTED_DETAIL}
    assert not MappingVoteSubmission.objects.exists() and not MappingVote.objects.exists()


def test_no_address_or_comment_is_logged(anonymous, caplog, settings):
    settings.DEBUG = False
    share = make_share()
    steps = proposal('STEPS')
    caplog.set_level(logging.DEBUG)

    with mock.patch.object(mapping_votes, 'send_mail', side_effect=OSError('smtp down')):
        submit(anonymous, share, [yes(steps, 'PRIVATE-COMMENT')], email='private-address@example.test')
    cast(anonymous, share, [yes(steps, 'PRIVATE-COMMENT')], email='private-address@example.test')

    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'private-address' not in logged and 'PRIVATE-COMMENT' not in logged
    assert emailed_token() not in logged and share.token not in logged


# ── Confirming ───────────────────────────────────────────────────────────

def test_opening_the_emailed_link_shows_the_votes_and_confirms_nothing(anonymous):
    share = make_share(title='HL7 review')
    steps = proposal('STEPS', loinc('55423-8', 'Number of steps'))
    sleep = proposal('SLEEP', category='Sleep')
    submit(anonymous, share, [no(sleep), yes(steps, 'Agree')])
    url = confirm_url(emailed_token())

    for _ in range(3):
        response = anonymous.get(url)

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'title', 'email', 'expires_at', 'votes'}
    assert data['title'] == 'HL7 review' and data['email'] == VOTER
    assert [(entry['proposal']['source_code'], entry['choice'], entry['comment']) for entry in data['votes']] == [
        ('STEPS', 'yes', 'Agree'), ('SLEEP', 'no', '')]
    assert data['votes'][0]['proposal']['destination_code'] == '55423-8'
    assert not MappingVote.objects.exists()
    assert MappingVoteSubmission.objects.count() == 1
    # Other verbs a scanner might try confirm nothing either.
    assert anonymous.head(url).status_code in (200, 405)
    assert anonymous.put(url).status_code == 405
    assert not MappingVote.objects.exists()


def test_confirming_records_the_votes_and_spends_the_token(anonymous, analyst):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    submit(anonymous, share, [yes(steps, 'Agree'), {'mapping': sleep.pk, 'comment': 'Check the unit'}])
    submitted_at = MappingVoteSubmission.objects.get().created_at
    url = confirm_url(emailed_token())

    response = anonymous.post(url)

    assert response.status_code == 200
    assert response.json() == {'recorded': 2, 'skipped': 0}
    assert not MappingVoteSubmission.objects.exists()
    votes = {vote.mapping_id: vote for vote in MappingVote.objects.all()}
    assert (votes[steps.pk].choice, votes[steps.pk].comment) == ('yes', 'Agree')
    assert (votes[sleep.pk].choice, votes[sleep.pk].comment) == ('', 'Check the unit')
    assert votes[steps.pk].voter_email == VOTER and votes[steps.pk].share == share
    assert votes[steps.pk].concept_id == steps.target_concept_id
    assert votes[steps.pk].created_at == submitted_at
    assert votes[steps.pk].confirmed_at >= submitted_at
    # Once.
    again, summary = anonymous.post(url), anonymous.get(url)
    assert again.status_code == summary.status_code == 404
    assert again.json() == summary.json() == {'detail': 'This confirmation link is no longer valid.'}
    assert MappingVote.objects.count() == 2


def test_used_expired_and_unknown_confirmation_links_are_one_answer(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    submit(anonymous, share, [yes(steps)])
    used = emailed_token()
    anonymous.post(confirm_url(used))
    submit(anonymous, share, [no(steps)], email='slow@example.test')
    expired = emailed_token()
    MappingVoteSubmission.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))

    answers = [getattr(anonymous, method)(confirm_url(token))
               for token in (used, expired, 'never-issued', 'x' * 400) for method in ('get', 'post')]

    assert {response.status_code for response in answers} == {404}
    assert len({response.content for response in answers}) == 1
    assert MappingVote.objects.get().choice == 'yes'


def test_a_link_cannot_be_confirmed_after_its_share_expires(anonymous):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    MappingVoteShare.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))

    assert anonymous.get(confirm_url(emailed_token())).status_code == 404
    assert anonymous.post(confirm_url(emailed_token())).status_code == 404
    assert not MappingVote.objects.exists()


def test_a_later_confirmed_submission_replaces_only_the_votes_it_names(anonymous, analyst, many_submissions):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    cast(anonymous, share, [yes(steps, 'First thought'), yes(sleep)])
    cast(anonymous, share, [no(steps)], email='someone-else@example.test')

    cast(anonymous, share, [no(steps, 'Changed my mind')])

    mine = {vote.mapping_id: vote for vote in MappingVote.objects.filter(voter_email=VOTER)}
    assert len(mine) == 2 and MappingVote.objects.count() == 3
    assert (mine[steps.pk].choice, mine[steps.pk].comment) == ('no', 'Changed my mind')
    assert (mine[sleep.pk].choice, mine[sleep.pk].comment) == ('yes', '')
    assert analyst.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()['tally'] == {
        'yes': 0, 'no': 2, 'comments': 1}


def test_the_same_address_votes_separately_on_two_shares(anonymous):
    first, second = make_share(title='One'), make_share(title='Two')
    steps = proposal('STEPS')

    cast(anonymous, first, [yes(steps)])
    cast(anonymous, second, [no(steps)])

    assert sorted(MappingVote.objects.values_list('share__title', 'choice')) == [('One', 'yes'), ('Two', 'no')]


def test_addresses_differing_only_in_case_are_one_voter(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    cast(anonymous, share, [yes(steps)], email='Voter@Example.test')
    cast(anonymous, share, [no(steps)], email='VOTER@example.TEST')

    assert list(MappingVote.objects.values_list('voter_email', 'choice')) == [(VOTER, 'no')]


def test_proposals_that_closed_before_confirmation_are_skipped_and_reported(anonymous, many_submissions):
    share = make_share()
    kept, approved, rejected, deleted, moved, cleared = (
        proposal(code) for code in ('KEPT', 'APPROVED', 'REJECTED', 'DELETED', 'MOVED', 'CLEARED'))
    submit(anonymous, share, [yes(mapping) for mapping in (kept, approved, rejected, deleted, moved, cleared)])
    SourceCodeConceptMapping.objects.filter(pk=approved.pk).update(status='approved')
    SourceCodeConceptMapping.objects.filter(pk=rejected.pk).update(status='rejected')
    deleted.delete()
    SourceCodeConceptMapping.objects.filter(pk=moved.pk).update(target_concept=loinc('NEW-1', 'Another concept'))
    SourceCodeConceptMapping.objects.filter(pk=cleared.pk).update(target_concept=None)
    url = confirm_url(emailed_token())

    summary = anonymous.get(url).json()
    response = anonymous.post(url)

    assert [entry['proposal']['source_code'] if entry['proposal'] else None for entry in summary['votes']] == [
        'KEPT', None, None, None, None, None]
    assert response.json() == {'recorded': 1, 'skipped': 5}
    assert list(MappingVote.objects.values_list('mapping_id', flat=True)) == [kept.pk]
    assert not MappingVoteSubmission.objects.exists()


def test_a_submission_whose_proposals_all_closed_still_spends_the_token(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    submit(anonymous, share, [yes(steps)])
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(status='approved')
    url = confirm_url(emailed_token())

    assert anonymous.post(url).json() == {'recorded': 0, 'skipped': 1}
    assert anonymous.post(url).status_code == 404
    assert not MappingVote.objects.exists()


# ── Limits ───────────────────────────────────────────────────────────────

def throttled(scope, rate='2/minute'):
    return mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {scope: rate})


def test_the_default_rates_are_conservative_and_configurable(settings):
    rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']

    assert rates['mapping_vote_read'] == '60/minute'
    assert rates['mapping_vote_submit'] == '5/hour'
    assert rates['mapping_vote_confirm'] == '20/hour'
    assert (settings.MAPPING_VOTE_MAX_PENDING_PER_EMAIL, settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY) == (3, 5)
    assert settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL == 500


def test_reading_the_page_is_throttled_by_client_address(anonymous):
    share = make_share()
    proposal('STEPS')

    with throttled('mapping_vote_read'):
        statuses = [anonymous.get(page_url(share)).status_code for _ in range(3)]
        elsewhere = anonymous.get(page_url(share), REMOTE_ADDR='203.0.113.9')
        unknown = anonymous.get(f'{PUBLIC}never-issued/')

    assert statuses == [200, 200, 429]
    assert elsewhere.status_code == 200
    assert unknown.status_code == 429, 'guessing tokens is throttled too'


def test_submitting_is_throttled_apart_from_reading(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    with throttled('mapping_vote_submit'):
        statuses = [submit(anonymous, share, [yes(steps)], email=f'voter-{index}@example.test').status_code
                    for index in range(3)]
        refused = submit(anonymous, share, [], REMOTE_ADDR='203.0.113.9')
        page = anonymous.get(page_url(share))

    assert statuses == [202, 202, 429]
    assert len(mail.outbox) == 2 and MappingVoteSubmission.objects.count() == 2
    assert refused.status_code == 400, 'another address has its own bucket, and a refused submission spends it'
    assert page.status_code == 200


def test_confirming_is_throttled_apart_from_the_rest(anonymous):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    url = confirm_url(emailed_token())

    with throttled('mapping_vote_confirm'):
        statuses = [anonymous.get(url).status_code, anonymous.get(confirm_url('guess')).status_code,
                    anonymous.post(url).status_code]
        page = anonymous.get(page_url(share))

    assert statuses == [200, 404, 429]
    assert not MappingVote.objects.exists()
    assert page.status_code == 200


def test_the_public_throttles_ignore_any_session(anonymous):
    """Keyed on the address even for a signed-in browser: the views read no
    session, so a visitor cannot buy a fresh bucket by signing in."""
    share = make_share()
    proposal('STEPS')
    signed_in = APIClient()
    signed_in.force_login(with_role('analyst', 'signed-in'))

    with throttled('mapping_vote_read'):
        statuses = [client.get(page_url(share)).status_code for client in (anonymous, signed_in, signed_in)]

    assert statuses == [200, 200, 429]


def test_at_most_three_pending_submissions_per_share_and_address(anonymous, many_submissions):
    share, other_share = make_share(), make_share(title='Other')
    steps = proposal('STEPS')

    answers = [submit(anonymous, share, [yes(steps)]) for _ in range(4)]

    assert [response.status_code for response in answers] == [202] * 4
    assert len({response.content for response in answers}) == 1
    assert MappingVoteSubmission.objects.count() == 3 and len(mail.outbox) == 3
    # The limit is per share and per address.
    assert submit(anonymous, other_share, [yes(steps)]).status_code == 202
    assert submit(anonymous, share, [yes(steps)], email='another@example.test').status_code == 202
    assert len(mail.outbox) == 5
    # Confirming one frees a place.
    anonymous.post(confirm_url(LINK.search(mail.outbox[0].body).group(1)))
    assert submit(anonymous, share, [no(steps)]).status_code == 202
    assert len(mail.outbox) == 6 and MappingVoteSubmission.objects.filter(share=share, email=VOTER).count() == 3


def test_at_most_five_confirmation_emails_a_day_per_address_across_shares(anonymous, many_submissions):
    shares = [make_share(title=f'Share {index}') for index in range(3)]
    steps = proposal('STEPS')

    answers = [submit(anonymous, share, [yes(steps)]) for share in shares for _ in range(2)]
    answers.append(submit(anonymous, make_share(title='One more'), [yes(steps)], email=VOTER.upper()))

    assert [response.status_code for response in answers] == [202] * 7
    assert len({response.content for response in answers}) == 1
    assert len(mail.outbox) == 5 and MappingVoteSubmission.objects.count() == 5
    assert submit(anonymous, shares[0], [yes(steps)], email='someone-else@example.test').status_code == 202
    assert len(mail.outbox) == 6


def test_confirming_does_not_buy_more_emails(anonymous, many_submissions):
    """The daily limit counts emails sent, not submissions still waiting."""
    share = make_share()
    steps = proposal('STEPS')
    for _ in range(5):
        cast(anonymous, share, [yes(steps)])

    response = submit(anonymous, share, [no(steps)])

    assert response.status_code == 202 and response.json() == {'detail': mapping_votes.SUBMITTED_DETAIL}
    assert len(mail.outbox) == 5 and not MappingVoteSubmission.objects.exists()
    assert MappingVote.objects.get().choice == 'yes'


def test_the_daily_limit_is_a_rolling_day(anonymous, many_submissions):
    share = make_share()
    steps = proposal('STEPS')
    for _ in range(5):
        cast(anonymous, share, [yes(steps)])
    MappingVoteEmail.objects.update(sent_at=timezone.now() - datetime.timedelta(hours=25))

    cast(anonymous, share, [no(steps)])

    assert len(mail.outbox) == 6
    assert MappingVoteEmail.objects.count() == 1, 'records past the window are purged'


def test_the_deployment_sends_a_bounded_number_of_emails_a_day(anonymous, many_submissions, settings):
    """Fresh addresses from fresh client addresses pass every other limit."""
    settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL = 4
    share = make_share()
    steps = proposal('STEPS')

    answers = [submit(anonymous, share, [yes(steps)], email=f'fresh-{index}@example.test',
                      REMOTE_ADDR=f'203.0.113.{index}') for index in range(6)]

    assert [response.status_code for response in answers] == [202] * 6
    assert len({response.content for response in answers}) == 1
    assert len(mail.outbox) == 4 and MappingVoteSubmission.objects.count() == 4


def test_the_limits_hold_without_the_cache(anonymous, many_submissions):
    """They are counted in the database, so clearing the throttle cache (or
    running on a worker with its own) changes nothing."""
    share = make_share()
    steps = proposal('STEPS')
    for _ in range(4):
        submit(anonymous, share, [yes(steps)])
        cache.clear()

    assert len(mail.outbox) == 3


def test_expired_submissions_are_purged_on_the_next_submit_and_by_the_command(anonymous, many_submissions, capsys):
    share = make_share()
    steps = proposal('STEPS')
    for _ in range(3):
        submit(anonymous, share, [yes(steps)])
    MappingVoteSubmission.objects.update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
    MappingVoteEmail.objects.update(sent_at=timezone.now() - datetime.timedelta(hours=49))

    assert submit(anonymous, share, [no(steps)]).status_code == 202
    assert [entry.payload[0]['choice'] for entry in MappingVoteSubmission.objects.all()] == ['no']

    MappingVoteSubmission.objects.update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
    cast_before = MappingVote.objects.count()
    call_command('purge_expired_vote_submissions')

    assert 'Deleted 1 expired' in capsys.readouterr().out
    assert not MappingVoteSubmission.objects.exists()
    assert MappingVote.objects.count() == cast_before


# ── What curators see ────────────────────────────────────────────────────

def test_a_writer_sees_who_voted_and_a_reader_sees_only_what_was_said(analyst, doctor, anonymous):
    share = make_share(title='HL7 review')
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps, 'Agree')])
    cast(anonymous, share, [no(steps)], email='second@example.test')
    url = f'/api/v1/code-mappings/{steps.pk}/votes/'

    for_writer, for_reader = analyst.get(url), doctor.get(url)

    assert for_writer.status_code == for_reader.status_code == 200
    writer, reader = for_writer.json(), for_reader.json()
    assert writer['tally'] == reader['tally'] == {'yes': 1, 'no': 1, 'comments': 1}
    assert writer['emails_included'] is True and reader['emails_included'] is False
    assert [(vote['voter_email'], vote['choice'], vote['comment'], vote['share_title'], vote['stale'])
            for vote in writer['votes']] == [
        ('second@example.test', 'no', '', 'HL7 review', False), (VOTER, 'yes', 'Agree', 'HL7 review', False)]
    assert [set(vote) for vote in reader['votes']] == [
        {'id', 'choice', 'comment', 'confirmed_at', 'share_id', 'share_title', 'stale'}] * 2
    assert '@' not in for_reader.content.decode()
    assert for_writer['Cache-Control'] == 'no-store'
    assert analyst.get('/api/v1/code-mappings/999999999/votes/').status_code == 404


def test_staff_see_addresses():
    staff = client_for(Identity.objects.create_user(email='staff-votes@example.test', is_staff=True))
    share = make_share()
    steps = proposal('STEPS')
    cast(APIClient(), share, [yes(steps)])

    assert staff.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()['votes'][0]['voter_email'] == VOTER


def test_a_vote_never_changes_the_mapping(anonymous, many_submissions):
    share = make_share()
    steps = proposal('STEPS')
    before = SourceCodeConceptMapping.objects.filter(pk=steps.pk).values().get()

    cast(anonymous, share, [no(steps, 'Wrong concept')])
    for index in range(4):
        cast(anonymous, share, [yes(steps)], email=f'voter-{index}@example.test')

    assert SourceCodeConceptMapping.objects.filter(pk=steps.pk).values().get() == before


def test_votes_on_an_earlier_destination_go_stale(analyst, anonymous):
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps, 'Right concept')])
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(target_concept=loinc('NEW-1', 'Another concept'))
    cast(anonymous, share, [no(steps)], email='later@example.test')

    data = analyst.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()
    [row] = analyst.get('/api/v1/code-mappings/', {'browse': '1', 'source': 'OpenWearables'}).json()['results']

    assert data['tally'] == row['votes'] == {'yes': 0, 'no': 1, 'comments': 0}
    assert [(vote['voter_email'], vote['stale']) for vote in data['votes']] == [
        ('later@example.test', False), (VOTER, True)]
    assert MappingVote.objects.count() == 2, 'the stale vote is kept'


def test_a_stale_vote_counts_again_if_the_destination_returns(analyst, anonymous):
    share = make_share()
    steps = proposal('STEPS')
    original = steps.target_concept_id
    cast(anonymous, share, [yes(steps)])
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(target_concept=loinc('NEW-1', 'Another'))
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(target_concept_id=original)

    assert analyst.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()['tally']['yes'] == 1


def test_the_list_carries_each_rows_current_tally(analyst, doctor, anonymous, many_submissions):
    share, other = make_share(), make_share(title='Other')
    steps, sleep, quiet = proposal('STEPS'), proposal('SLEEP'), proposal('QUIET')
    cast(anonymous, share, [yes(steps, 'Agree'), no(sleep)])
    cast(anonymous, share, [yes(steps), {'mapping': sleep.pk, 'comment': 'Unit?'}], email='second@example.test')
    cast(anonymous, other, [no(steps, 'Disagree')])
    submit(anonymous, share, [yes(quiet)], email='pending@example.test')

    for client in (analyst, doctor):
        rows = {row['source_code']: row['votes'] for row in client.get(
            '/api/v1/code-mappings/', {'browse': '1', 'source': 'OpenWearables'}).json()['results']}
        assert rows == {
            'STEPS': {'yes': 2, 'no': 1, 'comments': 2},
            'SLEEP': {'yes': 0, 'no': 1, 'comments': 1},
            'QUIET': {'yes': 0, 'no': 0, 'comments': 0},
        }
    flat = {row['source_code']: row['votes'] for row in analyst.get(
        '/api/v1/code-mappings/', {'source': 'Apple'}).json()}
    assert flat['STEPS'] == {'yes': 2, 'no': 1, 'comments': 2}


def test_the_list_tally_costs_no_query_per_row_or_per_vote(analyst, anonymous, many_submissions):
    share = make_share()
    first = proposal('FIRST', snomed('S-FIRST'))
    cast(anonymous, share, [yes(first)])
    params = {'browse': '1', 'source': 'OpenWearables', 'include_duplicates': '0'}
    with CaptureQueriesContext(connection) as small:
        assert analyst.get('/api/v1/code-mappings/', params).status_code == 200
    more = [proposal(f'NEXT-{index}', snomed(f'S-{index}')) for index in range(12)]
    for index in range(3):
        cast(anonymous, share, [yes(mapping, 'ok') for mapping in more], email=f'voter-{index}@example.test')
    AuditEvent.objects.all().delete()
    with CaptureQueriesContext(connection) as large:
        response = analyst.get('/api/v1/code-mappings/', params)

    assert len(response.json()['results']) == 13
    assert len(large) == len(small)


def test_a_single_row_response_and_a_machine_read_carry_no_tally(analyst):
    steps = proposal('STEPS')

    assert 'votes' not in analyst.get(f'/api/v1/code-mappings/{steps.pk}/').json()


def test_deleting_a_mapping_deletes_its_votes(anonymous):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    cast(anonymous, share, [yes(steps), yes(sleep)])

    steps.delete()

    assert list(MappingVote.objects.values_list('mapping_id', flat=True)) == [sleep.pk]


def test_the_votes_csv_is_for_writers_and_quotes_what_strangers_typed(analyst, anonymous, many_submissions):
    share, other = make_share(title='HL7 review'), make_share(title='Other')
    steps = proposal('HKSteps', loinc('55423-8', 'Number of steps'), metric_key='steps',
                     source_code_description='Step count')
    sleep = proposal('SLEEP', category='Sleep', metric_key='sleep')
    cast(anonymous, share, [yes(sleep), no(steps, '=HYPERLINK("http://evil.example","click")')])
    cast(anonymous, share, [{'mapping': steps.pk, 'comment': '+1 from me\nsecond line'}], email='b@example.test')
    cast(anonymous, other, [yes(steps)], email='elsewhere@example.test')
    SourceCodeConceptMapping.objects.filter(pk=sleep.pk).update(target_concept=loinc('NEW-1', 'Another concept'))

    response = analyst.get(f'{SHARES}{share.pk}/votes-export/')

    assert response.status_code == 200
    assert response['Content-Type'] == 'text/csv; charset=utf-8'
    assert response['Content-Disposition'] == f'attachment; filename="mapping-votes-share-{share.pk}.csv"'
    assert response['Cache-Control'] == 'no-store'
    assert response.content.startswith(b'\xef\xbb\xbf')
    lines = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert lines[0] == list(mapping_votes.CSV_COLUMNS)
    rows = [dict(zip(lines[0], line)) for line in lines[1:]]
    assert [(row['Source code'], row['Voter email'], row['Vote'], row['Comment'], row['Stale']) for row in rows] == [
        ('HKSteps', 'b@example.test', '', "'+1 from me\nsecond line", 'no'),
        ('HKSteps', VOTER, 'no', '\'=HYPERLINK("http://evil.example","click")', 'no'),
        ('SLEEP', VOTER, 'yes', '', 'yes'),
    ]
    assert rows[1]['Voted on destination'] == 'LOINC:55423-8' and rows[1]['Voted on concept'] == 'Number of steps'
    assert rows[1]['Source'] == 'HealthKit' and rows[1]['Description'] == 'Step count'
    assert rows[2]['Voted on destination'] == f'LOINC:L-SLEEP', 'the destination the voter saw'
    assert 'elsewhere@example.test' not in response.content.decode()
    assert analyst.get(f'{SHARES}999999/votes-export/').status_code == 404


def test_an_empty_share_exports_a_header_only(analyst):
    share = make_share()

    lines = analyst.get(f'{SHARES}{share.pk}/votes-export/').content.decode('utf-8-sig').splitlines()

    assert lines == [','.join(mapping_votes.CSV_COLUMNS)]
