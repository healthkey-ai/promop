"""A purpose that requires another (#2782): it cannot be granted while that
one is not, and is withdrawn with it when that grant ends."""
import json

import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command

from patient_portal import consent as svc
from patient_portal.models import ConsentEvent, ConsentLedgerEntry, ConsentPurpose
from tests.factories import PatientRecordFactory
from tests.test_consent_v2 import purpose, text
from tests.test_phr_read_model import signed_in

pytestmark = pytest.mark.django_db

URL = '/api/v1/phr/consent/'


@pytest.fixture
def member():
    record = PatientRecordFactory(country='GB')
    return {'record': record, 'person': record.person, 'client': signed_in(record)}


@pytest.fixture
def purposes():
    """My record, which two permissions require; one of them requires the other in turn."""
    made = {
        'my_record': purpose('my_record', sort_order=1, erases_record_on_withdrawal=True),
        'community': purpose('community', sort_order=2, requires='my_record'),
        'research': purpose('research', sort_order=3, requires='my_record'),
        'research_contact': purpose('research_contact', sort_order=4, requires='research'),
        'updates': purpose('updates', kind='contact_preference', sort_order=5),
    }
    for p in made.values():
        text(p, '1.0')
    return made


def grant(person, *keys):
    for key in keys:
        svc.record_consent(person, key, ConsentEvent.GRANTED, version='1.0')


def standing(person):
    latest = svc.latest_events(person)
    return {p.key: latest[p.id].decision for p in ConsentPurpose.objects.all() if p.id in latest}


def test_withdrawing_my_record_withdraws_what_requires_it(member, purposes):
    person = member['person']
    grant(person, 'my_record', 'community', 'research', 'research_contact', 'updates')

    svc.record_consent(person, 'my_record', ConsentEvent.WITHDRAWN, version='1.0')

    assert standing(person) == {
        'my_record': 'withdrawn', 'community': 'withdrawn', 'research': 'withdrawn',
        'research_contact': 'withdrawn', 'updates': 'granted',
    }
    # Each is a system event with the version the member granted, and in the ledger too.
    for key in ('community', 'research', 'research_contact'):
        event = ConsentEvent.objects.filter(person=person, purpose__key=key).latest('recorded_at', 'id')
        assert (event.source, event.version) == (ConsentEvent.SYSTEM, '1.0')
        assert ConsentLedgerEntry.objects.filter(key=key, decision='withdrawn').exists()


def test_declining_my_record_after_granting_it_does_the_same(member, purposes):
    person = member['person']
    grant(person, 'my_record', 'community')

    svc.record_consent(person, 'my_record', ConsentEvent.DECLINED, version='1.0')

    assert standing(person)['community'] == 'withdrawn'


def test_a_dependent_never_granted_is_left_alone(member, purposes):
    person = member['person']
    grant(person, 'my_record')
    svc.record_consent(person, 'community', ConsentEvent.DECLINED, version='1.0')

    svc.record_consent(person, 'my_record', ConsentEvent.WITHDRAWN, version='1.0')

    assert standing(person)['community'] == 'declined'
    assert 'research' not in standing(person)


def test_a_dependent_cannot_be_granted_without_what_it_requires(member, purposes):
    person = member['person']
    with pytest.raises(ValidationError, match='needs my_record'):
        grant(person, 'community')
    grant(person, 'my_record', 'community')
    svc.record_consent(person, 'my_record', ConsentEvent.WITHDRAWN, version='1.0')
    with pytest.raises(ValidationError, match='needs my_record'):
        grant(person, 'community')


def test_a_batch_records_the_required_purpose_first(member, purposes):
    """Over the API, a client granting both at once may name the dependent first."""
    r = member['client'].post(URL, {'decisions': [
        {'purpose': 'research', 'decision': 'granted', 'version': '1.0'},
        {'purpose': 'my_record', 'decision': 'granted', 'version': '1.0'},
    ]}, format='json')
    assert r.status_code == 200, r.content
    assert standing(member['person']) == {'my_record': 'granted', 'research': 'granted'}
    payload = {p['key']: p for p in r.json()['purposes']}
    assert payload['research']['requires'] == 'my_record'
    assert payload['my_record']['requires'] is None


def test_a_purpose_cannot_require_itself_or_an_unknown_one(tmp_path):
    def load(spec):
        path = tmp_path / 'consent.json'
        path.write_text(json.dumps({'purposes': [spec]}))
        call_command('load_consent_texts', str(path), publish=True)

    with pytest.raises(CommandError, match='cannot require itself'):
        load({'key': 'research', 'requires': 'research', 'texts': [{'version': '1', 'title': 'R', 'body': 'b'}]})
    with pytest.raises(CommandError, match="requires 'my_record', which is not a purpose"):
        load({'key': 'research', 'requires': 'my_record', 'texts': [{'version': '1', 'title': 'R', 'body': 'b'}]})
