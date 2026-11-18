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
import threading
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver, resolve
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from omop_core.models import (
    GroupAccess, MappingVote, MappingVoteEmail, MappingVoteShare, MappingVoteSubmission,
    Organization, SourceCodeConceptMapping,
)
from omop_core.services import mapping_vote_jobs, mapping_votes
from patient_portal.api import mapping_votes as vote_api
from patient_portal.models import AuditEvent, Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

SHARES = '/api/v1/code-mappings/vote-shares/'
PAGE = '/api/v1/public/mapping-votes/'
SUBMIT = f'{PAGE}submit/'
PREVIEW = f'{PAGE}confirm/preview/'
CONFIRM = f'{PAGE}confirm/'
EXPORT = f'{PAGE}export/'
PUBLIC_PATHS = {PAGE, SUBMIT, PREVIEW, CONFIRM, EXPORT}
VOTER = 'voter@example.test'
LINK = re.compile(r'/vote/confirm#([A-Za-z0-9_-]+)')
NOT_AVAILABLE = {'detail': 'This link is not available.'}
LINK_INVALID = {'detail': 'This confirmation link is no longer valid.'}
UNAVAILABLE = {'detail': 'Voting is temporarily unavailable. Please try again later.'}
WELL_FORMED = 'never-issued-but-well-formed-token-0123456789'


@pytest.fixture(autouse=True)
def fresh_throttles():
    """The throttles count in the process cache, which outlives a test."""
    cache.clear()
    yield
    cache.clear()


def with_role(role, name=None, **org):
    user = Identity.objects.create_user(email=f'{name or role}-votes@example.test')
    organization, _ = Organization.objects.get_or_create(
        slug=org.pop('slug', 'votes-org'), defaults={'name': 'Votes Org', **org})
    GroupAccess.objects.create(identity=user, org=organization, role=role)
    return user


def client_for(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def admin():
    """An org admin: approves mappings, so manages shares and sees addresses."""
    return client_for(with_role('org_admin'))


@pytest.fixture
def analyst():
    """Writes mappings, and still does not manage shares."""
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
    """Lift the per-client throttles, for a test that is about the limits
    counted in the database."""
    with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {
            'mapping_vote_submit': '1000/minute', 'mapping_vote_confirm': '1000/minute'}):
        yield


@pytest.fixture
def queued():
    """Hand confirmation emails to a dispatcher that sends nothing."""
    with mapping_vote_jobs.use_dispatcher(mapping_vote_jobs.FakeDispatcher()) as dispatcher:
        yield dispatcher


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


def raw(client, url, body, **extra):
    """POST *body* as it is, for JSON the test client would refuse to write."""
    return client.post(url, body, content_type='application/json', **extra)


def page(client, share, **extra):
    token = share.token if isinstance(share, MappingVoteShare) else share
    return client.post(PAGE, {'token': token}, format='json', **extra)


def submit(client, share, votes, email=VOTER, **extra):
    return client.post(SUBMIT, {'token': share.token, 'email': email, 'votes': votes}, format='json', **extra)


def preview(client, token, **extra):
    return client.post(PREVIEW, {'token': token}, format='json', **extra)


def confirm(client, token, **extra):
    return client.post(CONFIRM, {'token': token}, format='json', **extra)


def emailed_token(index=-1):
    return LINK.search(mail.outbox[index].body).group(1)


def cast(client, share, votes, email=VOTER):
    """Submit and confirm: the whole of a voter's part."""
    before = len(mail.outbox)
    assert submit(client, share, votes, email=email).status_code == 202
    assert len(mail.outbox) == before + 1
    response = confirm(client, emailed_token())
    assert response.status_code == 200, response.content
    return response.json()


def yes(mapping, comment=''):
    return {'mapping': mapping.pk, 'choice': 'yes', 'comment': comment}


def no(mapping, comment=''):
    return {'mapping': mapping.pk, 'choice': 'no', 'comment': comment}


def listed_codes(client, share):
    return [entry['source_code'] for group in page(client, share).json()['groups']
            for entry in group['proposals']]


def in_threads(calls):
    """Run *calls* at the same moment, each on its own connection; what each
    returned, or the exception it raised."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def run(index, call):
        try:
            barrier.wait(timeout=10)
            results[index] = call()
        except Exception as error:  # noqa: BLE001 - the test reads it
            results[index] = error
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=item) for item in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), 'a call never returned'
    return results


# ── Sharing ──────────────────────────────────────────────────────────────

def test_a_mapping_administrator_creates_a_share_and_gets_its_public_link(admin, settings):
    settings.APP_BASE_URL = 'https://promop.example.test'

    response = admin.post(SHARES, {'title': '  HL7 review  ', 'note': 'Please vote by Friday.'}, format='json')

    assert response.status_code == 201, response.content
    share = MappingVoteShare.objects.get()
    assert share.title == 'HL7 review' and share.note == 'Please vote by Friday.'
    assert mapping_votes.valid_token(share.token) and len(share.token) >= 43
    data = response.json()
    # The token is the fragment: a browser sends it to no server.
    assert data['url'] == f'https://promop.example.test/vote#{share.token}'
    assert data['active'] is True and data['voters'] == 0 and data['expires_at'] is None
    assert response['Cache-Control'] == 'no-store'


def test_staff_manage_shares():
    staff = client_for(Identity.objects.create_user(email='staff-votes@example.test', is_staff=True))

    assert staff.post(SHARES, {'title': 'Review'}, format='json').status_code == 201
    assert 'url' in staff.get(SHARES).json()['results'][0]


def test_two_shares_never_get_the_same_token(admin):
    for _ in range(2):
        assert admin.post(SHARES, {'title': 'Review'}, format='json').status_code == 201
    first, second = MappingVoteShare.objects.values_list('token', flat=True)
    assert first != second


@pytest.mark.parametrize('body', [
    {}, {'title': '   '}, {'title': 'x' * 201}, {'title': 'ok', 'note': 'n' * 2001},
    {'title': 'ok', 'expires_at': '2000-01-01'}, {'title': 'ok', 'expires_at': 'soon'},
    {'title': 'ok', 'expires_at': '2099-01-01T00:00:00'}, {'title': 7}, {'title': 'ok', 'note': 7},
    {'title': 'ok', 'expires_at': 20270101},
    # Further ahead than a share may live, and an instant no database holds.
    {'title': 'ok', 'expires_at': '2099-01-01'},
    {'title': 'ok', 'expires_at': '9999-12-31T23:59:59-23:59'},
    {'title': 'ok', 'expires_at': '0001-01-01T00:00:00+23:59'},
    # The title goes into the confirmation email: no link of the writer's.
    {'title': 'See https://evil.example/login'}, {'title': 'see HTTP://evil.example'},
    {'title': 'visit www.evil.example now'},
])
def test_a_share_is_refused_for_a_bad_title_note_or_expiry(admin, body):
    response = admin.post(SHARES, body, format='json')

    assert response.status_code == 400, response.content
    assert not MappingVoteShare.objects.exists()


def test_text_that_is_not_valid_unicode_is_refused_not_stored(admin):
    """A lone surrogate, which a JSON escape can spell and neither the
    database nor a mail backend can hold."""
    for body in ('{"title": "a\\ud800b"}', '{"title": "ok", "note": "a\\udfffb"}'):
        response = raw(admin, SHARES, body)
        assert response.status_code == 400, response.content
    assert not MappingVoteShare.objects.exists()


def test_a_title_is_one_line_without_control_characters(admin):
    title = 'HL7\r\nreview \u2028 round\x00 two\x07\u2029'

    assert admin.post(SHARES, {'title': title, 'note': 'line one\nline two'}, format='json').status_code == 201

    share = MappingVoteShare.objects.get()
    assert share.title == 'HL7 review round two'
    assert share.note == 'line one\nline two'


def test_an_expiry_date_runs_to_the_end_of_that_day(admin):
    day = (timezone.now() + datetime.timedelta(days=3)).date()

    response = admin.post(SHARES, {'title': 'Review', 'expires_at': day.isoformat()}, format='json')

    assert response.status_code == 201
    expires = MappingVoteShare.objects.get().expires_at
    assert expires.date() == day and (expires.hour, expires.minute) == (23, 59)


def test_an_expiry_with_an_offset_is_stored_as_the_same_instant(admin):
    moment = (timezone.now() + datetime.timedelta(days=30)).replace(microsecond=0)
    local = moment.astimezone(datetime.timezone(datetime.timedelta(hours=-7)))

    assert admin.post(SHARES, {'title': 'Review', 'expires_at': local.isoformat()}, format='json').status_code == 201

    assert MappingVoteShare.objects.get().expires_at == moment


def refused_everything(client, share, mapping):
    """Whether *client* is refused every act of managing a share."""
    return [
        client.post(SHARES, {'title': 'Mine'}, format='json').status_code,
        client.post(f'{SHARES}{share.pk}/revoke/').status_code,
        client.delete(f'{SHARES}{share.pk}/').status_code,
        client.get(f'{SHARES}{share.pk}/votes-export/').status_code,
    ] == [403] * 4


@pytest.mark.parametrize('role', ['analyst', 'doctor'])
def test_reading_or_writing_mappings_is_not_managing_shares(role, anonymous):
    """An analyst writes mappings and still hands out no voting link and
    reads no voter's address: that is for those who approve."""
    client = client_for(with_role(role))
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps, 'Agree')])

    assert refused_everything(client, share, steps)
    listed = client.get(SHARES)
    votes = client.get(f'/api/v1/code-mappings/{steps.pk}/votes/')

    assert listed.status_code == votes.status_code == 200
    assert listed.json()['can_manage'] is False
    assert [set(entry) for entry in listed.json()['results']] == [{
        'id', 'title', 'note', 'created_at', 'expires_at', 'revoked_at', 'active', 'voters', 'votes', 'source_vocabularies'}]
    assert share.token not in listed.content.decode()
    assert votes.json()['emails_included'] is False
    assert votes.json()['tally'] == {'yes': 1, 'no': 0, 'comments': 1}
    assert '@' not in votes.content.decode()
    share.refresh_from_db()
    assert share.revoked_at is None and MappingVote.objects.count() == 1
    assert MappingVoteShare.objects.count() == 1


