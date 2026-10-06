"""/api/v1/phr/shares/ and /api/v1/phr/shared/ — a patient shares part of their record."""
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import ProcedureOccurrence
from patient_portal.api.phr import sharing
from patient_portal.models import AuditEvent, LabMarker, RecordShare, ShareScan
from tests.factories import ConceptFactory, PatientRecordFactory
from tests.test_phr_read_model import condition, icd10, lab, loinc, signed_in

pytestmark = pytest.mark.django_db

EVERYTHING = {s: {'all': True} for s in sharing.SHARE_SECTIONS}


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''
    settings.PHR_SHARE_URL = 'https://one.example/r'
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'


@pytest.fixture
def patient():
    record = PatientRecordFactory(
        disease='', gender='M', date_of_birth='1961-04-03', city='Houston', country='USA', race='', ethnicity='',
    )
    record.person.given_name, record.person.family_name = 'Kevin', 'Johnson'
    record.person.save(update_fields=['given_name', 'family_name'])
    diabetes = condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')
    gout = condition(record, icd10('M10.9', 'Gout'), '2021-01-01')
    LabMarker.objects.create(loinc_code='48378-4', rank=1, disease_slugs=['myeloma'])
    LabMarker.objects.create(loinc_code='718-7', rank=11, panels=['CBC'])
    hgb, ratio = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood'), loinc('48378-4', 'FLC ratio in Serum')
    lab(record, hgb, '2024-03-01', 11.0)
    lab(record, ratio, '2024-02-01', 2.5)
    ProcedureOccurrence.objects.create(
        procedure_occurrence_id=9101, person=record.person, procedure_date='2024-05-01',
        procedure_concept=ConceptFactory(concept_name='Bone marrow biopsy', concept_code='BMB'),
        procedure_type_concept=ConceptFactory(concept_name='EHR', concept_code='EHR'),
    )
    return {'record': record, 'client': signed_in(record), 'diabetes': diabetes, 'gout': gout,
            'hgb': hgb, 'ratio': ratio}


def grant(client, **body):
    body = {'recipient': 'doctor', 'name': 'Dr. Amara Chen', 'method': 'link', 'duration': '1w',
            'selection': EVERYTHING, **body}
    return client.post('/api/v1/phr/shares/', body, format='json')


def holder(share_or_url):
    url = share_or_url['url'] if isinstance(share_or_url, dict) else share_or_url
    client = APIClient()
    client.credentials(HTTP_X_SHARE_TOKEN=url.rsplit('/', 1)[1])
    return client


# ---------------------------------------------------------------- the link


def test_a_link_is_derived_from_the_share_and_a_forged_one_opens_nothing(patient):
    created = grant(patient['client'])
    assert created.status_code == 201
    url = created.data['url']
    assert url.startswith('https://one.example/r/')
    token = url.rsplit('/', 1)[1]
    assert len(token) == 34
    row = RecordShare.objects.get(public_id=created.data['id'])
    assert token not in str({f.name: getattr(row, f.attname) for f in row._meta.fields})

    assert holder(url).get('/api/v1/phr/shared/').status_code == 200
    forged = token[:-1] + ('A' if token[-1] != 'A' else 'B')
    for bad in (forged, token[:-2], 'x' * 34):
        client = APIClient()
        client.credentials(HTTP_X_SHARE_TOKEN=bad)
        assert client.get('/api/v1/phr/shared/').status_code == 404, bad
    assert APIClient().get('/api/v1/phr/shared/').status_code in (401, 403)


def test_rotating_the_share_secret_closes_links_but_a_secret_key_rotation_does_not(patient, settings):
    url = grant(patient['client']).data['url']
    old = settings.SECRET_KEY
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = 'a-new-key', [old]
    assert holder(url).get('/api/v1/phr/shared/').status_code == 200
    settings.PHR_SHARE_SECRET = 'a-dedicated-share-secret'
    assert holder(url).get('/api/v1/phr/shared/').status_code == 404


# ---------------------------------------------------------------- what the holder sees


