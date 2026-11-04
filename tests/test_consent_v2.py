"""Consent model v2: versioned texts, an append-only history, a ledger that
outlives the account, document acceptance, and /api/v1/phr/consent/."""
import json
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from patient_portal import consent as svc
from patient_portal.models import (
    ConsentEvent, ConsentLedgerEntry, ConsentPurpose, ConsentText, DocumentAcceptance,
    LegalDocument, LegalDocumentVersion, PatientUser,
)
from tests.factories import PatientRecordFactory
from tests.test_phr_read_model import signed_in

pytestmark = pytest.mark.django_db

NOW = timezone.now()
EARLIER = NOW - timedelta(days=30)


def purpose(key, **kw):
    return ConsentPurpose.objects.create(key=key, **kw)


def text(purpose, version, language='en', published_at=EARLIER, **kw):
    return ConsentText.objects.create(
        purpose=purpose, version=version, language=language, title=kw.pop('title', f'{key_of(purpose)} {version}'),
        body=kw.pop('body', f'body {version} {language}'), published_at=published_at, **kw,
    )


def key_of(obj):
    return obj.key


def document(key, version='1.0', language='en', published_at=EARLIER, **kw):
    doc, _ = LegalDocument.objects.get_or_create(key=key, defaults={'name': key.title()})
    LegalDocumentVersion.objects.create(
        document=doc, version=version, language=language, title=f'{key} {version}', body='...',
        published_at=published_at, **kw,
    )
    return doc


@pytest.fixture
def member():
    record = PatientRecordFactory(country='GB')
    client = signed_in(record)
    return {'record': record, 'person': record.person, 'client': client}


@pytest.fixture
def purposes():
    my_record = purpose('my_record', kind='permission', stage='first_profile', sort_order=1)
    text(my_record, '1.0')
    text(my_record, '1.0', language='es')
    research = purpose('research', answers=ConsentPurpose.YES_NO_ASK_LATER, gated_by_country=True,
                       enabled_countries=['GB'], sort_order=3)
    text(research, '1.0')
    updates = purpose('updates', kind='contact_preference', asks_frequency=True,
                      frequency_options=['weekly', 'monthly'], sort_order=4)
    text(updates, '1.0')
    declaration = purpose('declaration', kind='declaration', stage='sign_up', sort_order=0)
    text(declaration, '1.0')
    return {'my_record': my_record, 'research': research, 'updates': updates, 'declaration': declaration}


# ------------------------------------------------------------ texts as data


def test_current_text_is_the_latest_published_version_with_language_fallback(purposes):
    p = purposes['my_record']
    text(p, '2.0', published_at=NOW - timedelta(days=1))
    text(p, '3.0', published_at=None)  # a draft
    text(p, '4.0', published_at=NOW + timedelta(days=1))  # scheduled
    assert svc.current_version(p.texts.all()) == '2.0'
    assert svc.current_text(p.texts.all(), 'es').language == 'en'  # 2.0 has no Spanish yet
    assert svc.text_for(p.texts.all(), '1.0', 'es').language == 'es'


# ------------------------------------------------------------ recording


def test_each_answer_is_appended_with_its_version_and_mirrored_to_the_ledger(member, purposes):
    person = member['person']
    svc.record_consent(person, 'my_record', ConsentEvent.GRANTED)
    svc.record_consent(person, 'my_record', ConsentEvent.WITHDRAWN)
    events = list(ConsentEvent.objects.filter(person=person).order_by('id'))
    assert [e.decision for e in events] == ['granted', 'withdrawn']
    assert {e.version for e in events} == {'1.0'}
    assert svc.has_consent(person, 'my_record') is False

    ledger = list(ConsentLedgerEntry.objects.order_by('id'))
    assert [(l.email, l.key, l.decision, l.version) for l in ledger] == [
        (f'p{person.person_id}@example.test', 'my_record', 'granted', '1.0'),
        (f'p{person.person_id}@example.test', 'my_record', 'withdrawn', '1.0'),
    ]
    assert not any(f.is_relation for f in ConsentLedgerEntry._meta.fields), 'the ledger must not point at the account'