def test_a_self_signed_up_analyst_at_a_demo_org_manages_nothing(anonymous):
    demo = client_for(with_role('analyst', 'demo', slug='demo-org', allows_patient_signup=True))
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps)])

    assert refused_everything(demo, share, steps)
    assert 'url' not in demo.get(SHARES).json()['results'][0]
    assert 'voter_email' not in demo.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()['votes'][0]


@pytest.mark.parametrize(('method', 'path'), [
    ('get', SHARES), ('post', SHARES), ('post', f'{SHARES}1/revoke/'), ('delete', f'{SHARES}1/'),
    ('get', f'{SHARES}1/votes-export/'), ('get', '/api/v1/code-mappings/1/votes/'),
])
def test_the_curator_endpoints_need_a_session(anonymous, method, path):
    assert getattr(anonymous, method)(path).status_code in (401, 403)


@pytest.mark.parametrize('scopes', ['system/*.read', 'system/*.read system/etl.write system/etl.delete'])
def test_a_service_token_reaches_none_of_the_curator_endpoints(settings, scopes, anonymous):
    """A machine reads the mapping table (#2340); what people voted, and who
    they are, is not part of it."""
    secret = 'votes-service-secret-000000000000000000000'
    settings.SERVICE_AUTH_TOKENS = {'dbt': {'token': secret, 'scopes': scopes}}
    settings.SERVICE_AUTH_TOKEN = ''
    machine = APIClient()
    machine.credentials(HTTP_AUTHORIZATION=f'Bearer {secret}')
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps)])

    assert machine.get('/api/v1/code-mappings/', {'source': 'Apple'}).status_code == 200, 'the token works'
    assert refused_everything(machine, share, steps)
    assert machine.get(SHARES).status_code == 403
    assert machine.get(f'/api/v1/code-mappings/{steps.pk}/votes/').status_code == 403
    assert all('votes' not in row for row in machine.get('/api/v1/code-mappings/', {'source': 'Apple'}).json())
    assert MappingVoteShare.objects.count() == 1 and MappingVote.objects.count() == 1


def test_a_patient_reaches_none_of_the_curator_endpoints():
    patient = client_for(with_role('patient'))
    share = make_share()
    mapping = proposal('STEPS')

    assert refused_everything(patient, share, mapping)
    assert patient.get(SHARES).status_code == 403
    assert patient.get(f'/api/v1/code-mappings/{mapping.pk}/votes/').status_code == 403


def test_the_share_list_counts_confirmed_voters_not_votes(admin, anonymous, many_submissions):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    cast(anonymous, share, [yes(steps), no(sleep)])
    cast(anonymous, share, [yes(steps)], email='second@example.test')
    submit(anonymous, share, [yes(steps)], email='pending@example.test')

    data = admin.get(SHARES).json()

    assert data['can_manage'] is True
    assert (data['results'][0]['voters'], data['results'][0]['votes']) == (2, 3)


def test_the_share_list_says_whether_voting_can_work_here(admin, settings):
    """Without a mail backend voters are told to check a mailbox nothing
    reaches; the dialog warns the curator from these two flags."""
    settings.EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    settings.APP_BASE_URL = 'http://localhost:5173'
    unconfigured = admin.get(SHARES).json()
    settings.EMAIL_BACKEND = 'anymail.backends.mailgun.EmailBackend'
    settings.APP_BASE_URL = 'https://promop.example.test'
    configured = admin.get(SHARES).json()

    assert (unconfigured['email_configured'], unconfigured['base_url_configured']) == (False, False)
    assert (configured['email_configured'], configured['base_url_configured']) == (True, True)
    for backend in ('dummy', 'locmem', 'filebased'):
        settings.EMAIL_BACKEND = f'django.core.mail.backends.{backend}.EmailBackend'
        assert admin.get(SHARES).json()['email_configured'] is False, backend


def test_revoking_keeps_confirmed_votes_and_drops_pending_ones(admin, anonymous):
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps)])
    submit(anonymous, share, [no(steps)], email='late@example.test')
    token = emailed_token()

    response = admin.post(f'{SHARES}{share.pk}/revoke/')

    assert response.status_code == 200 and response.json()['active'] is False
    assert response.json()['voters'] == 1
    assert MappingVote.objects.count() == 1
    assert not MappingVoteSubmission.objects.exists()
    assert confirm(anonymous, token).status_code == 404
    assert admin.post(f'{SHARES}999999/revoke/').status_code == 404


def test_deleting_a_share_erases_its_votes_and_addresses(admin, anonymous, many_submissions):
    """How junk votes go, and how a voter's request to be removed is met."""
    share, kept = make_share(), make_share(title='Kept')
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps, 'Agree')])
    cast(anonymous, kept, [no(steps)], email='other@example.test')
    submit(anonymous, share, [no(steps)], email='pending@example.test')
    token = emailed_token()

    response = admin.delete(f'{SHARES}{share.pk}/')

    assert response.status_code == 204
    assert list(MappingVoteShare.objects.values_list('title', flat=True)) == ['Kept']
    assert list(MappingVote.objects.values_list('voter_email', flat=True)) == ['other@example.test']
    assert not MappingVoteSubmission.objects.exists()
    assert page(anonymous, share.token).status_code == 404
    assert confirm(anonymous, token).status_code == 404
    # The mapping is untouched, and the emails already sent still count:
    # deleting a share buys no fresh allowance.
    assert SourceCodeConceptMapping.objects.filter(pk=steps.pk).exists()
    assert MappingVoteEmail.objects.count() == 3
    assert MappingVoteEmail.objects.filter(share__isnull=True).count() == 2
    assert admin.delete(f'{SHARES}{share.pk}/').status_code == 404


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

    response = page(anonymous, share)

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'title', 'note', 'expires_at', 'comment_max_length', 'groups', 'source_systems'}
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


def test_the_public_payload_is_reference_data_only(anonymous):
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

    response = page(anonymous, share)

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


def test_only_described_global_wearable_proposals_with_a_destination_are_votable(anonymous):
    share = make_share()
    org = Organization.objects.create(slug='hospital', name='Hospital')
    open_one = proposal('OPEN')
    proposal('APPROVED', status='approved')
    proposal('REJECTED', status='rejected')
    proposal('SCOPED', organization=org)
    proposal('NOT-WEARABLE', source_vocabulary_id='ICD10CM')
    SourceCodeConceptMapping.objects.create(
        source_code='NO-DESTINATION', source_vocabulary_id='Apple', status='proposed', metric_key='steps')
    SourceCodeConceptMapping.objects.create(
        source_code='UNLOADED', source_vocabulary_id='Apple', status='proposed', metric_key='steps',
        target_concept_id=77_777_777)
    # Queued by ingest from a producer's text and given a destination by
    # Suggest: nobody has described it, so nobody outside is shown it.
    undescribed = proposal('INGEST-QUEUED <text a producer sent>', metric_key='')

    listed = [entry['id'] for group in page(anonymous, share).json()['groups'] for entry in group['proposals']]
    refused = submit(anonymous, share, [yes(open_one), yes(undescribed)])

    assert listed == [open_one.pk]
    assert refused.status_code == 400 and not MappingVoteSubmission.objects.exists()