def test_the_holder_sees_only_the_items_the_patient_chose(patient):
    selection = {
        'about': {'items': ['details']},
        'diagnoses': {'items': [f"condition-{patient['gout'].pk}"]},
        'labs': {'items': ['panel:CBC']},
    }
    share = grant(patient['client'], selection=selection).data
    client = holder(share)

    meta = client.get('/api/v1/phr/shared/')
    assert meta.data['patient'] == {'name': 'Kevin Johnson', 'first_name': 'Kevin'}
    assert meta.data['recipient'] == {'kind': 'doctor', 'name': 'Dr. Amara Chen'}
    assert meta.data['sections'] == ['about', 'diagnoses', 'labs']
    assert meta['Cache-Control'] == 'no-store' and meta['Referrer-Policy'] == 'no-referrer'

    assert set(client.get('/api/v1/phr/shared/status/').data['sections']) == {'about', 'diagnoses', 'labs'}
    about = {f['key'] for f in client.get('/api/v1/phr/shared/about/').data['fields']}
    assert {'date_of_birth', 'gender'} <= about <= set(sharing.ABOUT_GROUPS['details'])
    dx = client.get('/api/v1/phr/shared/diagnoses/').data
    assert [d['name'] for d in dx['other']] == ['Gout'] and dx['cancer'] == []
    assert [t['name'] for t in client.get('/api/v1/phr/shared/labs/').data['tests']] == ['Hemoglobin']

    hgb = client.get(f"/api/v1/phr/shared/labs/{patient['hgb'].concept_id}/")
    assert hgb.status_code == 200 and 'therapy' not in hgb.data  # lines of therapy were not shared
    assert client.get(f"/api/v1/phr/shared/labs/{patient['ratio'].concept_id}/").status_code == 404
    for unshared in ('procedures', 'medications', 'therapy', 'genetics', 'imaging'):
        assert client.get(f'/api/v1/phr/shared/{unshared}/').status_code == 404, unshared


def test_a_whole_section_includes_records_that_arrive_later(patient):
    client = holder(grant(patient['client'], selection={'diagnoses': {'all': True}}).data)
    condition(patient['record'], icd10('I10', 'Hypertension'), '2025-01-01')
    names = [d['name'] for d in client.get('/api/v1/phr/shared/diagnoses/').data['other']]
    assert names == ['Hypertension', 'Gout', 'Type 2 diabetes']


def test_a_shared_record_is_read_only_and_never_reaches_the_patients_own_endpoints(patient):
    client = holder(grant(patient['client']).data)
    assert client.post('/api/v1/phr/shared/medications/', {}, format='json').status_code == 405
    assert client.get('/api/v1/phr/about/').status_code in (401, 403)
    assert client.get('/api/v1/phr/shares/').status_code in (401, 403)
    # The feed summarises every section, so it has no shared twin.
    assert 'json' not in client.get('/api/v1/phr/shared/whats-new/')['Content-Type']


def test_each_read_is_audited_against_the_share(patient):
    share = grant(patient['client']).data
    holder(share).get('/api/v1/phr/shared/labs/')
    event = AuditEvent.objects.filter(path='/api/v1/phr/shared/labs/').get()
    assert event.client_id == f"share:{share['id']}" and event.user_id is None


# ---------------------------------------------------------------- expiry, renewal, removal


def test_an_expired_share_is_gone_until_the_patient_renews_it(patient):
    share = grant(patient['client'], duration='1h').data
    RecordShare.objects.filter(public_id=share['id']).update(expires_at=timezone.now() - timedelta(minutes=1))

    gone = holder(share).get('/api/v1/phr/shared/')
    assert gone.status_code == 410 and gone.data['code'] == 'expired'
    listed = patient['client'].get('/api/v1/phr/shares/').data['shares']
    assert [s['status'] for s in listed] == ['expired']

    renewed = patient['client'].post(f"/api/v1/phr/shares/{share['id']}/renew/", {'duration': '1y'}, format='json')
    assert renewed.status_code == 200 and renewed.data['status'] == 'active'
    assert holder(share).get('/api/v1/phr/shared/').status_code == 200
    bad = patient['client'].post(f"/api/v1/phr/shares/{share['id']}/renew/", {'duration': '5y'}, format='json')
    assert bad.status_code == 400


def test_removing_access_closes_the_link_for_good(patient):
    share = grant(patient['client']).data
    assert patient['client'].delete(f"/api/v1/phr/shares/{share['id']}/").status_code == 204
    assert holder(share).get('/api/v1/phr/shared/').status_code == 404
    assert patient['client'].get('/api/v1/phr/shares/').data['shares'] == []
    assert patient['client'].post(f"/api/v1/phr/shares/{share['id']}/renew/", {}, format='json').status_code == 404


def test_shares_belong_to_their_patient(patient):
    share = grant(patient['client']).data
    other = signed_in(PatientRecordFactory(disease=''))
    assert other.get('/api/v1/phr/shares/').data['shares'] == []
    assert other.patch(f"/api/v1/phr/shares/{share['id']}/", {'selection': EVERYTHING}, format='json').status_code == 404
    assert other.delete(f"/api/v1/phr/shares/{share['id']}/").status_code == 404
    assert other.post(f"/api/v1/phr/shares/{share['id']}/renew/", {}, format='json').status_code == 404


