"""Sign-up v2: the pilot country list and per-country minimum age, year of
birth written year-only, and the sign-up declarations and documents recorded
through consent v2 when an invitation is accepted (#2596)."""
import secrets
from datetime import date, timedelta

import pytest
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, Location, Organization
from patient_portal import signup_policy as policy
from patient_portal.models import (
    ConsentEvent, ConsentLedgerEntry, ConsentPurpose, ConsentText, DocumentAcceptance, Identity,
    LegalDocument, LegalDocumentVersion, PatientInvitation, PatientUser,
)
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

EARLIER = timezone.now() - timedelta(days=30)
ACCEPT = '/api/v1/patient-invitations/accept/'
POLICY = '/api/v1/phr/signup/policy/'
PASSWORD = 'sup3r-secret-pass-2026'


def purpose(key, stage='sign_up', kind=ConsentPurpose.DECLARATION, version='1.0', **kw):
    p = ConsentPurpose.objects.create(key=key, kind=kind, stage=stage, **kw)
    ConsentText.objects.create(purpose=p, version=version, language='en', title=key, body=f'{key} body',
                               summary=f'{key} summary', published_at=EARLIER)
    return p


def document(key, version='1.0'):
    d = LegalDocument.objects.create(key=key, name=key.title())
    LegalDocumentVersion.objects.create(document=d, version=version, language='en', title=key, body='terms',
                                        published_at=EARLIER)
    return d


@pytest.fixture
def texts():
    return {
        'age': purpose('age_declaration'),
        'declaration': purpose('sign_up_declaration'),
        'later': purpose('my_record', stage='first_profile', kind=ConsentPurpose.PERMISSION),
        'terms': document('terms'),
    }


def invite(email='rae@example.test', requires=True, year=1900, month=None, day=None):
    record = PatientRecordFactory(country=None)
    Organization.objects.filter(pk=record.organization_id).update(requires_signup_declarations=requires)
    person = record.person
    person.year_of_birth, person.month_of_birth, person.day_of_birth = year, month, day
    person.save(update_fields=['year_of_birth', 'month_of_birth', 'day_of_birth'])
    return PatientInvitation.objects.create(
        person=person, email=email, token=secrets.token_hex(32), expires_at=timezone.now() + timedelta(days=7),
    )


@pytest.fixture
def invitation():
    """An invitation to a record of a member-app org, which requires the declarations."""
    return invite()


def body(invitation, **extra):
    data = {
        'token': invitation.token, 'password': PASSWORD, 'country': 'GB', 'year_of_birth': 1990,
        'consent': {
            'decisions': [
                {'purpose': 'age_declaration', 'decision': 'granted', 'version': '1.0'},
                {'purpose': 'sign_up_declaration', 'decision': 'granted', 'version': '1.0'},
            ],
            'documents': [{'document': 'terms', 'version': '1.0'}],
        },
    }
    data.update(extra)
    return data


def accept(invitation, **extra):
    return APIClient().post(ACCEPT, body(invitation, **extra), format='json')


# ------------------------------------------------------------------ policy


def test_the_pilot_countries_carry_a_minimum_age_of_18_unless_configured(settings):
    codes = {c['code'] for c in policy.countries()}
    assert {'GB', 'US', 'CA', 'AU', 'CH', 'DE', 'FR', 'NO', 'IS'} <= codes
    assert all(c['minimum_age'] == 18 for c in policy.countries())
    settings.PHR_SIGNUP_MINIMUM_AGES = {'ca': 19}
    assert policy.minimum_age('CA') == 19
    assert policy.minimum_age('GB') == 18
    settings.PHR_SIGNUP_COUNTRIES = ['gb', 'ie']
    assert [c['code'] for c in policy.countries()] == ['GB', 'IE']
    with pytest.raises(ValidationError):
        policy.validate_country('US')
    assert policy.validate_country(' gb ') == 'GB'


def test_a_year_that_cannot_reach_the_minimum_age_is_refused_and_the_boundary_year_is_not(settings):
    today = date(2026, 11, 16)
    assert policy.validate_year_of_birth(2008, 'GB', today) == 2008  # may still be 17: the declaration decides
    assert policy.validate_year_of_birth('1990', 'GB', today) == 1990
    with pytest.raises(ValidationError, match='at least 18'):
        policy.validate_year_of_birth(2009, 'GB', today)
    settings.PHR_SIGNUP_MINIMUM_AGES = {'CA': 19}
    with pytest.raises(ValidationError, match='at least 19'):
        policy.validate_year_of_birth(2008, 'CA', today)
    for bad in ('199', 'abcd', 2027, 1900, True, None, 1990.0):
        with pytest.raises(ValidationError):
            policy.validate_year_of_birth(bad, 'GB', today)