def test_the_page_follows_the_proposals_as_they_change(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    assert listed_codes(anonymous, share) == ['STEPS']
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(status='approved')
    proposal('SLEEP')
    assert listed_codes(anonymous, share) == ['SLEEP']


def test_unknown_revoked_and_expired_links_are_one_answer(anonymous):
    proposal('STEPS')
    revoked = make_share()
    mapping_votes.revoke_share(revoked)
    expired = make_share()
    MappingVoteShare.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
    tokens = [WELL_FORMED, revoked.token, expired.token, 'short', 'x' * 500, '', None, 12345, ['a'], {'a': 1}]

    answers = [page(anonymous, token) for token in tokens]
    answers += [anonymous.post(SUBMIT, {'token': token, 'email': VOTER, 'votes': []}, format='json')
                for token in tokens]
    answers += [anonymous.post(PAGE, {}, format='json'), anonymous.post(SUBMIT, {'email': VOTER}, format='json')]

    for response in answers:
        assert response.status_code == 404
        assert response.json() == NOT_AVAILABLE
        assert response['Cache-Control'] == 'no-store'
    assert len({response.content for response in answers}) == 1
    assert not mail.outbox


@pytest.mark.parametrize('token', [
    'short', 'x' * 129, 'has space in the middle of it', 'semi;colon-and-more-characters',
    'nul\x00byte-in-a-token-of-length', 'percent%00-in-a-token-of-length', '../../../etc/passwd-and-more',
    'ünïcödé-token-of-sufficient-length', 12345, None, True, ['list'], {'a': 1}, 1.5,
])
def test_a_token_of_the_wrong_shape_reaches_no_query(anonymous, token):
    """Checked before the database is asked: a NUL byte used to be a 500."""
    for url, invalid in ((PAGE, NOT_AVAILABLE), (SUBMIT, NOT_AVAILABLE),
                         (PREVIEW, LINK_INVALID), (CONFIRM, LINK_INVALID)):
        with CaptureQueriesContext(connection) as queries:
            response = anonymous.post(url, {'token': token, 'email': VOTER, 'votes': []}, format='json')
        assert response.status_code == 404 and response.json() == invalid, url
        assert not [query for query in queries if 'mapping_vote' in query['sql']], url


def test_the_public_endpoints_set_no_cookie_and_are_not_cached(anonymous):
    share = make_share()
    steps = proposal('STEPS')

    answers = [page(anonymous, share), submit(anonymous, share, [yes(steps)])]
    answers += [preview(anonymous, emailed_token()), confirm(anonymous, emailed_token())]

    for response in answers:
        assert response.status_code in (200, 202)
        assert not response.cookies
        assert 'Set-Cookie' not in response
        assert response['Cache-Control'] == 'no-store'
        assert response['Referrer-Policy'] == 'no-referrer'
    assert not anonymous.session.keys()


def test_the_public_endpoints_answer_json_and_nothing_else(anonymous):
    """The browsable API drew a form around the visitor's own submission,
    address included."""
    share = make_share()
    steps = proposal('STEPS')
    body = {'token': share.token, 'email': VOTER, 'votes': [yes(steps)]}

    as_html = anonymous.post(SUBMIT, body, format='json', HTTP_ACCEPT='text/html')
    by_query = anonymous.post(f'{SUBMIT}?format=api', body, format='json')
    form = anonymous.post(SUBMIT, {'token': share.token, 'email': VOTER, 'votes': str(steps.pk)})

    for response in (as_html, by_query, form):
        assert response['Content-Type'] == 'application/json'
        assert b'<' not in response.content
        assert VOTER.encode() not in response.content
    assert (as_html.status_code, by_query.status_code, form.status_code) == (406, 404, 415)
    assert not MappingVoteSubmission.objects.exists()
    for view in (vote_api.PublicVotePageView, vote_api.PublicVoteSubmitView,
                 vote_api.PublicVotePreviewView, vote_api.PublicVoteConfirmView):
        assert [renderer.format for renderer in view.renderer_classes] == ['json']
        assert view.http_method_names and not hasattr(view, 'get')


def test_a_signed_in_visitor_with_a_stale_session_is_still_just_a_visitor():
    """The pages authenticate nobody: a session cookie causes no CSRF refusal
    and earns nothing."""
    user = with_role('doctor', 'visitor')
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    share = make_share()
    steps = proposal('STEPS')

    assert page(client, share).status_code == 200
    assert submit(client, share, [yes(steps)]).status_code == 202
    assert confirm(client, emailed_token()).status_code == 200


def test_a_flagged_session_does_not_lock_the_public_pages():
    user = with_role('doctor', 'flagged')
    user.must_change_password = True
    user.save()
    client = APIClient()
    client.force_login(user)
    share = make_share()
    proposal('STEPS')

    assert page(client, share).status_code == 200
    assert client.get(SHARES).status_code == 403


def test_the_forced_password_change_exemption_reaches_only_the_public_views():
    """The middleware lets a path prefix through; everything routed under
    that prefix must be a view that reads no session."""
    from patient_portal.api.middleware import _PUBLIC_VOTE_PREFIX

    def routes(resolver, prefix=''):
        for pattern in resolver.url_patterns:
            route = prefix + str(pattern.pattern)
            if hasattr(pattern, 'url_patterns'):
                yield from routes(pattern, route)
            else:
                yield route, pattern

    under = {route: pattern for route, pattern in routes(get_resolver())
             if ('/' + route).startswith(_PUBLIC_VOTE_PREFIX)}

    assert {'/' + route for route in under} == PUBLIC_PATHS
    for route, pattern in under.items():
        view = pattern.callback.view_class
        assert issubclass(view, vote_api._PublicVoteView), route
        assert view.authentication_classes == [] and pattern.name.startswith('v1-public-mapping-vote')
    assert _PUBLIC_VOTE_PREFIX == PAGE


def test_the_endpoints_are_on_v1_only_and_carry_no_token_in_the_url():
    """The legacy /api/ router has none of them, and no route takes a token:
    a token in a path would be in every access log."""
    for path in ('public/mapping-votes/', 'public/mapping-votes/submit/',
                 'public/mapping-votes/confirm/preview/', 'public/mapping-votes/confirm/',
                 'code-mappings/vote-shares/', 'code-mappings/vote-shares/1/',
                 'code-mappings/vote-shares/1/revoke/', 'code-mappings/vote-shares/1/votes-export/',
                 'code-mappings/1/votes/'):
        match = resolve(f'/api/v1/{path}')
        assert match.url_name.startswith('v1-'), path
        assert not [key for key in match.kwargs if 'token' in key], path
        assert resolve(f'/api/{path}').url_name == 'home', path
    for path in (f'public/mapping-votes/{WELL_FORMED}/', f'public/mapping-votes/{WELL_FORMED}/submit/',
                 f'public/mapping-votes/confirm/{WELL_FORMED}/'):
        assert resolve(f'/api/v1/{path}').url_name == 'home', path


def test_the_public_page_costs_the_same_queries_at_any_size(anonymous):
    share = make_share()
    proposal('FIRST')
    with CaptureQueriesContext(connection) as small:
        assert page(anonymous, share).status_code == 200
    for index in range(25):
        proposal(f'NEXT-{index}', category='Sleep' if index % 2 else 'Activity')
    AuditEvent.objects.all().delete()
    cache.clear()
    with CaptureQueriesContext(connection) as large:
        assert page(anonymous, share).status_code == 200

    assert len(large) == len(small)


def test_no_token_reaches_the_audit_trail_whatever_the_request(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    page(anonymous, share)
    submit(anonymous, share, [yes(steps)])
    token = emailed_token()
    preview(anonymous, token)
    confirm(anonymous, token)
    # Refusals and failures are audited like anything else.
    confirm(anonymous, token)
    page(anonymous, WELL_FORMED)
    submit(anonymous, share, [], email='nobody')
    raw(anonymous, SUBMIT, '{"token": ')
    anonymous.get(PAGE)
    with mock.patch.object(mapping_votes, 'public_payload', side_effect=RuntimeError('boom')):
        assert page(APIClient(raise_request_exception=False), share).status_code == 500

    events = list(AuditEvent.objects.values_list('path', 'detail', 'resource_id', 'user_email'))

    assert len(events) == 10, 'every request is still audited'
    assert {path for path, *_ in events} <= PUBLIC_PATHS
    stored = json.dumps(events, default=str)
    assert share.token not in stored and token not in stored and WELL_FORMED not in stored
    assert VOTER not in stored


@pytest.mark.parametrize('debug', [False, True])
def test_no_address_comment_or_token_is_logged(anonymous, caplog, settings, debug):
    settings.DEBUG = debug
    share = make_share()
    steps = proposal('STEPS')
    caplog.set_level(logging.DEBUG)
    # The request log and the audit line belong to loggers that do not
    # propagate, so they are listened to directly.
    silent = [logger for logger in logging.Logger.manager.loggerDict.values()
              if isinstance(logger, logging.Logger) and not logger.propagate]
    for logger in silent:
        logger.addHandler(caplog.handler)

    with mock.patch.object(mapping_votes.EmailMessage, 'send', side_effect=OSError(
            'Sending a message to private-address@example.test failed')):
        submit(anonymous, share, [yes(steps, 'PRIVATE-COMMENT')], email='private-address@example.test')
    cast(anonymous, share, [yes(steps, 'PRIVATE-COMMENT')], email='private-address@example.test')
    token = emailed_token()
    # A crash on each public route: the path it is reported under has no token.
    for target, call in (('public_payload', lambda client: page(client, share)),
                         ('submit', lambda client: submit(client, share, [yes(steps)])),
                         ('pending_summary', lambda client: preview(client, token)),
                         ('confirm', lambda client: confirm(client, token))):
        with mock.patch.object(mapping_votes, target, side_effect=RuntimeError('boom')):
            assert call(APIClient(raise_request_exception=False)).status_code == 500

    for logger in silent:
        logger.removeHandler(caplog.handler)
    logged = '\n'.join(
        record.getMessage() + (logging.Formatter().formatException(record.exc_info) if record.exc_info else '')
        for record in caplog.records)
    assert f'"path": "{CONFIRM}"' in logged, 'the audit line was heard'
    assert 'could not be sent (OSError)' in logged and 'Internal Server Error' in logged
    assert 'private-address' not in logged and 'PRIVATE-COMMENT' not in logged
    assert token not in logged and share.token not in logged


# ── Submitting ───────────────────────────────────────────────────────────

def test_a_submission_is_not_a_vote(anonymous, admin):
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
    assert admin.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json() == {
        'tally': {'yes': 0, 'no': 0, 'comments': 0}, 'emails_included': True, 'votes': []}
    assert admin.get(SHARES).json()['results'][0]['voters'] == 0
    [row] = admin.get('/api/v1/code-mappings/', {'browse': '1', 'source': 'OpenWearables'}).json()['results']
    assert row['votes'] == {'yes': 0, 'no': 0, 'comments': 0}
    assert 'Looks right' not in admin.get(f'{SHARES}{share.pk}/votes-export/').content.decode('utf-8-sig')


def test_the_confirmation_email_carries_the_link_and_nothing_anyone_typed_but_the_title(anonymous, settings):
    settings.APP_BASE_URL = 'https://promop.example.test'
    share = make_share(title='HL7 review', note='NOTE-TEXT see https://elsewhere.example')
    steps, sleep, energy = proposal('STEPS'), proposal('SLEEP'), proposal('ENERGY')

    submit(anonymous, share, [
        yes(steps, 'VOTER-TEXT-ONE'), no(sleep), {'mapping': energy.pk, 'comment': 'VOTER-TEXT-TWO'},
    ], email='  Voter@Example.TEST ')

    [message] = mail.outbox
    assert message.to == [VOTER]
    assert message.subject == 'Confirm your votes on the wearable mapping proposals'
    token = emailed_token()
    assert f'https://promop.example.test/vote/confirm#{token}' in message.body
    assert 'HL7 review' in message.body
    assert '2 votes and 2 comments' in message.body
    assert 'expires in 48 hours' in message.body
    assert 'VOTER-TEXT' not in message.body and 'NOTE-TEXT' not in message.body
    assert message.body.count('http') == 1, 'the confirmation link is the only link'
    assert not getattr(message, 'alternatives', None) and message.content_subtype == 'plain'
    assert mapping_votes.valid_token(token) and len(token) >= 43


def test_only_the_hash_of_the_confirmation_token_is_stored(anonymous):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    token = emailed_token()

    pending = MappingVoteSubmission.objects.get()

    assert pending.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(list(MappingVoteSubmission.objects.values()), default=str)
    record = MappingVoteEmail.objects.get()
    assert '@' not in record.email_key and len(record.email_key) == 64
    assert record.share == share and record.submission == pending


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
    # Addresses a validator passes and no confirmation can reach.
    'someone@localhost', 'someone@mail.localhost', 'someone@intranet', 'someone@[127.0.0.1]',
    'someone@[IPv6:::1]', 'someone@127.0.0.1', 'someone@10.0.0.8', '"ctl\x01char"@example.test',
    '"tab\tchar"@example.test', 'bidi\u202e@example.test',
])
def test_an_invalid_or_undeliverable_email_is_refused(anonymous, email):
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