def test_the_ledger_survives_deleting_the_account_and_the_person(member, purposes):
    person = member['person']
    svc.record_consent(person, 'my_record', ConsentEvent.GRANTED)
    PatientUser.objects.filter(person=person).delete()
    member['record'].delete()
    person.delete()
    assert ConsentEvent.objects.count() == 0
    assert ConsentLedgerEntry.objects.count() == 1


def test_history_rows_cannot_be_changed_or_deleted(member, purposes):
    e = svc.record_consent(member['person'], 'my_record', ConsentEvent.GRANTED)
    e.decision = ConsentEvent.WITHDRAWN
    with pytest.raises(ValidationError):
        e.save()
    with pytest.raises(ValidationError):
        e.delete()
    with pytest.raises(ValidationError):
        ConsentEvent.objects.filter(pk=e.pk).update(decision=ConsentEvent.WITHDRAWN)
    with pytest.raises(ValidationError):
        ConsentLedgerEntry.objects.all().delete()
    assert ConsentEvent.objects.get(pk=e.pk).decision == ConsentEvent.GRANTED


def test_a_member_cannot_answer_a_version_that_is_no_longer_current(member, purposes):
    text(purposes['my_record'], '2.0', published_at=NOW - timedelta(days=1))
    with pytest.raises(svc.StaleVersion):
        svc.record_consent(member['person'], 'my_record', ConsentEvent.GRANTED, version='1.0')
    # A migration carries over the version the member actually agreed to.
    e = svc.record_consent(member['person'], 'my_record', ConsentEvent.GRANTED, version='1.0',
                           source=ConsentEvent.MIGRATION, recorded_at=EARLIER)
    assert e.version == '1.0' and e.recorded_at == EARLIER
    state = {s.purpose.key: s for s in svc.purpose_states(member['person'])}['my_record']
    assert state.granted and state.outdated


def test_answer_sets_and_frequency_are_enforced_per_purpose(member, purposes):
    person = member['person']
    with pytest.raises(ValidationError):
        svc.record_consent(person, 'my_record', ConsentEvent.ASK_LATER)
    assert svc.record_consent(person, 'research', ConsentEvent.ASK_LATER).decision == 'ask_later'
    with pytest.raises(ValidationError):
        svc.record_consent(person, 'updates', ConsentEvent.GRANTED, frequency='daily')
    granted = svc.record_consent(person, 'updates', ConsentEvent.GRANTED, frequency='weekly')
    assert granted.frequency == 'weekly'
    declined = svc.record_consent(person, 'updates', ConsentEvent.DECLINED, frequency='weekly')
    assert declined.frequency == ''


def test_a_country_gated_purpose_grants_nothing_outside_its_countries(member, purposes):
    person = member['person']
    svc.record_consent(person, 'research', ConsentEvent.GRANTED)
    assert svc.has_consent(person, 'research') is True
    record = member['record']
    record.country = 'US'
    record.save(update_fields=['country'])
    assert svc.has_consent(person, 'research') is False
    purposes['research'].enabled_countries = ['us']
    purposes['research'].save()
    assert svc.has_consent(person, 'research') is True
    purposes['research'].is_active = False
    purposes['research'].save()
    assert svc.has_consent(person, 'research') is False


def test_has_consent_is_false_without_an_answer_or_a_purpose(member, purposes):
    assert svc.has_consent(member['person'], 'my_record') is False
    assert svc.has_consent(member['person'], 'no_such_purpose') is False


# ------------------------------------------------------------ documents


def test_a_significant_new_version_needs_confirming_and_a_minor_one_does_not(member):
    person = member['person']
    terms = document('terms', '0.4')
    assert [s.document.key for s in svc.pending_documents(person)] == ['terms']
    svc.accept_document(person, 'terms', version='0.4')
    assert svc.pending_documents(person) == []
    assert ConsentLedgerEntry.objects.get().decision == 'accepted'

    LegalDocumentVersion.objects.create(document=terms, version='0.4.1', language='en', title='t', body='b',
                                        published_at=NOW - timedelta(hours=2), requires_reacceptance=False)
    assert svc.pending_documents(person) == []
    LegalDocumentVersion.objects.create(document=terms, version='0.5', language='en', title='t', body='b',
                                        published_at=NOW - timedelta(hours=1))
    assert [s.current_version for s in svc.pending_documents(person)] == ['0.5']
    with pytest.raises(svc.StaleVersion):
        svc.accept_document(person, 'terms', version='0.4')
    svc.accept_document(person, 'terms', version='0.5')
    assert svc.pending_documents(person) == []
    assert DocumentAcceptance.objects.filter(person=person).count() == 2