def test_the_policy_endpoint_is_public_and_lists_sign_up_texts_only(texts):
    resp = APIClient().get(POLICY)
    assert resp.status_code == 200
    data = resp.json()
    assert {'code': 'GB', 'name': 'United Kingdom', 'minimum_age': 18} in data['countries']
    assert [p['key'] for p in data['purposes']] == ['age_declaration', 'sign_up_declaration']
    assert data['purposes'][0]['text']['version'] == '1.0'
    assert data['purposes'][0]['text']['body'] == 'age_declaration body'
    assert [d['key'] for d in data['documents']] == ['terms']
    assert data['documents'][0]['text']['version'] == '1.0'


# ------------------------------------------------------------------ acceptance


def test_accepting_writes_country_and_year_and_records_the_declarations(texts, invitation):
    resp = accept(invitation)
    assert resp.status_code == 200, resp.data
    person = invitation.person
    person.refresh_from_db()
    assert (person.year_of_birth, person.month_of_birth, person.day_of_birth) == (1990, None, None)
    assert Location.objects.get(location_id=person.location_id).country == 'GB'
    record = person.patient_record
    record.refresh_from_db()
    assert record.country == 'GB'
    assert (record.date_of_birth, record.date_of_birth_precision) == (date(1990, 1, 1), 'year')

    identity = PatientUser.objects.get(person=person).identity
    events = {e.purpose.key: e for e in ConsentEvent.objects.filter(person=person)}
    assert set(events) == {'age_declaration', 'sign_up_declaration'}
    assert all(e.decision == 'granted' and e.version == '1.0' and e.source == 'member' for e in events.values())
    assert all(e.recorded_by == identity for e in events.values())
    acceptance = DocumentAcceptance.objects.get(person=person)
    assert (acceptance.document.key, acceptance.version) == ('terms', '1.0')
    assert set(ConsentLedgerEntry.objects.values_list('email', flat=True)) == {'rae@example.test'}
    assert ConsentLedgerEntry.objects.count() == 3
    invitation.refresh_from_db()
    assert invitation.status == 'accepted'
    assert GroupAccess.objects.filter(identity=identity, org=record.organization, role='patient').exists()


def test_a_missing_or_declined_declaration_refuses_the_sign_up_and_writes_nothing(texts, invitation):
    consent = body(invitation)['consent']
    consent['decisions'] = consent['decisions'][:1]
    resp = accept(invitation, consent=consent)
    assert resp.status_code == 400
    assert 'sign_up_declaration must be answered' in resp.data['error']

    consent = body(invitation)['consent']
    consent['decisions'][0]['decision'] = 'declined'
    resp = accept(invitation, consent=consent)
    assert resp.status_code == 400
    assert 'age_declaration must be confirmed' in resp.data['error']

    consent = body(invitation)['consent']
    consent['documents'] = []
    resp = accept(invitation, consent=consent)
    assert resp.status_code == 400
    assert 'terms must be accepted' in resp.data['error']

    resp = accept(invitation, consent=None)
    assert resp.status_code == 400

    # Nothing of the refused sign-ups survives: no account, no history, no location.
    assert not Identity.objects.filter(email='rae@example.test').exists()
    assert not PatientUser.objects.filter(person=invitation.person).exists()
    assert ConsentEvent.objects.count() == DocumentAcceptance.objects.count() == 0
    assert ConsentLedgerEntry.objects.count() == 0
    invitation.person.refresh_from_db()
    assert invitation.person.location_id is None
    invitation.refresh_from_db()
    assert invitation.status == 'pending'


def test_wording_republished_since_the_form_loaded_is_a_409(texts, invitation):
    ConsentText.objects.create(purpose=texts['age'], version='1.1', language='en', title='age', body='new',
                               published_at=timezone.now() - timedelta(minutes=1))
    resp = accept(invitation)
    assert resp.status_code == 409
    assert 'now version 1.1' in resp.data['error']
    assert not Identity.objects.filter(email='rae@example.test').exists()