@pytest.mark.parametrize('choice', ['maybe', 'YES', 'approve', 1, True, ['yes'], {'yes': 1}])
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
    {'mapping': -1, 'choice': 'yes'}, {'mapping': 2 ** 70, 'choice': 'yes'}, [], [[]],
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
        yes(steps, '  line one\r\nline\x00 two\x07\x1b[31m \u202egnp.exe next  '),
        {'mapping': sleep.pk, 'comment': 'y' * 1000},
    ])

    assert too_long.status_code == 400 and not_text.status_code == 400
    assert accepted.status_code == 202
    first, second = MappingVoteSubmission.objects.get().payload
    assert first['comment'] == 'line one\nline two[31m gnp.exenext'
    assert len(second['comment']) == 1000


def test_text_that_is_not_valid_unicode_is_refused_in_a_submission(anonymous):
    """Lone surrogates: a 500 from the database in a comment, and from the
    mail library in an address."""
    share = make_share()
    steps = proposal('STEPS')
    template = '{"token": "%s", "email": %%s, "votes": [{"mapping": %d, "choice": "yes", "comment": %%s}]}' % (
        share.token, steps.pk)

    in_comment = raw(anonymous, SUBMIT, template % ('"voter@example.test"', '"a\\ud800b"'))
    in_email = raw(anonymous, SUBMIT, template % ('"vo\\udc00ter@example.test"', '"fine"'))
    in_token = raw(anonymous, SUBMIT, '{"token": "\\ud800%s", "email": "v@example.test", "votes": []}' % share.token)

    assert in_comment.status_code == 400 and 'valid text' in in_comment.json()['detail']
    assert in_email.status_code == 400 and in_email.json() == {'detail': 'Enter a valid email address.'}
    assert in_token.status_code == 404
    assert not MappingVoteSubmission.objects.exists() and not mail.outbox


def test_json_nested_deeper_than_the_parser_is_a_400_on_every_public_route(anonymous):
    """100,000 brackets fit inside the size cap and used to end in a
    RecursionError inside the JSON parser."""
    share = make_share()
    steps = proposal('STEPS')
    depth = 90_000
    top_level = '[' * depth + ']' * depth
    inside = '{"token": "%s", "email": "%s", "votes": [{"mapping": %d, "comment": %s}]}' % (
        share.token, VOTER, steps.pk, '[' * depth + ']' * depth)
    assert len(inside) < mapping_votes.MAX_BODY_BYTES

    for url in (PAGE, SUBMIT, PREVIEW, CONFIRM):
        for body in (top_level, inside, '{"token": ' + '{"a":' * 30_000 + '1' + '}' * 30_000 + '}'):
            response = raw(anonymous, url, body)
            assert response.status_code == 400, (url, response.status_code)
            assert response['Content-Type'] == 'application/json'
    assert not MappingVoteSubmission.objects.exists()


def test_malformed_json_and_a_json_array_are_400s(anonymous):
    for url in (PAGE, SUBMIT, PREVIEW, CONFIRM):
        assert raw(anonymous, url, '{"token": ').status_code == 400
        assert raw(anonymous, url, '["token"]').status_code == 400
        assert raw(anonymous, url, '"token"').status_code == 400
        assert raw(anonymous, url, '').status_code in (400, 404)


def test_an_oversized_body_is_refused_before_it_is_read(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    body = json.dumps({'token': share.token, 'email': VOTER, 'votes': [yes(steps)],
                       'padding': 'p' * (201 * 1024)})

    for url in (PAGE, SUBMIT, PREVIEW, CONFIRM):
        assert raw(anonymous, url, body).status_code == 413
    assert not MappingVoteSubmission.objects.exists() and not mail.outbox


# ── Sending the email ────────────────────────────────────────────────────

def test_the_request_hands_over_only_the_submissions_id(anonymous, queued):
    """No token exists yet when the request answers, so none can be in a
    task argument, a broker message or anything that reports them."""
    share = make_share()
    steps = proposal('STEPS')

    response = submit(anonymous, share, [yes(steps)])

    pending = MappingVoteSubmission.objects.get()
    assert response.status_code == 202
    assert queued.calls == [pending.pk]
    assert pending.token_hash is None and not mail.outbox
    assert MappingVoteEmail.objects.count() == 1, 'counted when queued'

    assert mapping_votes.send_confirmation(pending.pk) is True

    pending.refresh_from_db()
    assert pending.token_hash == hashlib.sha256(emailed_token().encode()).hexdigest()
    assert confirm(anonymous, emailed_token()).json() == {'recorded': 1, 'skipped': 0}


def test_with_a_broker_the_send_is_queued_on_commit(anonymous, settings, django_capture_on_commit_callbacks):
    from omop_core.tasks import send_vote_confirmation_task

    settings.CELERY_BROKER_URL = 'redis://broker.invalid:6379/0'
    share = make_share()
    steps = proposal('STEPS')
    assert isinstance(mapping_vote_jobs.dispatcher(), mapping_vote_jobs.CeleryDispatcher)

    with mock.patch.object(send_vote_confirmation_task, 'apply_async') as apply_async:
        with django_capture_on_commit_callbacks(execute=True):
            response = submit(anonymous, share, [yes(steps, 'A comment')])

    pending = MappingVoteSubmission.objects.get()
    assert response.status_code == 202 and not mail.outbox
    apply_async.assert_called_once_with(args=[pending.pk])
    settings.CELERY_BROKER_URL = ''
    assert isinstance(mapping_vote_jobs.dispatcher(), mapping_vote_jobs.InlineDispatcher)


def test_the_task_sends_the_email_and_keeps_no_result(anonymous, queued):
    from omop_core.tasks import send_vote_confirmation_task

    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])

    assert send_vote_confirmation_task(queued.calls[0]) is None

    assert len(mail.outbox) == 1 and mail.outbox[0].to == [VOTER]
    assert send_vote_confirmation_task.ignore_result is True
    assert send_vote_confirmation_task.name == 'omop_core.send_vote_confirmation'


def test_a_job_delivered_twice_mails_once(anonymous, queued):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    [submission_id] = queued.calls

    assert mapping_votes.send_confirmation(submission_id) is True
    assert mapping_votes.send_confirmation(submission_id) is False
    assert mapping_votes.send_confirmation(987_654_321) is False

    assert len(mail.outbox) == 1
    assert preview(anonymous, emailed_token()).status_code == 200, 'the first link still works'


def test_a_failed_send_answers_the_same_leaves_nothing_and_is_not_counted(anonymous, many_submissions):
    share = make_share()
    steps = proposal('STEPS')

    with mock.patch.object(mapping_votes.EmailMessage, 'send', side_effect=OSError('smtp down')):
        failed = [submit(anonymous, share, [yes(steps)]) for _ in range(6)]
    with mock.patch.object(mapping_votes.EmailMessage, 'send', return_value=0):
        failed.append(submit(anonymous, share, [yes(steps)]))

    assert {response.status_code for response in failed} == {202}
    assert failed[0].json() == {'detail': mapping_votes.SUBMITTED_DETAIL}
    assert not MappingVoteSubmission.objects.exists() and not MappingVote.objects.exists()
    assert not MappingVoteEmail.objects.exists(), 'an email that never left counts against nobody'
    # So the address still has its whole allowance.
    for _ in range(5):
        assert submit(anonymous, share, [yes(steps)]).status_code == 202
    assert len(mail.outbox) == 5


def test_a_deployed_process_without_a_mail_service_prints_no_link(anonymous, settings, capsys, caplog, queued):
    """A worker that was not given the mail settings falls back to the
    console backend, which would write the link and the address into its log
    and deliver to nobody."""
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))], email='private-address@example.test')
    settings.EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    settings.DEBUG = False
    capsys.readouterr()

    assert mapping_votes.send_confirmation(queued.calls[0]) is False

    printed = capsys.readouterr()
    assert 'vote/confirm#' not in printed.out + printed.err
    assert 'private-address' not in printed.out + printed.err + caplog.text
    assert 'has no mail backend' in caplog.text
    assert not MappingVoteSubmission.objects.exists() and not MappingVoteEmail.objects.exists()
    # On a developer machine the console is the mailbox.
    submit(anonymous, share, [yes(SourceCodeConceptMapping.objects.get())])
    settings.DEBUG = True
    assert mapping_votes.send_confirmation(queued.calls[1]) is True
    assert 'vote/confirm#' in capsys.readouterr().out