# ------------------------------------------------------------ the API


def test_get_lists_purposes_and_documents_in_the_members_language(member, purposes):
    document('privacy', '0.3')
    svc.record_consent(member['person'], 'updates', ConsentEvent.GRANTED, frequency='monthly')
    r = member['client'].get('/api/v1/phr/consent/?lang=es')
    assert r.status_code == 200
    by_key = {p['key']: p for p in r.data['purposes']}
    assert list(by_key) == ['declaration', 'my_record', 'research', 'updates']
    assert by_key['my_record']['text']['language'] == 'es'
    assert by_key['research']['text']['language'] == 'en'  # no Spanish text: English wording shown
    assert by_key['research']['answers'] == ['granted', 'declined', 'withdrawn', 'ask_later']
    assert by_key['research']['available'] is True
    assert by_key['my_record']['decision'] is None and by_key['my_record']['granted'] is False
    assert by_key['updates']['decision'] == 'granted' and by_key['updates']['frequency'] == 'monthly'
    assert by_key['updates']['frequency_options'] == ['weekly', 'monthly']
    assert by_key['declaration']['stage'] == 'sign_up'
    (doc,) = r.data['documents']
    assert doc['key'] == 'privacy' and doc['needs_acceptance'] is True and doc['accepted_version'] is None


def test_accept_language_header_picks_the_language(member, purposes):
    r = member['client'].get('/api/v1/phr/consent/', HTTP_ACCEPT_LANGUAGE='es-ES,es;q=0.9,en;q=0.8')
    assert r.data['language'] == 'es'


def test_post_records_several_answers_at_once_or_none(member, purposes):
    client, person = member['client'], member['person']
    r = client.post('/api/v1/phr/consent/', {'decisions': [
        {'purpose': 'declaration', 'decision': 'granted', 'version': '1.0'},
        {'purpose': 'my_record', 'decision': 'granted', 'version': '1.0'},
        {'purpose': 'research', 'decision': 'ask_later', 'version': '1.0'},
        {'purpose': 'updates', 'decision': 'granted', 'version': '1.0', 'frequency': 'weekly'},
    ]}, format='json')
    assert r.status_code == 200, r.data
    by_key = {p['key']: p for p in r.data['purposes']}
    assert by_key['my_record']['granted'] is True
    assert by_key['research']['decision'] == 'ask_later'
    assert svc.has_consent(person, 'my_record')
    assert ConsentEvent.objects.filter(person=person, recorded_by__isnull=False).count() == 4
    assert ConsentLedgerEntry.objects.count() == 4

    # One bad answer in the batch: nothing is written.
    r = client.post('/api/v1/phr/consent/', {'decisions': [
        {'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'},
        {'purpose': 'updates', 'decision': 'granted', 'version': '1.0', 'frequency': 'hourly'},
    ]}, format='json')
    assert r.status_code == 400
    assert svc.has_consent(person, 'my_record')
    assert ConsentEvent.objects.filter(person=person).count() == 4


def test_withdrawing_from_settings_keeps_the_grant_in_the_history(member, purposes):
    client, person = member['client'], member['person']
    client.post('/api/v1/phr/consent/', {'decisions': [{'purpose': 'my_record', 'decision': 'granted', 'version': '1.0'}]}, format='json')
    r = client.post('/api/v1/phr/consent/', {'decisions': [{'purpose': 'my_record', 'decision': 'withdrawn', 'version': '1.0'}]}, format='json')
    assert r.status_code == 200
    assert svc.has_consent(person, 'my_record') is False
    h = client.get('/api/v1/phr/consent/history/').data['history']
    assert [(x['key'], x['decision']) for x in h] == [('my_record', 'withdrawn'), ('my_record', 'granted')]