def test_country_and_year_are_checked_against_the_policy(texts, invitation, settings):
    assert accept(invitation, country='XX').status_code == 400
    assert 'country must be one of' in accept(invitation, country='XX').data['error']
    resp = accept(invitation, year_of_birth=timezone.now().year - 17)
    assert resp.status_code == 400
    assert 'at least 18' in resp.data['error']
    settings.PHR_SIGNUP_MINIMUM_AGES = {'CA': 19}
    resp = accept(invitation, country='CA', year_of_birth=timezone.now().year - 18)
    assert resp.status_code == 400
    assert 'at least 19' in resp.data['error']
    data = body(invitation)
    del data['country']
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 400
    assert 'country is required with year_of_birth' in resp.data['error']
    assert not Identity.objects.filter(email='rae@example.test').exists()


def test_a_country_gated_declaration_is_required_only_in_its_countries(texts, invitation):
    purpose('us_notice', gated_by_country=True, enabled_countries=['US'])
    resp = accept(invitation)  # GB: the US notice is not asked
    assert resp.status_code == 200, resp.data
    assert not ConsentEvent.objects.filter(purpose__key='us_notice').exists()

    other = invite('lee@example.test')
    resp = accept(other, country='US')
    assert resp.status_code == 400
    assert 'us_notice must be answered' in resp.data['error']
    data = body(other, country='US')
    data['consent']['decisions'].append({'purpose': 'us_notice', 'decision': 'granted', 'version': '1.0'})
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 200, resp.data
    assert ConsentEvent.objects.filter(person=other.person, purpose__key='us_notice').exists()


def test_an_invitation_without_sign_up_texts_loaded_is_accepted_as_before(invitation):
    """With no sign-up texts loaded, token and password are enough, even where
    the organisation would require declarations."""
    resp = APIClient().post(ACCEPT, {'token': invitation.token, 'password': PASSWORD}, format='json')
    assert resp.status_code == 200, resp.data
    assert PatientUser.objects.filter(person=invitation.person).exists()
    invitation.person.refresh_from_db()
    assert invitation.person.year_of_birth == 1900
    assert invitation.person.location_id is None
    assert ConsentEvent.objects.count() == 0


def test_a_sign_up_may_also_answer_a_later_stage_purpose_but_not_an_unknown_one(texts, invitation):
    data = body(invitation)
    data['consent']['decisions'].append({'purpose': 'my_record', 'decision': 'granted', 'version': '1.0'})
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 200, resp.data
    assert ConsentEvent.objects.filter(person=invitation.person, purpose__key='my_record').exists()

    other = invite('lee@example.test')
    data = body(other)
    data['consent']['decisions'].append({'purpose': 'nope', 'decision': 'granted', 'version': '1.0'})
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 400
    assert "Unknown purpose 'nope'" in resp.data['error']


def test_the_flf_pilot_texts_file_loads_and_defines_the_sign_up(invitation):
    from pathlib import Path
    from django.core.management import call_command
    path = str(Path(__file__).resolve().parents[1] / 'patient_portal/data/consent/flf-pilot.json')
    call_command('load_consent_texts', path, '--publish')
    data = APIClient().get(POLICY).json()
    assert [p['key'] for p in data['purposes']] == ['age_declaration', 'sign_up_declaration']
    assert [d['key'] for d in data['documents']] == ['terms', 'privacy_policy']
    assert ConsentPurpose.objects.filter(stage='first_profile').count() == 5
    assert ConsentPurpose.objects.get(key='research').answers == ConsentPurpose.YES_NO_ASK_LATER
    assert ConsentPurpose.objects.get(key='flf_updates').frequency_options == ['weekly', 'monthly', 'quarterly']
    # A second load changes nothing: the versions are kept as they are.
    call_command('load_consent_texts', path)
    assert ConsentText.objects.filter(purpose__key='age_declaration').count() == 1

    data = body(invitation)
    data['consent']['decisions'] = [
        {'purpose': 'age_declaration', 'decision': 'granted', 'version': '0.1'},
        {'purpose': 'sign_up_declaration', 'decision': 'granted', 'version': '0.1'},
    ]
    data['consent']['documents'] = [
        {'document': 'terms', 'version': '0.1'}, {'document': 'privacy_policy', 'version': '0.1'},
    ]
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 200, resp.data
    assert ConsentEvent.objects.filter(person=invitation.person).count() == 2
    assert DocumentAcceptance.objects.filter(person=invitation.person).count() == 2