def test_the_mail_backend_gets_this_messages_own_timeout(anonymous, settings):
    from django.core.mail import get_connection

    settings.MAPPING_VOTE_EMAIL_TIMEOUT = 7
    share = make_share()
    with mock.patch.object(mapping_votes, 'get_connection', wraps=mapping_votes.get_connection) as connect:
        submit(anonymous, share, [yes(proposal('STEPS'))])

    connect.assert_called_once_with(requests_timeout=7, timeout=7)
    assert len(mail.outbox) == 1
    # Each backend reads the name it knows: anymail's HTTP call, and SMTP.
    anymail = get_connection('anymail.backends.mailgun.EmailBackend', requests_timeout=7, timeout=7,
                             api_key='test-key', sender_domain='mail.example.test')
    smtp = get_connection('django.core.mail.backends.smtp.EmailBackend', requests_timeout=7, timeout=7)
    assert anymail.timeout == 7 and smtp.timeout == 7
    assert settings.MAPPING_VOTE_EMAIL_TIMEOUT < 30


# ── Confirming ───────────────────────────────────────────────────────────

def test_the_preview_shows_the_votes_and_confirms_nothing(anonymous):
    share = make_share(title='HL7 review')
    steps = proposal('STEPS', loinc('55423-8', 'Number of steps'))
    sleep = proposal('SLEEP', category='Sleep')
    submit(anonymous, share, [no(sleep), yes(steps, 'Agree')])
    token = emailed_token()

    for _ in range(3):
        response = preview(anonymous, token)

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'title', 'email', 'expires_at', 'votes'}
    assert data['title'] == 'HL7 review' and data['email'] == VOTER
    assert [(entry['proposal']['source_code'], entry['choice'], entry['comment']) for entry in data['votes']] == [
        ('STEPS', 'yes', 'Agree'), ('SLEEP', 'no', '')]
    assert data['votes'][0]['proposal']['destination_code'] == '55423-8'
    assert not MappingVote.objects.exists()
    assert MappingVoteSubmission.objects.count() == 1


@pytest.mark.parametrize('method', ['get', 'head', 'put', 'patch', 'delete', 'options'])
def test_nothing_but_the_confirm_post_records_a_vote(anonymous, method):
    """Whatever follows the emailed link: the page itself is served without
    the token (it is the fragment), and no other verb on any route records."""
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    token = emailed_token()

    for url in PUBLIC_PATHS:
        response = getattr(anonymous, method)(url, {'token': token}, format='json')
        assert response.status_code in (200, 405), (method, url)
    preview(anonymous, token)
    page(anonymous, share)

    assert not MappingVote.objects.exists()
    assert MappingVoteSubmission.objects.count() == 1


def test_confirming_records_the_votes_and_spends_the_token(anonymous):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    submit(anonymous, share, [yes(steps, 'Agree'), {'mapping': sleep.pk, 'comment': 'Check the unit'}])
    submitted_at = MappingVoteSubmission.objects.get().created_at
    token = emailed_token()

    response = confirm(anonymous, token)

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
    again, summary = confirm(anonymous, token), preview(anonymous, token)
    assert again.status_code == summary.status_code == 404
    assert again.json() == summary.json() == LINK_INVALID
    assert MappingVote.objects.count() == 2
    assert MappingVoteEmail.objects.count() == 1, 'the email still counts once its submission is gone'


def test_used_expired_and_unknown_confirmation_links_are_one_answer(anonymous, queued):
    share = make_share()
    steps = proposal('STEPS')
    submit(anonymous, share, [yes(steps)])
    mapping_votes.send_confirmation(queued.calls[-1])
    used = emailed_token()
    confirm(anonymous, used)
    submit(anonymous, share, [no(steps)], email='slow@example.test')
    mapping_votes.send_confirmation(queued.calls[-1])
    expired = emailed_token()
    MappingVoteSubmission.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
    # Queued and not sent yet: it has no token, so no token can name it.
    submit(anonymous, share, [no(steps)], email='queued@example.test')

    answers = [call(anonymous, token) for token in (used, expired, WELL_FORMED, 'short', 'x' * 400, '', None)
               for call in (preview, confirm)]
    answers += [anonymous.post(PREVIEW, {}, format='json'), anonymous.post(CONFIRM, {}, format='json')]

    assert {response.status_code for response in answers} == {404}
    assert len({response.content for response in answers}) == 1
    assert MappingVote.objects.get().choice == 'yes'


def test_a_link_cannot_be_confirmed_after_its_share_expires(anonymous):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    MappingVoteShare.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))

    assert preview(anonymous, emailed_token()).status_code == 404
    assert confirm(anonymous, emailed_token()).status_code == 404
    assert not MappingVote.objects.exists()


def test_a_later_confirmed_submission_replaces_only_the_votes_it_names(anonymous, admin, many_submissions):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    cast(anonymous, share, [yes(steps, 'First thought'), yes(sleep)])
    cast(anonymous, share, [no(steps)], email='someone-else@example.test')

    cast(anonymous, share, [no(steps, 'Changed my mind')])

    mine = {vote.mapping_id: vote for vote in MappingVote.objects.filter(voter_email=VOTER)}
    assert len(mine) == 2 and MappingVote.objects.count() == 3
    assert (mine[steps.pk].choice, mine[steps.pk].comment) == ('no', 'Changed my mind')
    assert (mine[sleep.pk].choice, mine[sleep.pk].comment) == ('yes', '')
    assert admin.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()['tally'] == {
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


def test_proposals_that_closed_before_confirmation_are_skipped_and_reported(anonymous):
    share = make_share()
    codes = ('KEPT', 'APPROVED', 'REJECTED', 'DELETED', 'MOVED', 'CLEARED', 'UNDESCRIBED')
    kept, approved, rejected, deleted, moved, cleared, undescribed = (proposal(code) for code in codes)
    submit(anonymous, share, [yes(mapping) for mapping in (
        kept, approved, rejected, deleted, moved, cleared, undescribed)])
    SourceCodeConceptMapping.objects.filter(pk=approved.pk).update(status='approved')
    SourceCodeConceptMapping.objects.filter(pk=rejected.pk).update(status='rejected')
    deleted.delete()
    SourceCodeConceptMapping.objects.filter(pk=moved.pk).update(target_concept=loinc('NEW-1', 'Another concept'))
    SourceCodeConceptMapping.objects.filter(pk=cleared.pk).update(target_concept=None)
    SourceCodeConceptMapping.objects.filter(pk=undescribed.pk).update(metric_key='')
    token = emailed_token()

    summary = preview(anonymous, token).json()
    response = confirm(anonymous, token)

    assert [entry['proposal']['source_code'] if entry['proposal'] else None for entry in summary['votes']] == [
        'KEPT', None, None, None, None, None, None]
    assert response.json() == {'recorded': 1, 'skipped': 6}
    assert list(MappingVote.objects.values_list('mapping_id', flat=True)) == [kept.pk]
    assert not MappingVoteSubmission.objects.exists()


def test_a_submission_whose_proposals_all_closed_still_spends_the_token(anonymous):
    share = make_share()
    steps = proposal('STEPS')
    submit(anonymous, share, [yes(steps)])
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(status='approved')
    token = emailed_token()

    assert confirm(anonymous, token).json() == {'recorded': 0, 'skipped': 1}
    assert confirm(anonymous, token).status_code == 404
    assert not MappingVote.objects.exists()


def test_confirming_costs_the_same_queries_for_three_votes_or_forty(anonymous, many_submissions):
    """It was a query or six per vote: 2,050 for 340."""
    share = make_share()
    proposals = [proposal(f'P{index:02d}', snomed(f'S-{index}')) for index in range(40)]

    def queries(count, email):
        submit(anonymous, share, [yes(mapping, 'ok') for mapping in proposals[:count]], email=email)
        token = emailed_token()
        AuditEvent.objects.all().delete()
        with CaptureQueriesContext(connection) as captured:
            assert confirm(anonymous, token).json() == {'recorded': count, 'skipped': 0}
        return len(captured)

    few = queries(3, 'few@example.test')
    many = queries(40, 'many@example.test')
    # And the same again when every row already exists and is replaced.
    replaced = queries(40, 'many@example.test')

    assert few == many == replaced
    assert MappingVote.objects.count() == 43


@pytest.mark.django_db(transaction=True)
def test_one_token_confirmed_twice_at_once_records_once(queued):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    mapping_votes.submit(share, VOTER, [yes(steps), no(sleep)])
    mapping_votes.send_confirmation(queued.calls[0])
    token = emailed_token()

    results = in_threads([lambda: mapping_votes.confirm(token)] * 6)

    assert sorted(results, key=lambda result: result is None) == [{'recorded': 2, 'skipped': 0}] + [None] * 5
    assert MappingVote.objects.count() == 2 and not MappingVoteSubmission.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_two_submissions_naming_the_same_proposals_confirm_at_once_without_deadlock(queued):
    """Opposite orders, same voter, same moment: each used to hold a vote row
    the other wanted, five rounds out of five."""
    share = make_share()
    first, second = proposal('FIRST'), proposal('SECOND')
    for _ in range(5):
        mail.outbox.clear()
        mapping_votes.submit(share, VOTER, [yes(first), yes(second)])
        mapping_votes.submit(share, VOTER, [no(second), no(first)])
        for submission_id in queued.calls[-2:]:
            mapping_votes.send_confirmation(submission_id)
        tokens = [emailed_token(0), emailed_token(1)]

        results = in_threads([lambda token=token: mapping_votes.confirm(token) for token in tokens])

        assert results == [{'recorded': 2, 'skipped': 0}] * 2, results
        # One of them was last, on both proposals.
        assert len(set(MappingVote.objects.values_list('choice', flat=True))) == 1
        assert MappingVote.objects.count() == 2
        MappingVoteEmail.objects.all().delete()


# ── Limits ───────────────────────────────────────────────────────────────

def throttled(scope, rate='2/minute'):
    return mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {scope: rate})