# ---------------------------------------------------------------- granting


def test_the_patient_changes_what_a_person_sees(patient):
    share = grant(patient['client']).data
    changed = patient['client'].patch(
        f"/api/v1/phr/shares/{share['id']}/", {'selection': {'labs': {'all': True}}}, format='json',
    )
    assert changed.status_code == 200 and changed.data['selection'] == {'labs': {'all': True}}
    assert holder(share).get('/api/v1/phr/shared/').data['sections'] == ['labs']


def test_what_to_share_is_validated(patient):
    client = patient['client']
    assert grant(client, selection={}).data == {'selection': 'Choose at least one item to share.'}
    assert grant(client, selection={'whats_new': {'all': True}}).status_code == 400
    assert grant(client, selection={'labs': {'items': ['x' * 65]}}).status_code == 400
    assert set(grant(client, recipient='boss', name=' ', duration='2d', method='fax').data) == {
        'recipient', 'name', 'duration', 'method',
    }
    assert grant(client, method='email', email='not-an-address').data == {
        'email': 'Enter an email address like name@example.com.',
    }
    assert not RecordShare.objects.exists()


def test_a_check_in_is_a_fixed_qr_code_and_logs_where_it_was_scanned(patient, settings):
    settings.PHR_SHARE_GEO_HEADERS = {'city': 'X-Client-City', 'region': 'X-Client-Region'}
    share = grant(patient['client'], recipient='checkin', name='MD Anderson', method='link').data
    assert share['method'] == 'qr'
    fixed = patient['client'].patch(f"/api/v1/phr/shares/{share['id']}/", {'selection': EVERYTHING}, format='json')
    assert fixed.status_code == 400

    client = holder(share)
    client.get('/api/v1/phr/shared/', HTTP_X_CLIENT_CITY='Houston', HTTP_X_CLIENT_REGION='TX', HTTP_X_FORWARDED_FOR='203.0.113.9')
    client.get('/api/v1/phr/shared/')  # the same visit
    assert ShareScan.objects.count() == 1
    ShareScan.objects.update(scanned_at=timezone.now() - timedelta(hours=1))
    client.get('/api/v1/phr/shared/')

    scans = patient['client'].get('/api/v1/phr/shares/').data['shares'][0]['scans']
    assert len(scans) == 2 and {k: v for k, v in scans[1].items() if k != 'at'} == {'city': 'Houston', 'region': 'TX'}
    assert '203.0.113.9' not in str(list(ShareScan.objects.values()))


def test_an_email_invite_carries_the_link_and_can_be_sent_again(patient):
    share = grant(patient['client'], method='email', email='amara.chen@example.org').data
    assert share['email'] == 'amara.chen@example.org'
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ['amara.chen@example.org']
    assert message.subject == 'Kevin Johnson shared their HealthKey health record with you'
    assert share['url'] in message.body and 'Hi Dr. Amara Chen' in message.body

    assert patient['client'].post(f"/api/v1/phr/shares/{share['id']}/resend/").status_code == 200
    assert len(mail.outbox) == 2
    link = grant(patient['client']).data
    assert patient['client'].post(f"/api/v1/phr/shares/{link['id']}/resend/").status_code == 404


def test_an_invite_that_cannot_be_sent_creates_no_share(patient, monkeypatch):
    def broken(*args, **kwargs):
        raise ConnectionError('mail relay down')

    monkeypatch.setattr(sharing, 'send_mail', broken)
    failed = grant(patient['client'], method='email', email='amara.chen@example.org')
    assert failed.status_code == 502
    assert not RecordShare.objects.exists()


def test_options_list_each_section_with_its_items_and_groups(patient):
    sections = {s['id']: s for s in patient['client'].get('/api/v1/phr/shares/options/').data['sections']}
    assert list(sections) == ['about', 'diagnoses', 'labs', 'procedures']
    details, contact = sections['about']['items']
    assert (details['key'], details['label']) == ('details', 'Personal details')
    assert {'date_of_birth', 'gender'} <= set(details['members'])
    assert (contact['key'], contact['label'], contact['members']) == (
        'contact', 'Contact information and address', ['address'],
    )
    assert [(i['key'], i['label']) for i in sections['labs']['items']] == [
        ('markers', 'Cancer markers'), ('panel:CBC', 'Blood counts (CBC)'),
    ]
    assert [i['label'] for i in sections['diagnoses']['items']] == ['Gout', 'Type 2 diabetes']
    assert sections['procedures']['items'][0]['label'] == 'Bone marrow biopsy'