def test_a_hospital_org_does_not_require_the_declarations_even_with_texts_loaded(texts):
    """The hospital record claim and a member app share one instance: the
    org decides. A hospital invitation is accepted with token and password
    alone, and records any consent the form does send."""
    inv = invite(requires=False)
    resp = APIClient().post(ACCEPT, {'token': inv.token, 'password': PASSWORD}, format='json')
    assert resp.status_code == 200, resp.data
    assert PatientUser.objects.filter(person=inv.person).exists()
    assert ConsentEvent.objects.count() == 0

    other = invite('lee@example.test', requires=False)
    data = body(other)
    data['consent']['decisions'] = data['consent']['decisions'][:1]
    data['consent']['documents'] = []
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 200, resp.data
    assert ConsentEvent.objects.filter(person=other.person, purpose__key='age_declaration').exists()
    # A stale version is still refused: an answer is never recorded against old wording.
    third = invite('kim@example.test', requires=False)
    data = body(third)
    data['consent']['decisions'][0]['version'] = '0.9'
    assert APIClient().post(ACCEPT, data, format='json').status_code == 409
    assert not Identity.objects.filter(email='kim@example.test').exists()


def test_a_hospital_date_of_birth_keeps_its_precision_when_the_member_gives_the_same_year(texts):
    inv = invite(year=1985, month=6, day=12)
    resp = accept(inv, year_of_birth=1985)
    assert resp.status_code == 200, resp.data
    inv.person.refresh_from_db()
    assert (inv.person.year_of_birth, inv.person.month_of_birth, inv.person.day_of_birth) == (1985, 6, 12)
    record = inv.person.patient_record
    record.refresh_from_db()
    assert (record.date_of_birth, record.date_of_birth_precision) == (date(1985, 6, 12), 'day')

    other = invite('lee@example.test', year=1985, month=6, day=12)
    resp = accept(other, year_of_birth=1986)  # the member's own statement replaces it, year-only
    assert resp.status_code == 200, resp.data
    other.person.refresh_from_db()
    assert (other.person.year_of_birth, other.person.month_of_birth, other.person.day_of_birth) == (1986, None, None)


def test_a_pending_ui_edit_of_country_does_not_win_over_the_sign_up(texts, invitation):
    record = invitation.person.patient_record
    record.country = 'FR'
    record.user_edited_fields = ['country', 'disease']
    record.save(update_fields=['country', 'user_edited_fields'])
    purpose('gb_notice', gated_by_country=True, enabled_countries=['GB'])
    data = body(invitation)
    data['consent']['decisions'].append({'purpose': 'gb_notice', 'decision': 'granted', 'version': '1.0'})
    resp = APIClient().post(ACCEPT, data, format='json')
    assert resp.status_code == 200, resp.data
    record.refresh_from_db()
    assert record.country == 'GB'
    assert record.user_edited_fields == ['disease']


def test_a_stale_document_version_is_refused_before_anything_is_written(texts, invitation):
    LegalDocumentVersion.objects.create(document=texts['terms'], version='1.1', language='en', title='terms',
                                        body='new', requires_reacceptance=True,
                                        published_at=timezone.now() - timedelta(minutes=1))
    data = APIClient().get(POLICY).json()
    assert data['documents'][0]['text']['version'] == '1.1'
    resp = accept(invitation)
    assert resp.status_code == 409
    assert 'terms is now version 1.1' in resp.data['error']
    invitation.person.refresh_from_db()
    assert invitation.person.location_id is None
    assert invitation.person.year_of_birth == 1900


def test_a_form_encoded_body_cannot_carry_consent_and_says_so(texts, invitation):
    resp = APIClient().post(ACCEPT, {'token': invitation.token, 'password': PASSWORD, 'consent': 'x'})
    assert resp.status_code == 400
    assert 'consent must be an object' in resp.data['error']


def test_the_policy_falls_back_to_english_for_a_language_with_no_text(texts):
    data = APIClient().get(POLICY, {'lang': 'es'}).json()
    assert data['purposes'][0]['text']['language'] == 'en'
    data = APIClient().get(POLICY, HTTP_ACCEPT_LANGUAGE='pt-BR,pt;q=0.9').json()
    assert data['documents'][0]['text']['language'] == 'en'


def test_misconfigured_minimum_ages_fail_loudly_rather_than_500(settings):
    for bad in (['CA'], {'CA': 'nineteen'}, {'CA': True}, {'CA': 0}):
        settings.PHR_SIGNUP_MINIMUM_AGES = bad
        with pytest.raises(ImproperlyConfigured):
            policy.countries()