def test_the_default_limits(settings):
    rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']

    assert rates['mapping_vote_read'] == '60/minute'
    # A working group voting from one meeting room is one address.
    assert rates['mapping_vote_submit'] == '30/hour'
    assert rates['mapping_vote_confirm'] == '60/hour'
    assert (settings.MAPPING_VOTE_MAX_PENDING_PER_EMAIL, settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY) == (3, 5)
    assert settings.MAPPING_VOTE_MAX_EMAILS_PER_SHARE_PER_DAY == 200
    assert settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL == 500
    assert settings.MAPPING_VOTE_PROXY_HOPS == 0
    assert 'NUM_PROXIES' not in settings.REST_FRAMEWORK, 'every other throttle is left as it was'


def test_reading_the_page_is_throttled_by_client_address(anonymous):
    share = make_share()
    proposal('STEPS')

    with throttled('mapping_vote_read'):
        statuses = [page(anonymous, share).status_code for _ in range(3)]
        elsewhere = page(anonymous, share, REMOTE_ADDR='203.0.113.9')
        unknown = page(anonymous, WELL_FORMED)

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
        read = page(anonymous, share)

    assert statuses == [202, 202, 429]
    assert len(mail.outbox) == 2 and MappingVoteSubmission.objects.count() == 2
    assert refused.status_code == 400, 'another address has its own bucket'
    assert read.status_code == 200


def test_the_confirmation_routes_share_a_throttle_apart_from_the_rest(anonymous):
    share = make_share()
    submit(anonymous, share, [yes(proposal('STEPS'))])
    token = emailed_token()

    with throttled('mapping_vote_confirm'):
        statuses = [preview(anonymous, token).status_code, preview(anonymous, WELL_FORMED).status_code,
                    confirm(anonymous, token).status_code]
        read = page(anonymous, share)

    assert statuses == [200, 404, 429]
    assert not MappingVote.objects.exists()
    assert read.status_code == 200


def test_the_public_throttles_ignore_any_session(anonymous):
    """Keyed on the address even for a signed-in browser: the views read no
    session, so a visitor cannot buy a fresh bucket by signing in."""
    share = make_share()
    proposal('STEPS')
    signed_in = APIClient()
    signed_in.force_login(with_role('analyst', 'signed-in'))

    with throttled('mapping_vote_read'):
        statuses = [page(client, share).status_code for client in (anonymous, signed_in, signed_in)]

    assert statuses == [200, 200, 429]


@pytest.mark.parametrize(('hops', 'forwarded', 'expected'), [
    # No proxy declared: the socket's peer, whatever the header claims.
    (0, None, '198.51.100.7'), (0, '6.6.6.6', '198.51.100.7'), (0, '6.6.6.6, 7.7.7.7', '198.51.100.7'),
    # One trusted proxy: the entry it appended, the last.
    (1, '203.0.113.5', '203.0.113.5'), (1, '6.6.6.6, 203.0.113.5', '203.0.113.5'),
    (1, ' 6.6.6.6 ,7.7.7.7,  203.0.113.5 ', '203.0.113.5'),
    # Two: the entry the outer one appended, second from the right.
    (2, '6.6.6.6, 203.0.113.5, 10.0.0.2', '203.0.113.5'), (2, '203.0.113.5, 10.0.0.2', '203.0.113.5'),
    # Fewer entries than proxies: not the header the deployment described.
    (1, None, '198.51.100.7'), (1, '', '198.51.100.7'), (1, ' , ', '198.51.100.7'),
    (2, '203.0.113.5', '198.51.100.7'), (3, '6.6.6.6, 203.0.113.5', '198.51.100.7'),
])
def test_the_client_address_is_read_where_the_deployment_says(rf, settings, hops, forwarded, expected):
    settings.MAPPING_VOTE_PROXY_HOPS = hops
    extra = {} if forwarded is None else {'HTTP_X_FORWARDED_FOR': forwarded}
    request = rf.post(PAGE, REMOTE_ADDR='198.51.100.7', **extra)

    assert vote_api.client_address(request) == expected


@pytest.mark.parametrize(('hops', 'real_tail'), [
    (0, None), (1, '203.0.113.5'), (2, '203.0.113.5, 10.0.0.2'),
])
def test_a_forged_forwarded_header_does_not_buy_a_fresh_bucket(anonymous, settings, hops, real_tail):
    """With DRF's own identity each made-up header was a new client: 520
    requests sent 500 emails. The part of the header the client writes is to
    the left of what the trusted proxies append, and is never read."""
    settings.MAPPING_VOTE_PROXY_HOPS = hops
    share = make_share()
    proposal('STEPS')

    def forged(index):
        made_up = f'10.9.{index}.{index}, 172.16.{index}.1'
        return made_up if real_tail is None else f'{made_up}, {real_tail}'

    with throttled('mapping_vote_read'):
        statuses = [page(anonymous, share, HTTP_X_FORWARDED_FOR=forged(index)).status_code for index in range(5)]

    assert statuses == [200, 200, 429, 429, 429]


def test_with_hops_set_two_clients_behind_the_proxy_have_their_own_buckets(anonymous, settings):
    settings.MAPPING_VOTE_PROXY_HOPS = 1
    share = make_share()
    proposal('STEPS')

    with throttled('mapping_vote_read'):
        one = [page(anonymous, share, HTTP_X_FORWARDED_FOR='203.0.113.5').status_code for _ in range(3)]
        other = page(anonymous, share, HTTP_X_FORWARDED_FOR='203.0.113.6')

    assert one == [200, 200, 429] and other.status_code == 200, 'the proxy address is not everyone'


def test_at_the_pending_limit_the_oldest_submission_gives_way(anonymous, many_submissions):
    """Refusing the new one would let anybody who knows a voter's address
    lock that voter out for two days with three junk submissions."""
    share, other_share = make_share(), make_share(title='Other')
    steps = proposal('STEPS')

    answers = [submit(anonymous, share, [yes(steps, f'attempt {index}')]) for index in range(4)]

    assert [response.status_code for response in answers] == [202] * 4
    assert len({response.content for response in answers}) == 1
    assert len(mail.outbox) == 4
    assert [entry.payload[0]['comment'] for entry in MappingVoteSubmission.objects.order_by('id')] == [
        'attempt 1', 'attempt 2', 'attempt 3']
    assert preview(anonymous, emailed_token(0)).status_code == 404, 'the first link is gone'
    assert confirm(anonymous, emailed_token(3)).json() == {'recorded': 1, 'skipped': 0}
    assert MappingVote.objects.get().comment == 'attempt 3'
    # The limit is per share and per address; the daily email limit is not.
    assert submit(anonymous, other_share, [yes(steps)]).status_code == 202
    assert len(mail.outbox) == 5
    assert submit(anonymous, share, [no(steps)]).status_code == 202
    assert len(mail.outbox) == 5, 'the sixth email of the day is not sent'
    assert submit(anonymous, share, [yes(steps)], email='another@example.test').status_code == 202
    assert len(mail.outbox) == 6


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


def test_plus_addresses_of_one_inbox_share_one_allowance(anonymous, many_submissions):
    """Forty plus-addresses put forty mails in one inbox. They are counted as
    the address they deliver to, and vote as the address that was typed."""
    share = make_share()
    steps = proposal('STEPS')

    answers = [submit(anonymous, share, [yes(steps)], email=f'voter+tag{index}@example.test')
               for index in range(8)]
    plain = submit(anonymous, share, [yes(steps)], email=VOTER)
    cased = submit(anonymous, share, [yes(steps)], email='Voter+Other@Example.Test')

    assert {response.status_code for response in answers + [plain, cased]} == {202}
    assert len(mail.outbox) == 5, 'the daily limit is the inbox owner\'s'
    assert [message.to for message in mail.outbox] == [[f'voter+tag{index}@example.test'] for index in range(5)]
    assert MappingVoteSubmission.objects.count() == 3, 'and so is the pending limit'
    assert MappingVoteEmail.objects.values('email_key').distinct().count() == 1
    assert confirm(anonymous, emailed_token()).status_code == 200
    assert MappingVote.objects.get().voter_email == 'voter+tag4@example.test'
    assert mapping_votes.canonical_email('a+b+c@example.test') == 'a@example.test'
    assert mapping_votes.canonical_email('+tag@example.test') == '+tag@example.test'
    assert submit(anonymous, share, [yes(steps)], email='voter2@example.test').status_code == 202
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


def test_one_share_sends_a_bounded_number_of_emails_a_day(anonymous, many_submissions, settings):
    """So one leaked link cannot use up what the others need."""
    settings.MAPPING_VOTE_MAX_EMAILS_PER_SHARE_PER_DAY = 3
    leaked, other = make_share(title='Leaked'), make_share(title='Other')
    steps = proposal('STEPS')

    answers = [submit(anonymous, leaked, [yes(steps)], email=f'fresh-{index}@example.test',
                      REMOTE_ADDR=f'203.0.113.{index}') for index in range(5)]
    elsewhere = submit(anonymous, other, [yes(steps)], email='fresh-0@example.test')

    assert [response.status_code for response in answers] == [202, 202, 202, 503, 503]
    assert answers[-1].json() == UNAVAILABLE
    assert elsewhere.status_code == 202
    assert len(mail.outbox) == 4 and MappingVoteSubmission.objects.filter(share=leaked).count() == 3