def test_post_refuses_stale_wording_and_unknown_purposes(member, purposes):
    client = member['client']
    text(purposes['my_record'], '2.0', published_at=NOW - timedelta(days=1))
    r = client.post('/api/v1/phr/consent/', {'decisions': [{'purpose': 'my_record', 'decision': 'granted', 'version': '1.0'}]}, format='json')
    assert r.status_code == 409
    assert 'version 2.0' in r.data['detail']
    r = client.post('/api/v1/phr/consent/', {'decisions': [{'purpose': 'nope', 'decision': 'granted', 'version': '1.0'}]}, format='json')
    assert r.status_code == 400
    r = client.post('/api/v1/phr/consent/', {'decisions': []}, format='json')
    assert r.status_code == 400
    assert ConsentEvent.objects.count() == 0


def test_confirming_the_new_terms(member):
    client, person = member['client'], member['person']
    document('terms', '0.4')
    r = client.post('/api/v1/phr/consent/documents/', {'document': 'terms', 'version': '0.4'}, format='json')
    assert r.status_code == 200
    assert r.data['documents'][0]['needs_acceptance'] is False
    assert r.data['documents'][0]['accepted_version'] == '0.4'
    r = client.post('/api/v1/phr/consent/documents/', {'document': 'terms', 'version': '0.3'}, format='json')
    assert r.status_code == 409
    r = client.post('/api/v1/phr/consent/documents/', {'document': 'nope', 'version': '1'}, format='json')
    assert r.status_code == 400
    h = client.get('/api/v1/phr/consent/history/').data['history']
    assert [(x['kind'], x['key'], x['decision']) for x in h] == [('document', 'terms', 'accepted')]


def test_only_a_member_with_a_record_reads_consent(member, purposes):
    from rest_framework.test import APIClient
    assert APIClient().get('/api/v1/phr/consent/').status_code in (401, 403)
    from patient_portal.models import Identity
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='nobody@example.test'))
    assert client.get('/api/v1/phr/consent/').status_code == 404


def test_a_member_sees_only_their_own_answers(member, purposes):
    other = PatientRecordFactory(country='GB')
    svc.record_consent(other.person, 'my_record', ConsentEvent.GRANTED)
    r = member['client'].get('/api/v1/phr/consent/')
    assert {p['key']: p['decision'] for p in r.data['purposes']}['my_record'] is None
    assert member['client'].get('/api/v1/phr/consent/history/').data['history'] == []


# ------------------------------------------------------------ the loader


def test_load_consent_texts_adds_versions_and_never_rewrites_one(tmp_path, member):
    spec = {
        'purposes': [{'key': 'my_record', 'kind': 'permission', 'stage': 'first_profile',
                      'texts': [{'version': '1.0', 'language': 'en', 'title': 'My record', 'body': 'first wording'}]}],
        'documents': [{'key': 'terms', 'name': 'Terms of Service',
                       'versions': [{'version': '0.4', 'title': 'Terms', 'body': 'the terms',
                                     'published_at': '2026-11-01T00:00:00Z'}]}],
    }
    path = tmp_path / 'consent.json'
    path.write_text(json.dumps(spec))
    call_command('load_consent_texts', str(path), '--publish')
    t = ConsentText.objects.get(purpose__key='my_record', version='1.0')
    assert t.is_published and t.body == 'first wording'
    assert LegalDocumentVersion.objects.get(document__key='terms', version='0.4').is_published
    assert svc.has_consent(member['person'], 'my_record') is False

    spec['purposes'][0]['texts'][0]['body'] = 'reworded in place'  # not allowed: must be a new version
    spec['purposes'][0]['texts'].append({'version': '1.1', 'language': 'en', 'title': 'My record', 'body': 'new wording'})
    spec['purposes'][0]['stage'] = 'sign_up'
    path.write_text(json.dumps(spec))
    call_command('load_consent_texts', str(path))
    assert ConsentText.objects.get(purpose__key='my_record', version='1.0').body == 'first wording'
    draft = ConsentText.objects.get(purpose__key='my_record', version='1.1')
    assert draft.published_at is None and draft.body == 'new wording'
    assert ConsentPurpose.objects.get(key='my_record').stage == 'sign_up'
    assert svc.current_version(ConsentText.objects.filter(purpose__key='my_record')) == '1.0'