def test_the_deployment_sends_a_bounded_number_of_emails_a_day(anonymous, many_submissions, settings):
    """Fresh addresses from fresh client addresses pass every other limit.
    Past it voters are told so, not told to wait for mail that will not come."""
    settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL = 4
    shares = [make_share(title=f'Share {index}') for index in range(6)]
    steps = proposal('STEPS')

    answers = [submit(anonymous, share, [yes(steps)], email=f'fresh-{index}@example.test',
                      REMOTE_ADDR=f'203.0.113.{index}') for index, share in enumerate(shares)]

    assert [response.status_code for response in answers] == [202] * 4 + [503] * 2
    assert answers[-1].json() == UNAVAILABLE and answers[-1]['Content-Type'] == 'application/json'
    assert len(mail.outbox) == 4 and MappingVoteSubmission.objects.count() == 4
    # The read and the confirmation of what was already sent still work.
    assert page(anonymous, shares[0]).status_code == 200
    assert confirm(anonymous, emailed_token()).status_code == 200


def test_an_address_over_its_own_limit_gets_what_everyone_gets_at_a_shared_limit(
        anonymous, many_submissions, settings):
    """Otherwise the 202 among the 503s would say: this address has been used
    five times today."""
    share = make_share()
    steps = proposal('STEPS')
    for _ in range(5):
        submit(anonymous, share, [yes(steps)])
    settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL = 5

    over_its_own = submit(anonymous, share, [yes(steps)])
    fresh = submit(anonymous, share, [yes(steps)], email='fresh@example.test')

    assert over_its_own.status_code == fresh.status_code == 503
    assert over_its_own.content == fresh.content


@pytest.mark.django_db(transaction=True)
def test_the_shared_limits_hold_under_simultaneous_submissions(settings, queued):
    """Counted and written under one lock: twelve at once cannot each read
    'one below the limit'."""
    settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL = 4
    steps = proposal('STEPS')
    shares = [make_share(title=f'Share {index}') for index in range(12)]

    results = in_threads([
        lambda share=share, index=index: mapping_votes.submit(share, f'fresh-{index}@example.test', [yes(steps)])
        for index, share in enumerate(shares)])

    assert sorted(type(result).__name__ for result in results) == ['Unavailable'] * 8 + ['bool'] * 4
    assert MappingVoteEmail.objects.count() == 4 and len(queued.calls) == 4

    settings.MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL = 500
    settings.MAPPING_VOTE_MAX_EMAILS_PER_SHARE_PER_DAY = 3
    MappingVoteEmail.objects.all().delete()
    MappingVoteSubmission.objects.all().delete()
    results = in_threads([
        lambda index=index: mapping_votes.submit(shares[0], f'fresh-{index}@example.test', [yes(steps)])
        for index in range(10)])

    assert sorted(type(result).__name__ for result in results) == ['Unavailable'] * 7 + ['bool'] * 3
    assert MappingVoteEmail.objects.filter(share=shares[0]).count() == 3


@pytest.mark.django_db(transaction=True)
def test_the_address_limits_hold_under_simultaneous_submissions(queued):
    steps = proposal('STEPS')
    shares = [make_share(title=f'Share {index}') for index in range(9)]

    results = in_threads([
        lambda share=share: mapping_votes.submit(share, VOTER, [yes(steps)]) for share in shares])

    assert sorted(results) == [False] * 4 + [True] * 5
    assert MappingVoteEmail.objects.count() == 5


def test_the_limits_hold_without_the_cache(anonymous, many_submissions):
    """They are counted in the database, so clearing the throttle cache (or
    running on a worker with its own) changes nothing."""
    share = make_share()
    steps = proposal('STEPS')
    for _ in range(7):
        submit(anonymous, share, [yes(steps)])
        cache.clear()

    assert len(mail.outbox) == 5


def test_expired_submissions_are_purged_by_a_submit_a_page_read_and_the_command(anonymous, capsys, many_submissions):
    share = make_share()
    steps = proposal('STEPS')

    def expire():
        MappingVoteSubmission.objects.update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
        MappingVoteEmail.objects.update(sent_at=timezone.now() - datetime.timedelta(hours=49))

    for _ in range(3):
        submit(anonymous, share, [yes(steps)])
    expire()
    assert submit(anonymous, share, [no(steps)]).status_code == 202
    assert [entry.payload[0]['choice'] for entry in MappingVoteSubmission.objects.all()] == ['no']

    # Nothing schedules the command, so a read of any page does it too.
    expire()
    assert page(anonymous, share).status_code == 200
    assert not MappingVoteSubmission.objects.exists() and not MappingVoteEmail.objects.exists()

    cast(anonymous, share, [yes(steps)])
    submit(anonymous, share, [no(steps)])
    expire()
    call_command('purge_expired_vote_submissions')

    assert 'Deleted 1 expired' in capsys.readouterr().out
    assert not MappingVoteSubmission.objects.exists()
    assert MappingVote.objects.count() == 1, 'a confirmed vote is never purged'


# ── What curators see ────────────────────────────────────────────────────

def test_an_administrator_sees_who_voted_and_a_reader_sees_only_what_was_said(admin, analyst, doctor, anonymous):
    share = make_share(title='HL7 review')
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps, 'Agree')])
    cast(anonymous, share, [no(steps)], email='second@example.test')
    url = f'/api/v1/code-mappings/{steps.pk}/votes/'

    for_admin = admin.get(url)

    assert for_admin.status_code == 200
    data = for_admin.json()
    assert data['tally'] == {'yes': 1, 'no': 1, 'comments': 1} and data['emails_included'] is True
    assert [(vote['voter_email'], vote['choice'], vote['comment'], vote['share_title'], vote['stale'])
            for vote in data['votes']] == [
        ('second@example.test', 'no', '', 'HL7 review', False), (VOTER, 'yes', 'Agree', 'HL7 review', False)]
    assert for_admin['Cache-Control'] == 'no-store'
    for reader in (analyst, doctor):
        response = reader.get(url)
        assert response.status_code == 200
        assert response.json()['tally'] == data['tally'] and response.json()['emails_included'] is False
        assert [set(vote) for vote in response.json()['votes']] == [
            {'id', 'choice', 'comment', 'confirmed_at', 'share_id', 'share_title', 'stale'}] * 2
        assert '@' not in response.content.decode()
    assert admin.get('/api/v1/code-mappings/999999999/votes/').status_code == 404


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


def test_votes_on_an_earlier_destination_go_stale(admin, anonymous):
    share = make_share()
    steps = proposal('STEPS')
    cast(anonymous, share, [yes(steps, 'Right concept')])
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(target_concept=loinc('NEW-1', 'Another concept'))
    cast(anonymous, share, [no(steps)], email='later@example.test')

    data = admin.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()
    [row] = admin.get('/api/v1/code-mappings/', {'browse': '1', 'source': 'OpenWearables'}).json()['results']

    assert data['tally'] == row['votes'] == {'yes': 0, 'no': 1, 'comments': 0}
    assert [(vote['voter_email'], vote['stale']) for vote in data['votes']] == [
        ('later@example.test', False), (VOTER, True)]
    assert MappingVote.objects.count() == 2, 'the stale vote is kept'


def test_a_stale_vote_counts_again_if_the_destination_returns(admin, anonymous):
    share = make_share()
    steps = proposal('STEPS')
    original = steps.target_concept_id
    cast(anonymous, share, [yes(steps)])
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(target_concept=loinc('NEW-1', 'Another'))
    SourceCodeConceptMapping.objects.filter(pk=steps.pk).update(target_concept_id=original)

    assert admin.get(f'/api/v1/code-mappings/{steps.pk}/votes/').json()['tally']['yes'] == 1


def test_the_list_carries_each_rows_current_tally(admin, analyst, doctor, anonymous, many_submissions):
    share, other = make_share(), make_share(title='Other')
    steps, sleep, quiet = proposal('STEPS'), proposal('SLEEP'), proposal('QUIET')
    cast(anonymous, share, [yes(steps, 'Agree'), no(sleep)])
    cast(anonymous, share, [yes(steps), {'mapping': sleep.pk, 'comment': 'Unit?'}], email='second@example.test')
    cast(anonymous, other, [no(steps, 'Disagree')])
    submit(anonymous, share, [yes(quiet)], email='pending@example.test')

    for client in (admin, analyst, doctor):
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


def test_a_single_row_response_carries_no_tally(analyst):
    steps = proposal('STEPS')

    assert 'votes' not in analyst.get(f'/api/v1/code-mappings/{steps.pk}/').json()


def test_deleting_a_mapping_deletes_its_votes(anonymous):
    share = make_share()
    steps, sleep = proposal('STEPS'), proposal('SLEEP')
    cast(anonymous, share, [yes(steps), yes(sleep)])

    steps.delete()

    assert list(MappingVote.objects.values_list('mapping_id', flat=True)) == [sleep.pk]


def test_the_votes_csv_is_for_administrators_and_quotes_what_strangers_typed(admin, anonymous, many_submissions):
    share, other = make_share(title='HL7 review'), make_share(title='Other')
    steps = proposal('HKSteps', loinc('55423-8', 'Number of steps'), metric_key='steps',
                     source_code_description='Step count')
    sleep = proposal('SLEEP', category='Sleep', metric_key='sleep')
    cast(anonymous, share, [yes(sleep), no(steps, '=HYPERLINK("http://evil.example","click")')])
    cast(anonymous, share, [{'mapping': steps.pk, 'comment': '+1 from me\nsecond line'}], email='b@example.test')
    cast(anonymous, other, [yes(steps)], email='elsewhere@example.test')
    SourceCodeConceptMapping.objects.filter(pk=sleep.pk).update(target_concept=loinc('NEW-1', 'Another concept'))

    response = admin.get(f'{SHARES}{share.pk}/votes-export/')

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
    assert rows[2]['Voted on destination'] == 'LOINC:L-SLEEP', 'the destination the voter saw'
    assert 'elsewhere@example.test' not in response.content.decode()
    assert admin.get(f'{SHARES}999999/votes-export/').status_code == 404


def test_an_empty_share_exports_a_header_only(admin):
    share = make_share()

    lines = admin.get(f'{SHARES}{share.pk}/votes-export/').content.decode('utf-8-sig').splitlines()

    assert lines == [','.join(mapping_votes.CSV_COLUMNS)]


# ── The public download ──────────────────────────────────────────────────

def download(client, share, **body):
    token = share.token if isinstance(share, MappingVoteShare) else share
    return client.post(EXPORT, {'token': token, **body}, format='json')


def csv_lines(response):
    import csv
    import io
    lines = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    return lines[0], [dict(zip(lines[0], line)) for line in lines[1:]]


def test_a_share_holder_downloads_the_mappings_without_provenance_or_source_units(anonymous):
    share = make_share()
    org = Organization.objects.create(slug='secret-hospital', name='Secret Hospital')
    proposal('STEPS', notes='CURATOR-NOTE-TEXT', origin_system='suggest-secret',
             source_unit_evidence=[{'unit': 'EVIDENCE-UNIT', 'source': 'patient-data'}])
    proposal('HEART', status='approved')
    proposal('GONE', status='rejected')
    proposal('SCOPED', organization=org)

    response = download(anonymous, share)

    assert response.status_code == 200
    assert response['Content-Type'].startswith('text/csv')
    assert 'attachment; filename="wearable-mappings-' in response['Content-Disposition']
    assert response['Cache-Control'] == 'no-store' and response['Referrer-Policy'] == 'no-referrer'
    assert not response.cookies
    header, rows = csv_lines(response)
    # The proposals and the approved mappings: what the group is asked about, and what is settled.
    assert sorted(row['Source code'] for row in rows) == ['HEART', 'STEPS']
    assert {row['Provenance'] for row in rows} == {''} and {row['Source unit'] for row in rows} == {''}
    text = response.content.decode()
    for secret in ('CURATOR-NOTE-TEXT', 'suggest-secret', 'EVIDENCE-UNIT', 'Secret Hospital', 'SCOPED', 'GONE'):
        assert secret not in text


def test_the_public_download_offers_word_and_the_cdisc_bc_target(anonymous):
    share = make_share()
    heart = loinc('8867-4', 'Heart rate')
    proposal('HEART', concept=heart)
    proposal('C49677', concept=heart, source_vocabulary_id='CDISC-BC', source_code_description='Heart Rate')

    word = download(anonymous, share, file_format='docx')
    bc = download(anonymous, share, destination='cdisc-bc')

    assert word.status_code == 200 and word.content[:2] == b'PK'
    assert 'wordprocessingml' in word['Content-Type']
    assert 'wearable-cdisc-bc-mappings-' in bc['Content-Disposition']
    _, rows = csv_lines(bc)
    assert [(row['Source code'], row['CDISC BC ID']) for row in rows] == [('HEART', 'C49677')]


@pytest.mark.parametrize('body', [
    {'file_format': 'pdf'}, {'destination': 'nope'}, {'file_format': ['csv']}, {'destination': 7}])
def test_the_public_download_refuses_what_it_does_not_write(anonymous, body):
    response = download(anonymous, make_share(), **body)
    assert response.status_code == 400 and response['Content-Type'] == 'application/json'


def test_the_public_download_needs_a_live_link(anonymous):
    revoked = make_share()
    mapping_votes.revoke_share(revoked)
    for token in (revoked, WELL_FORMED, ''):
        response = download(anonymous, token)
        assert (response.status_code, response.json()) == (404, NOT_AVAILABLE)
    assert anonymous.get(EXPORT).status_code == 405


def test_the_public_download_is_throttled_per_client(anonymous):
    share = make_share()
    with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'mapping_vote_export': '2/minute'}):
        codes = [download(anonymous, share).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ── A share limited to some source code systems ──────────────────────────

def test_a_share_with_no_systems_puts_every_proposal_to_the_vote(anonymous):
    proposal('STEP', source_vocabulary_id='Apple')
    proposal('STEPS', source_vocabulary_id='Garmin')
    share = make_share()
    assert share.source_vocabularies == []
    assert sorted(listed_codes(anonymous, share)) == ['STEP', 'STEPS']


def test_a_limited_share_lists_and_exports_only_its_systems(anonymous):
    proposal('STEP', source_vocabulary_id='Apple')
    proposal('STEPS', source_vocabulary_id='Garmin')
    share = make_share(sources=['Garmin'])
    payload = page(anonymous, share).json()
    assert listed_codes(anonymous, share) == ['STEPS']
    assert payload['source_systems'] == ['Garmin']
    response = anonymous.post(EXPORT, {'token': share.token, 'file_format': 'csv'}, format='json')
    body = response.content.decode('utf-8-sig')
    assert 'STEPS' in body and 'STEP,' not in body.replace('STEPS', '')


def test_a_voter_cannot_vote_outside_the_shares_systems(anonymous):
    inside = proposal('STEPS', source_vocabulary_id='Garmin')
    outside = proposal('STEP', source_vocabulary_id='Apple')
    share = make_share(sources=['Garmin'])
    assert submit(anonymous, share, [yes(outside)]).status_code == 400
    assert submit(anonymous, share, [yes(inside)]).status_code == 202


def test_only_an_administrator_names_the_systems_and_an_unknown_one_is_a_400(admin):
    made = admin.post(SHARES, {'title': 'Garmin only', 'source_vocabularies': ['Garmin', 'Apple']}, format='json')
    assert made.status_code == 201, made.content
    assert made.json()['source_vocabularies'] == ['Apple', 'Garmin']
    listed = admin.get(SHARES).json()['results']
    assert listed[0]['source_vocabularies'] == ['Apple', 'Garmin']
    refused = admin.post(SHARES, {'title': 'Bad', 'source_vocabularies': ['Nope']}, format='json')
    assert refused.status_code == 400
    assert admin.post(SHARES, {'title': 'Bad', 'source_vocabularies': 'Apple'}, format='json').status_code == 201


def test_a_page_names_its_systems_only_when_limited(anonymous):
    proposal('STEPS', source_vocabulary_id='Garmin')
    assert page(anonymous, make_share()).json()['source_systems'] == []
    assert page(anonymous, make_share(sources=['Garmin', 'Apple'])).json()['source_systems'] == ['HealthKit', 'Garmin']


def test_an_out_of_scope_pending_vote_is_dropped_at_preview_and_confirmation(anonymous):
    inside = proposal('STEPS', source_vocabulary_id='Garmin')
    outside = proposal('STEP', source_vocabulary_id='Apple')
    share = make_share(sources=['Garmin'])
    # A submission that names a proposal outside the scope, as one stored before the
    # share was limited (or made some other way) would.
    mail.outbox.clear()
    assert submit(anonymous, share, [yes(inside)]).status_code == 202
    token = emailed_token()
    submission = MappingVoteSubmission.objects.get()
    submission.payload = submission.payload + [{
        'mapping': outside.pk, 'concept': outside.target_concept_id, 'choice': 'yes', 'comment': ''}]
    submission.save()
    items = preview(anonymous, token).json()['votes']
    assert [item['proposal'] is not None for item in items] == [True, False]
    result = confirm(anonymous, token).json()
    assert result['recorded'] == 1 and result['skipped'] == 1
    assert list(MappingVote.objects.values_list('mapping_id', flat=True)) == [inside.pk]


def test_the_public_export_of_a_limited_share_honours_the_cdisc_target_and_its_refusal(anonymous):
    concept = loinc('55423-8', 'Number of steps')
    proposal('STEPS', concept, source_vocabulary_id='Garmin')
    proposal('BC1', concept, source_vocabulary_id='CDISC-BC')
    share = make_share(sources=['Garmin'])
    ok = anonymous.post(EXPORT, {'token': share.token, 'destination': 'cdisc-bc'}, format='json')
    assert ok.status_code == 200
    assert 'STEPS' in ok.content.decode('utf-8-sig')
    bc_only = make_share(sources=['CDISC-BC'])
    refused = anonymous.post(EXPORT, {'token': bc_only.token, 'destination': 'cdisc-bc'}, format='json')
    assert refused.status_code == 400 and 'no codes of its own' in refused.json()['detail']


def test_a_share_whose_stored_systems_have_all_left_the_tab_exports_nothing_rather_than_everything(anonymous):
    proposal('STEPS', source_vocabulary_id='Garmin')
    share = make_share()
    MappingVoteShare.objects.filter(pk=share.pk).update(source_vocabularies=['Gone'])
    response = anonymous.post(EXPORT, {'token': share.token}, format='json')
    assert response.status_code == 400
    assert 'no longer available' in response.json()['detail']
    # A stale id beside a live one is dropped; the live one still limits the file.
    MappingVoteShare.objects.filter(pk=share.pk).update(source_vocabularies=['Gone', 'Garmin'])
    assert anonymous.post(EXPORT, {'token': share.token}, format='json').status_code == 200


def test_the_systems_of_a_share_must_be_a_list_of_ids(admin):
    for bad in ({'a': 1}, [1], [['Apple']], 5):
        response = admin.post(SHARES, {'title': 'Bad', 'source_vocabularies': bad}, format='json')
        assert response.status_code == 400, bad
