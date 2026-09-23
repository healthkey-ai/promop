import hashlib
import hmac
import json
import socket
import urllib3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, Organization, PatientDocument, PatientRecord, Person
from patient_portal.api.fhir.sync import AGGREGATION_EXT_URL
from patient_portal.models import (
    Identity, InboundWebhookEvent, WebhookDelivery, WebhookSubscription,
    WebhookSubscriptionChange,
)
from patient_portal.tasks import deliver_webhook, dispatch_pending_webhooks
from patient_portal.webhooks import (
    compute_hmac_signature, encode_payload, enqueue_delivery, publish_event,
    resolve_webhook_url, send_webhook,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(settings):
    settings.WEBHOOKS_ENABLED = True
    from django.core.cache import cache
    cache.clear()
    settings.CELERY_BROKER_URL = ''
    org = Organization.objects.create(name='Webhook tenant', slug='webhook-tenant')
    other = Organization.objects.create(name='Other tenant', slug='webhook-other')
    person = Person.objects.create(person_id=420001)
    PatientRecord.objects.create(person=person, organization=org)
    user = Identity.objects.create_user(email='webhook-admin@example.org')
    GroupAccess.objects.create(identity=user, org=org, role='org_admin')
    settings.WEBHOOK_INBOUND_SOURCES = {
        'lab-system': {'secret': 'test-source-secret', 'organization': org.slug},
    }
    subscription = WebhookSubscription.objects.create(
        organization=org, url='https://subscriber.example/events',
        event_types=['patient.changed', 'lab.updated', 'document.received', 'foundation.synced'],
    )
    return org, other, person, user, subscription


def inbound(payload=None, signature=None, source='lab-system', **headers):
    payload = payload or {'id': 'event-1', 'type': 'lab.updated', 'data': {'person_id': 420001}}
    body = payload if isinstance(payload, bytes) else encode_payload(payload)
    timestamp = headers.pop('HTTP_X_HEALTHKEY_TIMESTAMP', str(int(time.time())))
    return APIClient().post(
        '/api/v1/webhooks/inbound/', body, content_type='application/json',
        HTTP_X_HEALTHKEY_SOURCE=source,
        HTTP_X_HEALTHKEY_TIMESTAMP=timestamp,
        HTTP_X_HEALTHKEY_SIGNATURE=signature if signature is not None else compute_hmac_signature(body, 'test-source-secret', timestamp),
        **headers,
    )


def test_hmac_matches_standard():
    payload = b'{"exact": "bytes"}\n'
    expected = 'sha256=' + hmac.new(b'secret', payload, hashlib.sha256).hexdigest()
    assert compute_hmac_signature(payload, 'secret') == expected
    assert compute_hmac_signature(payload.rstrip(), 'secret') != expected


@pytest.mark.parametrize('signature', ['', 'invalid', 'sha256=' + '0' * 64, 'é'])
def test_inbound_rejects_bad_signature(setup, signature):
    assert inbound(signature=signature).status_code == 401
    assert not InboundWebhookEvent.objects.exists()


def test_unknown_source_cannot_select_org(setup):
    assert inbound(source='other').status_code == 401
    assert not WebhookDelivery.objects.exists()


@pytest.mark.parametrize('event_type', ['lab.updated', 'document.received', 'foundation.synced'])
def test_inbound_dispatch_and_deduplication(setup, event_type):
    org, other, person, user, subscription = setup
    WebhookSubscription.objects.create(organization=other, url=subscription.url, event_types=[event_type])
    payload = {'id': 'event-1', 'type': event_type, 'data': {'person_id': person.pk, 'resource_id': 'external-42'}}
    assert inbound(payload, HTTP_IDEMPOTENCY_KEY='event-1').status_code == 202
    assert inbound(payload).data['duplicate'] is True
    assert InboundWebhookEvent.objects.get().processed_at is not None
    delivery = WebhookDelivery.objects.get()
    assert delivery.subscription_id == subscription.pk
    assert delivery.payload['type'] == event_type
    assert delivery.payload['data']['resource_id'] == 'external-42'


def test_id_reuse_with_changed_payload_conflicts(setup):
    assert inbound().status_code == 202
    assert inbound({'id': 'event-1', 'type': 'document.received', 'data': {'person_id': 420001}}).status_code == 409
    assert WebhookDelivery.objects.count() == 1


@pytest.mark.parametrize('payload', [
    b'not JSON', b'null', b'[]', b'{}',
    {'id': 'x', 'type': 'unsupported', 'data': {'person_id': 420001}},
    {'id': 'x', 'type': 'lab.updated', 'data': {'person_id': 'invalid'}},
    {'id': 'x', 'type': 'lab.updated', 'data': {}},
])
def test_signed_invalid_payload_rejected(setup, payload):
    assert inbound(payload).status_code == 400
    assert not InboundWebhookEvent.objects.exists()


def test_source_cannot_notify_another_tenant(setup):
    org, other, person, user, subscription = setup
    PatientRecord.objects.filter(person=person).update(organization=other)
    assert inbound().status_code == 400
    assert not WebhookDelivery.objects.exists()


def test_a_replay_stays_a_duplicate_after_the_patient_leaves(setup):
    """An event this endpoint accepted cannot become rejected later.

    The sender retries because it did not see our answer, not because anything
    changed here. If the patient has since been deleted or moved to another
    organization, answering "unknown patient" would tell the sender an event we
    took was refused, and its retry would never settle.
    """
    org, other, person, user, subscription = setup
    assert inbound().status_code == 202

    PatientRecord.objects.filter(person=person).update(organization=other)
    replay = inbound()
    assert replay.status_code == 200, replay.data
    assert replay.data['duplicate'] is True

    # A first-time event for a patient this source cannot name is still refused.
    fresh = inbound({'id': 'event-2', 'type': 'lab.updated', 'data': {'person_id': 420001}})
    assert fresh.status_code == 400, fresh.data
    assert not InboundWebhookEvent.objects.filter(event_id='event-2').exists()


def test_unsigned_idempotency_key_must_match_signed_id(setup):
    assert inbound(HTTP_IDEMPOTENCY_KEY='different').status_code == 400


def test_subscription_management_and_secret_visibility(setup):
    org, other, person, user, subscription = setup
    client = APIClient()
    # A session, not force_authenticate: writes here are credential
    # administration and the endpoint requires an interactive session.
    client.force_login(user)
    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        response = client.post('/api/v1/webhooks/subscriptions/', {
            'organization': org.pk, 'url': subscription.url, 'event_types': ['lab.updated'],
        }, format='json')
        assert response.status_code == 201
        assert len(response.data['secret']) >= 32
        assert client.post('/api/v1/webhooks/subscriptions/', {
            'organization': other.pk, 'url': subscription.url, 'event_types': ['lab.updated'],
        }, format='json').status_code == 400
    assert 'secret' not in client.get(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').data
    assert subscription.secret not in client.get('/api/v1/webhooks/subscriptions/').content.decode()
    alien = WebhookSubscription.objects.create(organization=other, url=subscription.url, event_types=['lab.updated'])
    assert client.get(f'/api/v1/webhooks/subscriptions/{alien.pk}/deliveries/').status_code == 404
    assert client.patch(f'/api/v1/webhooks/subscriptions/{alien.pk}/', {'active': False}, format='json').status_code == 404
    assert client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/', {'organization': other.pk}, format='json').status_code == 400
    assert client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/', {'active': False}, format='json').status_code == 200


def test_every_egress_change_names_an_actor_a_destination_and_an_organization(setup):
    """The generic audit row says an egress config changed, not what it became.

    Without this, an admin could point an organization's patient events at a
    host of their choosing, leave it for a week, delete the subscription, and
    leave nothing behind saying where they had gone.
    """
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)

    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        created = client.post('/api/v1/webhooks/subscriptions/', {
            'organization': org.pk, 'url': 'https://first.example/events',
            'event_types': ['lab.updated'],
        }, format='json')
    assert created.status_code == 201
    pk = created.data['id']

    change = WebhookSubscriptionChange.objects.get(subscription_pk=pk)
    assert change.action == WebhookSubscriptionChange.ACTION_CREATE
    assert (change.url_before, change.url_after) == ('', 'https://first.example/events')
    assert change.actor_id == str(user.pk) and change.actor_email == user.email
    assert change.organization_slug == org.slug
    assert change.event_types_after == ['lab.updated']

    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        assert client.patch(f'/api/v1/webhooks/subscriptions/{pk}/', {
            'url': 'https://second.example/collect', 'event_types': ['patient.changed'],
        }, format='json').status_code == 200

    updated = WebhookSubscriptionChange.objects.filter(
        subscription_pk=pk, action=WebhookSubscriptionChange.ACTION_UPDATE).get()
    # The destination it left is the half a PATCH used to overwrite with no trace.
    assert updated.url_before == 'https://first.example/events'
    assert updated.url_after == 'https://second.example/collect'
    assert updated.event_types_before == ['lab.updated']
    assert updated.event_types_after == ['patient.changed']

    assert client.delete(f'/api/v1/webhooks/subscriptions/{pk}/').status_code == 204
    removed = WebhookSubscriptionChange.objects.filter(
        subscription_pk=pk, action=WebhookSubscriptionChange.ACTION_DELETE).get()
    assert removed.url_before == 'https://second.example/collect'
    assert removed.url_after == ''
    assert removed.actor_id == str(user.pk)


def test_a_removed_subscription_keeps_its_history_and_accepts_no_writes(setup):
    org, other, person, user, subscription = setup
    delivery = WebhookDelivery.objects.create(
        subscription=subscription, payload={'id': 'e1', 'type': 'lab.updated'},
        destination_url='https://subscriber.example/events',
    )
    client = APIClient()
    client.force_login(user)

    assert client.delete(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 204

    subscription.refresh_from_db()
    assert subscription.deleted_at is not None and subscription.active is False
    # The cascade used to take this row with the subscription, which is exactly
    # the record of where the organization's events had been going.
    assert WebhookDelivery.objects.filter(pk=delivery.pk).exists()

    # Reads still reach it; every write is gone.
    assert client.get(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 200
    history = client.get(f'/api/v1/webhooks/subscriptions/{subscription.pk}/deliveries/')
    assert history.status_code == 200
    rows = history.data['results'] if isinstance(history.data, dict) else history.data
    assert rows[0]['destination_host'] == 'subscriber.example'
    # The host, not the address: for some receivers the path is the credential.
    assert 'destination_url' not in rows[0]
    assert client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                        {'active': True}, format='json').status_code == 404
    assert client.delete(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 404


def test_a_removed_subscription_leaves_the_listing_but_stays_retrievable(setup):
    """Removal still means it stops appearing, which is what a client expects.

    The history has to stay reachable — that is the point of not deleting the
    row — but a collection route that silently starts returning every
    subscription an organization has ever removed is a different endpoint than
    the one clients were written against.
    """
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    assert client.delete(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 204

    def ids(response):
        rows = response.data['results'] if isinstance(response.data, dict) else response.data
        return {row['id'] for row in rows}

    assert subscription.pk not in ids(client.get('/api/v1/webhooks/subscriptions/'))
    assert subscription.pk in ids(
        client.get('/api/v1/webhooks/subscriptions/?include_removed=true'))
    assert client.get(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 200
    # A flag it cannot read is an error, not a silent "no": the one route to a
    # removed subscription's history should not vanish on a spelling.
    assert client.get('/api/v1/webhooks/subscriptions/?include_removed=maybe').status_code == 400
    # And the flag is a read; it cannot reopen a removed subscription to a write.
    assert client.patch(
        f'/api/v1/webhooks/subscriptions/{subscription.pk}/?include_removed=true',
        {'active': True}, format='json').status_code == 404
    # And it is discoverable: a generated client, or a reviewer reading the
    # schema, should not have to find this route in prose.
    from drf_spectacular.generators import SchemaGenerator
    from patient_portal.api import v1_urls
    schema = SchemaGenerator(patterns=v1_urls.urlpatterns).get_schema(public=True)
    listing = schema['paths']['/webhooks/subscriptions/']['get']
    assert any(parameter['name'] == 'include_removed'
               for parameter in listing.get('parameters', []))


def test_a_removed_subscription_receives_no_further_events(setup):
    """Both readers of "removed" agree, and neither leans on `active`.

    The API clears `active` alongside the mark, but a shell fix or a data
    migration that marks only `deleted_at` must not keep flushing queued PHI to
    a destination someone removed — so the mark alone is what is tested here.
    """
    org, other, person, user, subscription = setup
    queued = WebhookDelivery.objects.create(
        subscription=subscription, payload={'id': 'e1', 'type': 'lab.updated'},
        destination_url=subscription.url,
    )
    subscription.deleted_at = timezone.now()
    subscription.save(update_fields=['deleted_at'])

    with patch('patient_portal.webhooks.enqueue_delivery'):
        publish_event(org.pk, 'lab.updated', {'person_id': person.person_id})
    assert WebhookDelivery.objects.count() == 1  # nothing new

    with patch('patient_portal.tasks.send_webhook') as send:
        deliver_webhook(str(queued.pk))
        send.assert_not_called()
    queued.refresh_from_db()
    assert queued.status == 'cancelled'


def test_a_delivery_is_addressed_when_it_is_queued_and_sent_there(setup):
    """The row carries its own address, and the attempt uses it.

    Reading the destination at send time would let a PATCH move PHI that was
    already queued, silently. The row is addressed once, which is both what
    stops that and what makes the mismatch detectable when the subscription
    later moves.
    """
    org, other, person, user, subscription = setup
    with patch('patient_portal.webhooks.enqueue_delivery'):
        publish_event(org.pk, 'lab.updated', {'person_id': person.person_id})
    delivery = WebhookDelivery.objects.get()
    assert delivery.destination_url == 'https://subscriber.example/events'

    sent = []
    with patch('patient_portal.tasks.send_webhook',
               side_effect=lambda url, *args, **kwargs: sent.append(url) or 200):
        deliver_webhook(str(delivery.pk))
    delivery.refresh_from_db()
    assert sent == ['https://subscriber.example/events']
    assert delivery.status == 'delivered'

    # An event published after a change is addressed to the new destination.
    WebhookSubscription.objects.filter(pk=subscription.pk).update(
        url='https://moved.example/events')
    with patch('patient_portal.webhooks.enqueue_delivery'):
        publish_event(org.pk, 'lab.updated', {'person_id': person.person_id})
    assert WebhookDelivery.objects.exclude(pk=delivery.pk).get().destination_url == (
        'https://moved.example/events')


def test_changing_the_url_cancels_what_was_queued_for_the_old_one(setup):
    """Changing the destination has to be a kill switch that works.

    A frozen address means a queued delivery cannot be redirected — which, on
    its own, would send the backlog to the address the admin was moving away
    from as soon as the subscription came back. Those rows are cancelled
    instead: nothing goes to the old address, nothing is redirected to the new
    one, and each row keeps saying where it had been addressed.
    """
    org, other, person, user, subscription = setup
    with patch('patient_portal.webhooks.enqueue_delivery'):
        publish_event(org.pk, 'lab.updated', {'person_id': person.person_id})
    queued = WebhookDelivery.objects.get()

    client = APIClient()
    client.force_login(user)
    assert client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                        {'url': 'https://corrected.example/events'},
                        format='json').status_code == 200

    queued.refresh_from_db()
    assert queued.status == 'cancelled'
    assert queued.destination_url == 'https://subscriber.example/events'

    with patch('patient_portal.tasks.send_webhook') as send:
        deliver_webhook(str(queued.pk))
        send.assert_not_called()

    # A delivery already addressed to the new URL is untouched.
    with patch('patient_portal.webhooks.enqueue_delivery'):
        publish_event(org.pk, 'lab.updated', {'person_id': person.person_id})
    fresh = WebhookDelivery.objects.exclude(pk=queued.pk).get()
    assert fresh.status == 'pending'
    assert fresh.destination_url == 'https://corrected.example/events'


def test_no_attempt_goes_to_an_address_the_organization_has_left(setup):
    """Cancelling at the moment of the change cannot reach every row.

    A publisher that read the subscription just before the change still inserts
    a delivery addressed to the old URL, and a row a worker is holding has
    passed the cancellation already. Every attempt therefore checks the address
    it was frozen for against the one the organization designates now.
    """
    org, other, person, user, subscription = setup
    with patch('patient_portal.webhooks.enqueue_delivery'):
        publish_event(org.pk, 'lab.updated', {'person_id': person.person_id})
    delivery = WebhookDelivery.objects.get()

    # The row is untouched by the cancellation — as if it had been inserted a
    # moment after it, or been in a worker's hands during it.
    WebhookSubscription.objects.filter(pk=subscription.pk).update(
        url='https://corrected.example/events')
    assert WebhookDelivery.objects.get(pk=delivery.pk).status == 'pending'

    with patch('patient_portal.tasks.send_webhook') as send:
        deliver_webhook(str(delivery.pk))
        send.assert_not_called()

    delivery.refresh_from_db()
    assert delivery.status == 'cancelled'
    assert delivery.attempts == 0  # it never became an attempt


def test_a_rolled_back_change_writes_no_audit_row_claiming_it_happened(setup):
    """The signed trail must not assert a change the database refused.

    A Python attribute is not rolled back, so setting it while the transaction
    is still open would leave a chained audit row pointing at a change row that
    does not exist — a contradiction between the two trails with nobody having
    tampered.
    """
    from patient_portal.models import AuditEvent

    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    before = AuditEvent.objects.count()

    with patch('patient_portal.api.webhook_views.record_subscription_change',
               side_effect=RuntimeError('database said no')):
        with pytest.raises(RuntimeError):
            client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                         {'url': 'https://corrected.example/events'}, format='json')

    subscription.refresh_from_db()
    assert subscription.url == 'https://subscriber.example/events'
    assert not WebhookSubscriptionChange.objects.exists()
    # The request is still audited — every request is. What it must not do is
    # carry the destination of a change that never happened.
    audited = AuditEvent.objects.latest('timestamp')
    assert AuditEvent.objects.count() == before + 1
    assert audited.detail is None


@pytest.mark.django_db(transaction=True)
def test_an_egress_change_is_also_written_to_the_signed_audit_row(setup):
    """The change table is the index; the chained audit row is the evidence.

    Transactional, because the detail is handed to the middleware on commit —
    which is the point: a change the database refuses leaves no audit row
    asserting it did happen.
    """
    from patient_portal.models import AuditEvent

    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    assert client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                        {'url': 'https://corrected.example/events'},
                        format='json').status_code == 200

    audited = AuditEvent.objects.filter(path__contains='/webhooks/subscriptions/',
                                        method='PATCH').latest('timestamp')
    assert audited.detail['host_before'] == 'subscriber.example'
    assert audited.detail['host_after'] == 'corrected.example'
    assert audited.detail['organization'] == org.slug
    assert audited.detail['action'] == 'update'
    # The path is withheld: audit rows go to stdout and are readable by any
    # service token, a wider audience than the admins who may configure egress,
    # and for some receivers the path is the credential.
    assert 'events' not in json.dumps(audited.detail)
    # The digest still binds the exact address, so the two trails can be
    # compared without either carrying it.
    change = WebhookSubscriptionChange.objects.get(pk=audited.detail['webhook_subscription_change'])
    assert audited.detail['url_after_digest'] == hashlib.sha256(change.url_after.encode()).hexdigest()
    # Signed and chained, so rewriting either trail contradicts the other.
    assert audited.signature and audited.signature == audited.compute_signature()


def test_a_second_delete_is_a_404(setup):
    """The write queryset hides it; the lock below is the second line."""
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    WebhookSubscription.objects.filter(pk=subscription.pk).update(
        deleted_at=timezone.now(), active=False)
    assert client.delete(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 404


def test_the_lock_refuses_a_row_removed_between_load_and_write(setup):
    """The window the write queryset cannot close.

    `get_object()` reads before the transaction; by the time the view writes,
    another request may have removed the row. Called directly because that is
    the only way to stand between the two.
    """
    from rest_framework.exceptions import NotFound

    from patient_portal.api.webhook_views import WebhookSubscriptionViewSet

    org, other, person, user, subscription = setup
    view = WebhookSubscriptionViewSet()
    assert view._lock_live(subscription.pk).pk == subscription.pk

    WebhookSubscription.objects.filter(pk=subscription.pk).update(
        deleted_at=timezone.now(), active=False)
    with pytest.raises(NotFound):
        view._lock_live(subscription.pk)


def test_a_delivery_written_before_the_column_existed_still_sends(setup):
    """The fallback for rows that predate the frozen address."""
    org, other, person, user, subscription = setup
    legacy = WebhookDelivery.objects.create(
        subscription=subscription, payload={'id': 'old', 'type': 'lab.updated'})
    assert legacy.destination_url == ''

    sent = []
    with patch('patient_portal.tasks.send_webhook',
               side_effect=lambda url, *args, **kwargs: sent.append(url) or 200):
        deliver_webhook(str(legacy.pk))
    assert sent == ['https://subscriber.example/events']


def test_the_migration_freezes_only_the_destinations_still_in_flight(setup):
    """Rows already queued when 0023 lands need an address too.

    Without one the worker falls back to the subscription's mutable URL, and a
    PATCH would redirect patient events queued before the deploy — the thing
    freezing the address exists to prevent. A terminal row gets nothing: the
    subscription's URL today is a guess about where that one went, and a guess
    written into an egress record is worse than an empty column.
    """
    import importlib

    from django.apps import apps as django_apps

    org, other, person, user, subscription = setup
    queued = WebhookDelivery.objects.create(
        subscription=subscription, payload={'id': 'q', 'type': 'lab.updated'})
    done = WebhookDelivery.objects.create(
        subscription=subscription, payload={'id': 'd', 'type': 'lab.updated'},
        status='delivered')
    assert queued.destination_url == '' and done.destination_url == ''

    migration = importlib.import_module(
        'patient_portal.migrations.0023_webhookdelivery_destination_url_and_more')
    migration.freeze_queued_destinations(django_apps, None)

    queued.refresh_from_db()
    done.refresh_from_db()
    assert queued.destination_url == 'https://subscriber.example/events'
    assert done.destination_url == ''


def test_a_patch_cannot_undo_a_delete_committed_while_it_was_in_flight(setup):
    """DRF holds an instance loaded before the transaction and writes it whole.

    A DELETE committing in between would be rolled back by that write —
    `deleted_at` and `active` restored from the stale copy — resurrecting a
    subscription someone removed, and still receiving events.
    """
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)

    def delete_it(value):
        # Runs during is_valid(), which is after the view has loaded the
        # instance and before it saves: exactly the window the lock closes.
        WebhookSubscription.objects.filter(pk=subscription.pk).update(
            deleted_at=timezone.now(), active=False)

    with patch('patient_portal.api.webhook_views.validate_webhook_url', side_effect=delete_it):
        response = client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                                {'url': 'https://elsewhere.example/collect'}, format='json')

    assert response.status_code == 404, response.data
    subscription.refresh_from_db()
    assert subscription.deleted_at is not None and subscription.active is False
    assert subscription.url == 'https://subscriber.example/events'


def test_a_change_record_cannot_be_rewritten_or_removed(setup):
    org, other, person, user, subscription = setup
    from patient_portal.webhooks import record_subscription_change

    record_subscription_change(subscription, WebhookSubscriptionChange.ACTION_CREATE, user)
    change = WebhookSubscriptionChange.objects.get()

    change.url_after = 'https://rewritten.example/'
    with pytest.raises(DjangoValidationError):
        change.save()
    with pytest.raises(DjangoValidationError):
        change.delete()
    change.refresh_from_db()
    assert change.url_after == subscription.url


def test_an_organization_cannot_hold_unbounded_destinations(setup, settings):
    """Each subscription adds an insert to every clinical write of that org.

    The cost lands inside the transaction of the write that triggered it, so an
    unbounded list is an organization's own admin multiplying the cost of that
    organization's writes.
    """
    settings.WEBHOOK_MAX_SUBSCRIPTIONS_PER_ORG = 2
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)

    def create(host):
        return client.post('/api/v1/webhooks/subscriptions/', {
            'organization': org.pk, 'url': f'https://{host}.example/events',
            'event_types': ['lab.updated'],
        }, format='json')

    assert create('second').status_code == 201
    refused = create('third')
    assert refused.status_code == 400
    assert 'at most 2' in str(refused.data)

    # A removed subscription receives nothing, so it does not hold a slot.
    assert client.delete(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 204
    assert create('third').status_code == 201


def test_a_reader_who_cannot_change_the_destination_does_not_see_it(trusted_professional):
    """For a Slack- or Zapier-shaped receiver the path is the credential.

    Reads follow the wider admin reach on purpose — a trust-derived
    professional should be able to see that an organization sends events, and
    where. Holding the credential is a different thing.
    """
    client, org, other, professional = trusted_professional
    subscription = WebhookSubscription.objects.get(organization=org)

    listed = client.get(f'/api/v1/webhooks/subscriptions/{subscription.pk}/')
    assert listed.status_code == 200
    assert listed.data['url'] == 'https://subscriber.example/***'
    assert '/events' not in client.get('/api/v1/webhooks/subscriptions/').content.decode()


def test_a_direct_admin_still_sees_the_whole_destination(setup):
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    assert client.get(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').data['url'] == (
        'https://subscriber.example/events')


def test_rotating_the_secret_keeps_the_destination_and_records_who(setup):
    """Rotation used to mean create a new subscription and delete the old one.

    That moved the destination for no reason and, before removal became a mark,
    destroyed the old subscription's delivery history along with it.
    """
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    original = subscription.secret

    response = client.post(f'/api/v1/webhooks/subscriptions/{subscription.pk}/rotate-secret/')
    assert response.status_code == 200
    assert len(response.data['secret']) >= 32
    assert response.data['secret'] != original
    assert response['Cache-Control'] == 'no-store'

    subscription.refresh_from_db()
    assert subscription.secret == response.data['secret']
    assert subscription.url == 'https://subscriber.example/events'

    recorded = WebhookSubscriptionChange.objects.get(
        action=WebhookSubscriptionChange.ACTION_ROTATE)
    assert recorded.actor_id == str(user.pk)
    assert recorded.url_before == recorded.url_after == subscription.url
    # The secret itself is never written to either trail.
    assert original not in str(recorded.__dict__)


def test_rotation_needs_the_same_authority_as_any_other_egress_write(trusted_professional, setup):
    client, org, other, professional = trusted_professional
    subscription = WebhookSubscription.objects.get(organization=org)
    before = subscription.secret
    assert client.post(
        f'/api/v1/webhooks/subscriptions/{subscription.pk}/rotate-secret/',
    ).status_code in (403, 404)
    subscription.refresh_from_db()
    assert subscription.secret == before


@pytest.mark.parametrize('resource_id', [
    'lab report: elevated CRP, see notes', 'has space', 'ключ', 'a' * 129, '',
])
def test_a_relayed_resource_id_must_be_an_identifier(setup, resource_id):
    """Whatever a partner puts here, this deployment forwards under its own
    signature — so it cannot be a free-text channel for clinical detail."""
    response = inbound(payload={
        'id': f'event-{abs(hash(resource_id))}', 'type': 'lab.updated',
        'data': {'person_id': 420001, 'resource_id': resource_id},
    })
    assert response.status_code == 400
    assert not WebhookDelivery.objects.exists()


def test_a_well_formed_resource_id_is_still_relayed(setup):
    assert inbound(payload={
        'id': 'event-ok', 'type': 'lab.updated',
        'data': {'person_id': 420001, 'resource_id': 'Observation/abc-123'},
    }).status_code == 202
    delivered = WebhookDelivery.objects.get()
    assert delivered.payload['data']['resource_id'] == 'Observation/abc-123'


def test_patient_and_expired_admin_cannot_manage_subscriptions(setup):
    org, other, person, user, subscription = setup
    client = APIClient()
    assert client.get('/api/v1/webhooks/subscriptions/').status_code in (401, 403)
    client.force_authenticate(user)
    GroupAccess.objects.filter(identity=user).update(role='patient')
    assert client.get('/api/v1/webhooks/subscriptions/').status_code == 403
    GroupAccess.objects.filter(identity=user).update(role='org_admin', expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get('/api/v1/webhooks/subscriptions/').status_code == 403


def test_subscription_url_errors_never_expose_exception_details(setup):
    client = APIClient()
    client.force_login(setup[3])
    # Patch the shared implementation, not the view-local alias: DRF copies the
    # model-field validator onto the serializer field, so the model path is the
    # one that produces this error and the alias is never reached on a failure.
    with patch('patient_portal.webhooks.validate_webhook_url', side_effect=ValueError('private resolver details')):
        response = client.post('/api/v1/webhooks/subscriptions/', {
            'organization': setup[0].pk,
            'url': 'https://subscriber.example/events',
            'event_types': ['lab.updated'],
        }, format='json')
    assert response.status_code == 400
    assert str(response.data['url'][0]) == 'Webhook URL must resolve only to public HTTPS addresses on port 443.'
    assert 'private resolver details' not in response.content.decode()


def test_rollback_discards_event_and_queue_dispatch(setup, django_capture_on_commit_callbacks):
    with patch('patient_portal.webhooks.enqueue_delivery') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError), transaction.atomic():
                publish_event(setup[0].pk, 'patient.changed', {'person_id': setup[2].pk})
                raise RuntimeError('rollback')
        enqueue.assert_not_called()
    assert not WebhookDelivery.objects.exists()


def test_model_changes_queue_only_after_commit(setup, django_capture_on_commit_callbacks):
    with patch('patient_portal.webhooks.enqueue_delivery') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            PatientDocument.objects.create(person=setup[2], doc_type='OTHER')
            enqueue.assert_not_called()
            assert WebhookDelivery.objects.get().payload['type'] == 'document.received'
        enqueue.assert_called_once()


def test_lab_sync_bulk_write_emits_one_notification(setup, django_capture_on_commit_callbacks):
    org, other, person, user, subscription = setup
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    client = APIClient()
    client.force_authenticate(user)
    with patch('patient_portal.webhooks.enqueue_delivery') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post('/api/lab-results/sync/', {
                'person_id': person.pk,
                'measurements': [{'test_name': 'Webhook lab', 'value': '42', 'measured_at': '2026-09-12'}],
            }, format='json')
            assert response.status_code == 201, response.data
            enqueue.assert_not_called()
        enqueue.assert_called_once()
    delivery = WebhookDelivery.objects.get()
    assert delivery.payload['type'] == 'lab.updated'
    assert delivery.payload['data']['count'] == 1


def test_tp53_reconciliation_notifies_only_when_it_applies_a_change(setup):
    """`reconcile_tp53_cache --apply` writes with QuerySet.update(), which no
    post_save receiver sees. Without an explicit publish the subscriber keeps a
    stale tp53_disruption until some unrelated save happens to touch the row."""
    from io import StringIO

    from django.core.management import call_command

    org, other, person, user, subscription = setup
    # No source findings, so the reconciled value is None; the stored False is
    # what the command has to correct, and correcting it is the change event.
    record = PatientRecord.objects.get(person=person)
    PatientRecord.objects.filter(pk=record.pk).update(tp53_disruption=False)

    WebhookDelivery.objects.all().delete()
    call_command('reconcile_tp53_cache', person_id=person.pk, stdout=StringIO())
    assert not WebhookDelivery.objects.exists(), 'preview changes nothing and must stay silent'

    call_command('reconcile_tp53_cache', person_id=person.pk, apply=True, stdout=StringIO())
    delivery = WebhookDelivery.objects.get()
    assert delivery.subscription_id == subscription.pk
    assert delivery.payload['type'] == 'patient.changed'
    assert delivery.payload['data'] == {
        'person_id': person.pk, 'resource_id': str(record.pk),
        'resource_type': 'omop_core.patientrecord', 'operation': 'saved',
    }
    record.refresh_from_db()
    assert record.tp53_disruption is None

    WebhookDelivery.objects.all().delete()
    call_command('reconcile_tp53_cache', person_id=person.pk, apply=True, stdout=StringIO())
    assert not WebhookDelivery.objects.exists(), 'a no-op re-run must not notify again'


def test_tp53_reconciliation_holds_pending_edits_without_notifying(setup):
    from io import StringIO

    from django.core.management import call_command

    org, other, person, user, subscription = setup
    PatientRecord.objects.filter(person=person).update(
        tp53_disruption=False, user_edited_fields=['tp53_disruption'])
    WebhookDelivery.objects.all().delete()
    call_command('reconcile_tp53_cache', person_id=person.pk, apply=True, stdout=StringIO())
    assert not WebhookDelivery.objects.exists()
    assert PatientRecord.objects.get(person=person).tp53_disruption is False


_SYNC_BUNDLE = {
    'resourceType': 'Bundle', 'type': 'collection',
    'entry': [
        {'resource': {'resourceType': 'Patient', 'id': 'p1'}},
        {'resource': {
            'resourceType': 'Observation', 'subject': {'reference': 'Patient/p1'},
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '718-7',
                                 'display': 'Hemoglobin'}]},
            'effectiveDateTime': '2026-02-01',
            'valueQuantity': {'value': 13.2, 'unit': 'g/dL'},
        }},
        {'resource': {
            'resourceType': 'Condition', 'subject': {'reference': 'Patient/p1'},
            'code': {'coding': [{'system': 'http://snomed.info/sct', 'code': '254837009'}],
                     'text': 'Malignant neoplasm of breast'},
            'onsetDateTime': '2025-11-15',
        }},
    ],
}


@pytest.fixture
def sync_client(setup, settings):
    settings.SERVICE_AUTH_SCOPES = 'patient/*.write'
    service_user = Identity.objects.create(issuer='urn:service', sub='webhook-fhir-sync')
    service_user.set_unusable_password()
    service_user.save(update_fields=['password'])
    client = APIClient()
    client.force_authenticate(user=service_user, token='service-token')
    return client


def _sync(client, person, bundle=None):
    return client.post('/api/v1/fhir/sync/', {
        'person_id': person.pk, 'bundle': bundle or _SYNC_BUNDLE,
    }, format='json')


def test_fhir_sync_notifies_once_per_table_and_stays_quiet_when_idempotent(setup, sync_client):
    """The sync view writes with bulk_create, which fires no signal, so these
    ingested rows reach a subscriber only if the path publishes explicitly."""
    org, other, person, user, subscription = setup
    WebhookDelivery.objects.all().delete()

    assert _sync(sync_client, person).status_code == 201
    by_type = {}
    for delivery in WebhookDelivery.objects.all():
        by_type.setdefault(delivery.payload['type'], []).append(delivery.payload['data'])
    assert set(by_type) == {'lab.updated', 'patient.changed'}
    assert by_type['lab.updated'][0]['resource_type'] == 'omop_core.measurement'
    assert by_type['lab.updated'][0]['count'] == 1
    assert [d['resource_type'] for d in by_type['patient.changed']] == ['omop_core.conditionoccurrence']
    assert by_type['patient.changed'][0]['count'] == 1

    WebhookDelivery.objects.all().delete()
    assert _sync(sync_client, person).status_code == 201
    assert not WebhookDelivery.objects.exists(), 'an idempotent re-sync changes nothing and must stay silent'


def test_fhir_sync_emits_one_event_per_table_across_ingest_helpers(setup, sync_client):
    """Measurement is written by both the discrete and the daily-rollup helper,
    and DrugExposure by both medications and immunizations. Publishing inside
    each helper would split one bundle into two events per table, each with a
    partial count."""
    org, other, person, user, subscription = setup
    bundle = {
        'resourceType': 'Bundle', 'type': 'collection',
        'entry': [
            {'resource': {'resourceType': 'Patient', 'id': 'p1'}},
            # discrete lab
            {'resource': {
                'resourceType': 'Observation', 'subject': {'reference': 'Patient/p1'},
                'code': {'coding': [{'system': 'http://loinc.org', 'code': '718-7',
                                     'display': 'Hemoglobin'}]},
                'effectiveDateTime': '2026-02-01',
                'valueQuantity': {'value': 13.2, 'unit': 'g/dL'},
            }},
            # daily rollup — same table, different helper
            {'resource': {
                'resourceType': 'Observation', 'subject': {'reference': 'Patient/p1'},
                'extension': [{'url': AGGREGATION_EXT_URL, 'valueCode': 'daily'}],
                'code': {'coding': [{'system': 'http://loinc.org', 'code': '55423-8',
                                     'display': 'Step count'}]},
                'effectivePeriod': {'start': '2026-02-02T00:00:00Z',
                                    'end': '2026-02-02T23:59:59Z'},
                'valueQuantity': {'value': 8000, 'unit': 'steps'},
            }},
            # medication and immunization — both DrugExposure, different helpers
            {'resource': {
                'resourceType': 'MedicationStatement', 'subject': {'reference': 'Patient/p1'},
                'medicationCodeableConcept': {'text': 'AC-T'},
                'effectivePeriod': {'start': '2025-12-01'},
            }},
            {'resource': {
                'resourceType': 'Immunization', 'patient': {'reference': 'Patient/p1'},
                'status': 'completed',
                'vaccineCode': {'text': 'Influenza vaccine'},
                'occurrenceDateTime': '2025-10-01',
            }},
        ],
    }
    WebhookDelivery.objects.all().delete()
    assert _sync(sync_client, person, bundle).status_code == 201

    per_table = {}
    for delivery in WebhookDelivery.objects.all():
        data = delivery.payload['data']
        per_table.setdefault(data['resource_type'], []).append(data['count'])
    assert all(len(counts) == 1 for counts in per_table.values()), per_table
    assert per_table.get('omop_core.measurement') == [2], per_table
    assert per_table.get('omop_core.drugexposure') == [2], per_table


def test_fhir_sync_counts_a_twice_matched_rollup_row_once(setup, sync_client):
    """Two bundle entries under different display text can resolve to the same
    stored daily row — the rollup path matches on source value OR concept. It
    is then saved twice but changed once, so the count must not report two."""
    from omop_core.models import Measurement
    from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

    # Without a resolvable concept every display string is its own row and the
    # OR-match never fires, so the concept has to exist for this to be the
    # scenario it claims to be.
    ConceptFactory(
        concept_name='Step count', concept_code='55423-8', standard_concept='S',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC'),
        domain=DomainFactory(domain_id='Measurement', domain_name='Measurement'),
    )

    org, other, person, user, subscription = setup

    def rollup(display, value):
        return {'resource': {
            'resourceType': 'Observation', 'subject': {'reference': 'Patient/p1'},
            'extension': [{'url': AGGREGATION_EXT_URL, 'valueCode': 'daily'}],
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '55423-8',
                                 'display': display}]},
            'effectivePeriod': {'start': '2026-02-02T00:00:00Z',
                                'end': '2026-02-02T23:59:59Z'},
            'valueQuantity': {'value': value, 'unit': 'steps'},
        }}

    seed = {'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': {'resourceType': 'Patient', 'id': 'p1'}},
                      rollup('Step count', 8000)]}
    assert _sync(sync_client, person, seed).status_code == 201
    stored = Measurement.objects.filter(person=person).count()

    # Same concept and day, two different display strings, both differing from
    # what is stored: each resolves to the one existing row.
    again = {'resourceType': 'Bundle', 'type': 'collection',
             'entry': [{'resource': {'resourceType': 'Patient', 'id': 'p1'}},
                       rollup('Steps', 9000), rollup('Step Count (daily)', 9500)]}
    WebhookDelivery.objects.all().delete()
    assert _sync(sync_client, person, again).status_code == 201

    measurement_events = [d.payload['data'] for d in WebhookDelivery.objects.all()
                          if d.payload['data']['resource_type'] == 'omop_core.measurement']
    assert Measurement.objects.filter(person=person).count() == stored, 'no new row: both entries match the stored one'
    assert len(measurement_events) == 1, measurement_events
    assert measurement_events[0]['count'] == 1, measurement_events


def test_fhir_sync_collapse_of_stacked_duplicates_is_not_a_patient_event(setup, sync_client):
    """Collapsing internal stacked rows is bookkeeping. Before the fix it was
    the only thing the sync path notified about: one event per deleted
    duplicate, and none for the rows actually ingested."""
    from omop_core.models import ConditionOccurrence

    org, other, person, user, subscription = setup
    assert _sync(sync_client, person).status_code == 201
    original = ConditionOccurrence.objects.get(person=person)

    duplicate = ConditionOccurrence.objects.get(pk=original.pk)
    duplicate.pk = None
    duplicate.condition_occurrence_id = original.condition_occurrence_id + 10_000
    duplicate.save()
    assert ConditionOccurrence.objects.filter(person=person).count() == 2

    WebhookDelivery.objects.all().delete()
    assert _sync(sync_client, person).status_code == 201

    assert ConditionOccurrence.objects.filter(person=person).count() == 1, 'the duplicate should be collapsed'
    payloads = [d.payload for d in WebhookDelivery.objects.all()]
    assert all(p['data'].get('operation') != 'deleted' for p in payloads), payloads
    assert len(payloads) == 1 and payloads[0]['data']['count'] == 1


def test_write_boundary_is_only_taken_when_webhooks_are_on(settings):
    """The transaction is not free — a single-row POST runs the patient-record
    derivation inside it — and it buys nothing with no outbox to protect, so
    a deployment with webhooks off keeps the behaviour that shipped before."""
    from contextlib import nullcontext

    from patient_portal.api.views import _webhook_write_atomic

    settings.WEBHOOKS_ENABLED = False
    assert isinstance(_webhook_write_atomic(), nullcontext)
    settings.WEBHOOKS_ENABLED = True
    assert not isinstance(_webhook_write_atomic(), nullcontext)


def test_bulk_publish_honours_the_suppressor(setup):
    from patient_portal.webhooks import publish_patient_bulk_change, suppress_webhook_events

    org, other, person, user, subscription = setup
    WebhookDelivery.objects.all().delete()
    with suppress_webhook_events():
        publish_patient_bulk_change(person.pk, 'measurement', 3)
    assert not WebhookDelivery.objects.exists()
    publish_patient_bulk_change(person.pk, 'measurement', 3)
    assert WebhookDelivery.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_clinical_write_rolls_back_when_the_outbox_insert_fails(setup):
    """The outbox row has to commit with the row it describes.

    Without a transaction around the DRF write the document is already
    committed when the receiver runs, so a failing insert would leave a
    persisted document that no subscriber ever hears about.
    """
    org, other, person, user, subscription = setup
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    client = APIClient()
    client.force_authenticate(user)

    before = PatientDocument.objects.count()
    with patch('patient_portal.webhooks.WebhookDelivery.objects.create',
               side_effect=RuntimeError('outbox unavailable')):
        with pytest.raises(RuntimeError):
            client.post('/api/v1/documents/', {
                'person': person.pk, 'doc_type': 'OTHER', 'title': 'Atomic boundary',
            }, format='json')

    assert PatientDocument.objects.count() == before, 'the document must not survive a failed outbox insert'
    assert not WebhookDelivery.objects.exists()

    response = client.post('/api/v1/documents/', {
        'person': person.pk, 'doc_type': 'OTHER', 'title': 'Atomic boundary',
    }, format='json')
    assert response.status_code == 201, response.data
    assert PatientDocument.objects.count() == before + 1
    assert WebhookDelivery.objects.get().payload['type'] == 'document.received'


@pytest.mark.parametrize('operation', ['update', 'delete'])
def test_bulk_changes_notify_even_when_refresh_is_skipped(setup, operation):
    user = setup[3]
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    client = APIClient()
    client.force_authenticate(user)
    response = client.post('/api/lab-results/sync/', {
        'person_id': setup[2].pk,
        'measurements': [{'test_name': 'Webhook bulk', 'value': '42', 'measured_at': '2026-09-12'}],
    }, format='json')
    assert response.status_code == 201, response.data
    measurement_id = response.data['measurement_ids'][0]
    WebhookDelivery.objects.all().delete()
    body = ({'ids': [measurement_id]} if operation == 'delete' else
            [{'measurement_id': measurement_id, 'value_as_number': 43}])
    send = client.post if operation == 'delete' else client.patch
    response = send(f'/api/v1/measurements/bulk_{operation}/?skip_refresh=true', body, format='json')
    assert response.status_code == 200, response.data
    delivery = WebhookDelivery.objects.get()
    assert delivery.payload['type'] == ('patient.changed' if operation == 'delete' else 'lab.updated')
    assert delivery.payload['data']['count'] == 1


@pytest.fixture
def delivery(setup):
    return WebhookDelivery.objects.create(subscription=setup[4], payload={'id': 'event', 'type': 'lab.updated', 'data': {}})


def test_successful_delivery_and_duplicate_task(delivery):
    with patch('patient_portal.tasks.send_webhook', return_value=204) as send:
        deliver_webhook(str(delivery.pk))
        deliver_webhook(str(delivery.pk))
        send.assert_called_once()
    delivery.refresh_from_db()
    assert delivery.status == 'delivered'
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None


@pytest.mark.parametrize('response_status', [301, 400, 401, 429, 500, 503])
def test_retry_backoff_and_five_attempt_limit(delivery, response_status, django_capture_on_commit_callbacks):
    with patch('patient_portal.tasks.send_webhook', return_value=response_status) as send:
        with patch('patient_portal.tasks.enqueue_delivery') as enqueue:
            for attempt in range(1, 6):
                with django_capture_on_commit_callbacks(execute=True):
                    deliver_webhook(str(delivery.pk))
                delivery.refresh_from_db()
                assert delivery.attempts == attempt
                assert delivery.response_status == response_status
                if attempt < 5:
                    assert delivery.status == 'retry'
                    assert enqueue.call_args.kwargs['countdown'] == 30 * 2 ** (attempt - 1)
                    deliver_webhook(str(delivery.pk))
                    assert send.call_count == attempt
                    WebhookDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
            assert delivery.status == 'dead_letter'
            deliver_webhook(str(delivery.pk))
            assert send.call_count == 5
            assert enqueue.call_count == 4


def test_network_errors_are_redacted_and_retried(delivery):
    with patch('patient_portal.tasks.send_webhook', side_effect=OSError('secret in a URL')):
        deliver_webhook(str(delivery.pk))
    delivery.refresh_from_db()
    assert delivery.status == 'retry'
    assert delivery.error == 'connection_error'


def test_disabled_subscription_is_not_delivered(delivery):
    WebhookSubscription.objects.filter(pk=delivery.subscription_id).update(active=False)
    with patch('patient_portal.tasks.send_webhook') as send:
        deliver_webhook(str(delivery.pk))
        send.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == 'cancelled'
    assert delivery.attempts == 0


def test_broker_failure_retains_outbox_for_sweep(delivery, settings):
    settings.CELERY_BROKER_URL = 'redis://unavailable'
    with patch('patient_portal.tasks.deliver_webhook.apply_async', side_effect=OSError):
        enqueue_delivery(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.status == 'pending'
    with patch('patient_portal.tasks.enqueue_delivery') as enqueue:
        dispatch_pending_webhooks()
        enqueue.assert_called_once_with(delivery.pk)


def test_lost_worker_lease_recovers_with_attempt_limit(delivery):
    WebhookDelivery.objects.filter(pk=delivery.pk).update(status='sending', attempts=5)
    with patch('patient_portal.tasks.send_webhook') as send:
        deliver_webhook(str(delivery.pk))
        send.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == 'dead_letter'


@pytest.mark.parametrize('url', [
    'http://example.com/', 'https://user:password@example.com/',
    'https://example.com:8080/', 'https://example.com/#fragment',
])
def test_unsafe_url_schemes_and_credentials(url):
    with pytest.raises(ValueError):
        resolve_webhook_url(url)


@pytest.mark.parametrize('address', [
    '127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fd00::1', '::ffff:127.0.0.1',
    # IPv6 notations carrying a private IPv4 inside. `is_global` below CPython
    # 3.12.4 (CVE-2024-4032) calls the 6to4 pair globally reachable, and every
    # Render service was pinned to 3.12.0; the filter now unwraps them itself,
    # so this holds whatever interpreter the deployment rolls back to.
    '2002:7f00:1::',                              # 6to4 wrap of 127.0.0.1
    '2002:a00:5::',                               # 6to4 wrap of 10.0.0.5
    '2001:0:4136:e378:8000:63bf:f5ff:fffa',       # Teredo client 10.0.0.5
])
def test_private_destinations_rejected(address):
    with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', (address, 443))]):
        with pytest.raises(ValueError):
            resolve_webhook_url('https://subscriber.example/')


@pytest.mark.parametrize('address', ['2002:7f00:1::', '2002:a00:5::', '::ffff:127.0.0.1'])
def test_a_wrapped_private_address_is_refused_as_a_literal_url(address):
    """No DNS is involved, so this is the branch a subscription create reaches."""
    with pytest.raises(ValueError):
        resolve_webhook_url(f'https://[{address}]/events')


def test_a_public_ipv6_literal_is_still_accepted():
    """The notation is not what is being refused above; the address inside is."""
    with patch('socket.getaddrinfo', return_value=[
        (socket.AF_INET6, 1, 6, '', ('2606:4700:4700::1111', 443, 0, 0)),
    ]):
        _, addresses = resolve_webhook_url('https://[2606:4700:4700::1111]/events')
    assert addresses == ['2606:4700:4700::1111']


def test_the_filter_does_not_depend_on_the_interpreters_special_address_table():
    """The table has been wrong once. Judge the wrapped address on its own terms.

    `is_global` is forced True for every address here, which is what CPython
    3.12.0 answers for the 6to4 pair; what must refuse it is the unwrapping.
    """
    import ipaddress

    from patient_portal.webhooks import _public_address

    with patch('ipaddress.IPv6Address.is_global', new_callable=PropertyMock, return_value=True):
        assert not _public_address(ipaddress.ip_address('2002:7f00:1::'))
        assert not _public_address(ipaddress.ip_address('2002:a00:5::'))
        assert not _public_address(ipaddress.ip_address('::ffff:169.254.169.254'))
        assert _public_address(ipaddress.ip_address('2606:4700:4700::1111'))


DUAL_STACK = [
    (socket.AF_INET6, 1, 6, '', ('2606:4700:4700::1111', 443, 0, 0)),
    (socket.AF_INET, 1, 6, '', ('8.8.8.8', 443)),
]


def test_only_the_first_address_of_each_family_is_a_candidate():
    """A hostname with many records must not turn one attempt into many."""
    many = [(socket.AF_INET, 1, 6, '', (f'8.8.8.{n}', 443)) for n in range(1, 9)]
    many.append((socket.AF_INET6, 1, 6, '', ('2606:4700:4700::1111', 443, 0, 0)))
    with patch('socket.getaddrinfo', return_value=many):
        _, addresses = resolve_webhook_url('https://subscriber.example/')
    assert addresses == ['8.8.8.1', '2606:4700:4700::1111']


def test_a_dual_stack_subscriber_is_tried_over_ipv4_first():
    """Sorting the strings put '2606:...' before '8.8.8.8' and pinned IPv6.

    On a runtime without IPv6 egress that is a connection failure on every
    attempt, and the stored error is a fixed token, so nothing says why.
    """
    with patch('socket.getaddrinfo', return_value=DUAL_STACK):
        parsed, addresses = resolve_webhook_url('https://subscriber.example/')
    assert addresses == ['8.8.8.8', '2606:4700:4700::1111']


def test_every_resolved_address_is_still_validated():
    mixed = [(socket.AF_INET, 1, 6, '', ('8.8.8.8', 443)),
             (socket.AF_INET, 1, 6, '', ('127.0.0.1', 443))]
    with patch('socket.getaddrinfo', return_value=mixed):
        with pytest.raises(ValueError):
            resolve_webhook_url('https://subscriber.example/')


def test_a_refused_address_falls_back_to_the_next_validated_one():
    pools, pool_kwargs = [], []

    def make_pool(address, **kwargs):
        pool = Mock()
        if address == '8.8.8.8':
            pool.urlopen.side_effect = urllib3.exceptions.NewConnectionError(Mock(), 'no route')
        else:
            pool.urlopen.return_value = Mock(status=200)
        pools.append((address, pool))
        pool_kwargs.append(kwargs)
        return pool

    with patch('socket.getaddrinfo', return_value=DUAL_STACK):
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool', side_effect=make_pool):
            assert send_webhook('https://subscriber.example/', {'id': 'x'}, 'secret', 'delivery') == 200

    assert [address for address, _ in pools] == ['8.8.8.8', '2606:4700:4700::1111']
    for _, pool in pools:
        pool.close.assert_called_once()
    # The pinning property has to hold on the second pool too, not just the first.
    for kwargs in pool_kwargs:
        assert kwargs['server_hostname'] == 'subscriber.example'
        assert kwargs['assert_hostname'] == 'subscriber.example'
        assert kwargs['cert_reqs'] == 'CERT_REQUIRED'
        assert kwargs['retries'] is False
        assert (kwargs['timeout'].connect_timeout, kwargs['timeout'].read_timeout) == (5, 10)


def test_a_blackholed_address_times_out_and_still_falls_over():
    """urllib3 2.7.0: NewConnectionError subclasses ConnectTimeoutError.

    A route that is dropped rather than refused — the IPv6-without-egress case
    — raises the parent, so catching only NewConnectionError would not fail over
    in the very scenario the ordering change exists for.
    """
    attempted = []

    def make_pool(address, **kwargs):
        attempted.append(address)
        pool = Mock()
        if address == '8.8.8.8':
            pool.urlopen.side_effect = urllib3.exceptions.ConnectTimeoutError('timed out')
        else:
            pool.urlopen.return_value = Mock(status=204)
        return pool

    with patch('socket.getaddrinfo', return_value=DUAL_STACK):
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool', side_effect=make_pool):
            assert send_webhook('https://subscriber.example/', {'id': 'x'}, 'secret', 'd') == 204
    assert attempted == ['8.8.8.8', '2606:4700:4700::1111']


def test_a_failure_after_the_request_is_on_the_wire_is_not_retried_elsewhere():
    """A read timeout may mean the peer already acted; retrying is a resend."""
    calls = []

    def make_pool(address, **kwargs):
        calls.append(address)
        pool = Mock()
        pool.urlopen.side_effect = urllib3.exceptions.ReadTimeoutError(Mock(), '/', 'timed out')
        return pool

    with patch('socket.getaddrinfo', return_value=DUAL_STACK):
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool', side_effect=make_pool):
            with pytest.raises(urllib3.exceptions.ReadTimeoutError):
                send_webhook('https://subscriber.example/', {'id': 'x'}, 'secret', 'delivery')

    assert calls == ['8.8.8.8'], 'only the first address may be attempted'


def test_the_last_address_failing_propagates_rather_than_returning_none():
    def make_pool(address, **kwargs):
        pool = Mock()
        pool.urlopen.side_effect = urllib3.exceptions.NewConnectionError(Mock(), 'refused')
        return pool

    with patch('socket.getaddrinfo', return_value=DUAL_STACK):
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool', side_effect=make_pool):
            with pytest.raises(urllib3.exceptions.NewConnectionError):
                send_webhook('https://subscriber.example/', {'id': 'x'}, 'secret', 'delivery')


def test_transport_pins_ip_verifies_tls_signs_bytes_and_disables_redirects():
    response = Mock(status=302)
    with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', ('8.8.8.8', 443))]):
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool') as pool:
            pool.return_value.urlopen.return_value = response
            assert send_webhook('https://subscriber.example/path?a=1', {'id': 'x'}, 'secret', 'delivery') == 302
    assert pool.call_args.args == ('8.8.8.8',)
    assert pool.call_args.kwargs['server_hostname'] == 'subscriber.example'
    assert pool.call_args.kwargs['assert_hostname'] == 'subscriber.example'
    assert pool.call_args.kwargs['cert_reqs'] == 'CERT_REQUIRED'
    args, kwargs = pool.return_value.urlopen.call_args
    assert args == ('POST', '/path?a=1')
    assert kwargs['redirect'] is False
    assert kwargs['retries'] is False
    assert kwargs['preload_content'] is False
    sent_ts = kwargs['headers']['X-HealthKey-Timestamp']
    assert kwargs['headers']['X-HealthKey-Signature'] == compute_hmac_signature(
        kwargs['body'], 'secret', sent_ts)
    response.close.assert_called_once()


@pytest.mark.parametrize('url,address,hostname,host_header', [
    ('https://[2606:4700:4700::1111]/', '2606:4700:4700::1111', '2606:4700:4700::1111', '[2606:4700:4700::1111]'),
    ('https://bücher.example/', '8.8.8.8', 'xn--bcher-kva.example', 'xn--bcher-kva.example'),
])
def test_transport_normalizes_ipv6_and_idna_hosts(url, address, hostname, host_header):
    with patch('socket.getaddrinfo', return_value=[(socket.AF_INET6, 1, 6, '', (address, 443))]) as dns:
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool') as pool:
            pool.return_value.urlopen.return_value = Mock(status=200)
            send_webhook(url, {'id': 'event'}, 'secret', 'delivery')
    assert dns.call_args.args[0] == hostname
    assert pool.call_args.kwargs['server_hostname'] == hostname
    assert pool.return_value.urlopen.call_args.kwargs['headers']['Host'] == host_header


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_inbound_events(setup):
    def receive():
        close_old_connections()
        try:
            return inbound().status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: receive(), range(2)))
    assert sorted(results) == [200, 202]
    assert InboundWebhookEvent.objects.count() == 1
    assert WebhookDelivery.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_delivery_tasks_send_once(delivery):
    def run():
        close_old_connections()
        try:
            deliver_webhook(str(delivery.pk))
        finally:
            close_old_connections()

    with patch('patient_portal.tasks.send_webhook', return_value=200) as send:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: run(), range(2)))
        send.assert_called_once()
    delivery.refresh_from_db()
    assert delivery.attempts == 1


@pytest.mark.parametrize('timestamp', ['', 'abc', '１２３', '-1', '9' * 30, '0'])
def test_inbound_requires_valid_recent_timestamp(setup, timestamp):
    # Invalid timestamp need not be signed: all fail before dispatch.
    assert inbound(signature='invalid', HTTP_X_HEALTHKEY_TIMESTAMP=timestamp).status_code == 401
    assert not InboundWebhookEvent.objects.exists()


@pytest.mark.parametrize('offset', [-301, 301])
def test_correctly_signed_stale_or_future_request_rejected(setup, offset):
    assert inbound(HTTP_X_HEALTHKEY_TIMESTAMP=str(int(time.time()) + offset)).status_code == 401
    assert not WebhookDelivery.objects.exists()


def test_timestamp_is_part_of_signed_bytes(setup):
    body = encode_payload({'id': 'event-1', 'type': 'lab.updated', 'data': {'person_id': 420001}})
    timestamp = str(int(time.time()))
    signature = compute_hmac_signature(body, 'test-source-secret', str(int(timestamp) - 1))
    assert inbound(body, signature=signature, HTTP_X_HEALTHKEY_TIMESTAMP=timestamp).status_code == 401
    expected = 'sha256=' + hmac.new(b'test-source-secret', timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    assert compute_hmac_signature(body, 'test-source-secret', timestamp) == expected


def test_inbound_quota_is_per_authenticated_source(setup, settings):
    settings.WEBHOOK_INBOUND_RATE = '2/minute'
    settings.WEBHOOK_INBOUND_SOURCES['second-source'] = settings.WEBHOOK_INBOUND_SOURCES['lab-system']
    for _ in range(3):
        assert inbound(signature='invalid').status_code == 401
    assert inbound().status_code == 202
    assert inbound().status_code == 200
    response = inbound()
    assert response.status_code == 429
    assert int(response['Retry-After']) > 0
    assert inbound(source='second-source').status_code == 202


def test_subscription_validation_does_not_resolve_dns(setup):
    client = APIClient()
    client.force_login(setup[3])
    with patch('socket.getaddrinfo', side_effect=AssertionError('DNS in request')):
        result = client.post('/api/v1/webhooks/subscriptions/', {
            'organization': setup[0].pk, 'url': 'https://unresolvable.example/events',
            'event_types': ['lab.updated'],
        }, format='json')
        assert result.status_code == 201, result.data
        assert client.patch(f"/api/v1/webhooks/subscriptions/{result.data['id']}/", {
            'url': 'https://another.example/events',
        }, format='json').status_code == 200


def test_disabled_webhooks_add_no_queries(setup, settings, django_assert_num_queries):
    from django.db.models.signals import post_save
    from patient_portal.webhooks import patient_data_changed, publish_patient_bulk_change
    settings.WEBHOOKS_ENABLED = False
    with django_assert_num_queries(0), patch('patient_portal.webhooks.enqueue_delivery') as enqueue:
        patient_data_changed(PatientDocument, PatientDocument(person_id=setup[2].pk), signal=post_save)
        publish_patient_bulk_change(setup[2].pk, 'measurement', 1)
        publish_event(setup[0].pk, 'patient.changed', {})
        dispatch_pending_webhooks()
        enqueue.assert_not_called()
    assert inbound().status_code == 503


def test_broker_enqueue_disables_retry_and_result_backend(delivery, settings):
    settings.CELERY_BROKER_URL = 'redis://unavailable'
    with patch('patient_portal.tasks.deliver_webhook.apply_async') as enqueue:
        enqueue_delivery(delivery.pk)
    assert enqueue.call_args.kwargs['retry'] is False
    assert enqueue.call_args.kwargs['ignore_result'] is True


def test_webhooks_registered_only_in_current_api_and_schema(setup):
    from django.urls import resolve
    from drf_spectacular.generators import SchemaGenerator
    from patient_portal.api import v1_urls, urls
    from patient_portal.api.webhook_views import InboundWebhookView
    assert resolve('/api/v1/webhooks/inbound/').func.view_class is InboundWebhookView
    assert all('webhook' not in prefix for prefix, _, _ in urls.router.registry)
    assert not any(getattr(pattern, 'name', None) == 'webhook-inbound' for pattern in urls.urlpatterns)
    assert 'Deprecation' not in inbound()
    schema = SchemaGenerator(patterns=v1_urls.urlpatterns).get_schema(public=True)
    assert '/webhooks/inbound/' in schema['paths']
    assert '/webhooks/subscriptions/' in schema['paths']


def test_prune_keeps_active_recent_and_unprocessed_rows(delivery, setup):
    from django.core.management import call_command
    from io import StringIO
    old = timezone.now() - timedelta(days=40)
    retained = []
    terminal = []
    for status in ['pending', 'retry', 'sending', 'delivered', 'dead_letter', 'cancelled']:
        row = WebhookDelivery.objects.create(subscription=setup[4], payload={}, status=status)
        WebhookDelivery.objects.filter(pk=row.pk).update(created_at=old, next_attempt_at=old)
        (retained if status in ['pending', 'retry', 'sending'] else terminal).append(row.pk)
    assert inbound().status_code == 202
    event = InboundWebhookEvent.objects.get()
    InboundWebhookEvent.objects.filter(pk=event.pk).update(received_at=old, processed_at=old)
    call_command('prune_webhooks', dry_run=True, stdout=StringIO())
    assert WebhookDelivery.objects.filter(pk__in=terminal).count() == 3
    call_command('prune_webhooks', batch_size=1, stdout=StringIO())
    assert WebhookDelivery.objects.filter(pk__in=retained).count() == 3
    assert WebhookDelivery.objects.filter(pk=delivery.pk).exists()
    assert not WebhookDelivery.objects.filter(pk__in=terminal).exists()
    assert not InboundWebhookEvent.objects.exists()
    # An old captured signature remains invalid after deduplication storage is pruned.
    assert inbound(HTTP_X_HEALTHKEY_TIMESTAMP=str(int(old.timestamp()))).status_code == 401


@pytest.mark.parametrize('fail', [False, True])
def test_fhir_outbox_shares_patient_transaction(setup, django_capture_on_commit_callbacks, fail):
    from io import BytesIO
    from patient_portal.tests import _make_vocab_fixtures, _make_fhir_bundle
    from patient_portal.webhooks import publish_patient_bulk_change
    _make_vocab_fixtures()
    client = APIClient()
    client.force_authenticate(setup[3])
    upload = BytesIO(json.dumps(_make_fhir_bundle()).encode())
    upload.name = 'bundle.json'
    seen = []

    def publish(*args, **kwargs):
        from django.db import connection
        # TestCase's outer transaction plus the upload's patient savepoint.
        assert connection.savepoint_ids
        publish_patient_bulk_change(*args, **kwargs)
        seen.append(True)
        if fail:
            raise RuntimeError('force failure after outbox creation')

    # Refresh is a final required step after the bulk events have been persisted.
    with patch('patient_portal.webhooks.enqueue_delivery') as enqueue:
        with django_capture_on_commit_callbacks(execute=True):
            if fail:
                with patch('patient_portal.api.views.refresh_patient_record', side_effect=RuntimeError('rollback')):
                    response = client.post('/api/v1/patient-records/upload_fhir/', {'file': upload}, format='multipart')
            else:
                with patch('patient_portal.webhooks.publish_patient_bulk_change', side_effect=publish):
                    response = client.post('/api/v1/patient-records/upload_fhir/', {'file': upload}, format='multipart')
            enqueue.assert_not_called()
        assert response.status_code == 200, response.data
        if fail:
            assert response.data['errors']
            assert not WebhookDelivery.objects.exists()
            assert not Person.objects.filter(given_name='Jane', family_name='Smith').exists()
            enqueue.assert_not_called()
        else:
            assert not response.data['errors'], response.data
            assert seen
            assert WebhookDelivery.objects.exists()
            assert enqueue.called


# --- review findings on #1318 ---------------------------------------------

def test_subscription_mutations_require_csrf(setup):
    """A subscription names where this org's patient events are sent, so a page
    an admin merely visits must not be able to create or delete one. The project
    default session backend is CSRF-exempt, so the viewset has to opt back in."""
    org, other, person, user, subscription = setup
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        created = client.post('/api/v1/webhooks/subscriptions/', {
            'organization': org.pk, 'url': 'https://attacker.example/collect',
            'event_types': ['patient.changed'],
        }, format='json')
    assert created.status_code == 403, created.data
    assert not WebhookSubscription.objects.filter(url='https://attacker.example/collect').exists()
    assert client.delete(f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 403
    # The row surviving proves nothing now that removal is a mark rather than a
    # delete; the mark being absent is what says the DELETE was refused.
    subscription.refresh_from_db()
    assert subscription.deleted_at is None and subscription.active
    # Reading is still fine without a token.
    assert client.get('/api/v1/webhooks/subscriptions/').status_code == 200


def test_disclosed_secret_is_not_cacheable(setup):
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)
    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        response = client.post('/api/v1/webhooks/subscriptions/', {
            'organization': org.pk, 'url': subscription.url, 'event_types': ['lab.updated'],
        }, format='json')
    assert response.status_code == 201
    assert response['Cache-Control'] == 'no-store'
    assert response['Pragma'] == 'no-cache'


def test_unverified_inbound_traffic_is_metered(setup, settings):
    """The source quota keys on a verified source, so it cannot bound traffic
    that never verifies. Without an ingress bucket, bad signatures are free."""
    from django.core.cache import cache

    settings.WEBHOOK_INGRESS_RATE = '5/minute'
    cache.clear()
    try:
        seen = [inbound(signature='wrong').status_code for _ in range(8)]
        assert 429 in seen, seen
        assert seen.index(429) >= 5, seen
        # A caller cannot mint a fresh bucket by prepending to a header it
        # controls: the key counts back from the end of the chain.
        spoofed = inbound(signature='wrong',
                          HTTP_X_FORWARDED_FOR='9.9.9.9, 127.0.0.1').status_code
        assert spoofed == 429, spoofed
    finally:
        cache.clear()


def test_a_verified_source_meets_its_own_quota_first(setup, settings):
    """The ingress bucket sits above the per-source rate on purpose: metering
    unverified traffic must not cap a legitimate sender below its quota."""
    from django.core.cache import cache

    settings.WEBHOOK_INGRESS_RATE = '1200/minute'
    settings.WEBHOOK_INBOUND_RATE = '600/minute'
    cache.clear()
    try:
        codes = set()
        for i in range(70):
            codes.add(inbound(payload={
                'id': f'quota-{i}', 'type': 'lab.updated', 'data': {'person_id': 420001},
            }).status_code)
        assert codes == {202}, codes
    finally:
        cache.clear()


def test_csrf_protected_endpoint_still_serves_its_real_callers(setup):
    """The CSRF fix must deny the cross-site POST without denying the two ways
    this endpoint is legitimately called."""
    org, other, person, user, subscription = setup

    # 1. A session caller that does send the token — same pattern as
    #    test_session_admin_upload_with_csrf_succeeds.
    from django.middleware.csrf import _get_new_csrf_string

    session = APIClient(enforce_csrf_checks=True)
    session.force_login(user)
    csrf = _get_new_csrf_string()
    session.cookies['csrftoken'] = csrf
    session.credentials(HTTP_X_CSRFTOKEN=csrf)
    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        created = session.post(
            '/api/v1/webhooks/subscriptions/',
            {'organization': org.pk, 'url': 'https://legit.example/events',
             'event_types': ['lab.updated']},
            format='json',
        )
    assert created.status_code == 201, created.data

    # 2. A header-authenticated caller, which carries no cookie and so is not
    #    subject to CSRF at all.
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    bearer = APIClient(enforce_csrf_checks=True)
    bearer.force_authenticate(user, token='service-token')
    assert bearer.get('/api/v1/webhooks/subscriptions/').status_code == 200


def test_oversized_body_is_refused_before_it_is_read(setup):
    """Refused on the declared length, so a 2.5MB body is never buffered.

    Asserting the status alone is not enough — a size check placed after
    `request.body` also answers 413 — so this makes reading the body an error
    and shows the request is refused without it.
    """
    big = b'{"id": "e", "type": "lab.updated", "data": {"person_id": 420001}, "pad": "' + b'x' * 70000 + b'"}'
    boom = PropertyMock(side_effect=AssertionError('the body was buffered'))
    with patch('django.http.HttpRequest.body', new_callable=lambda: property(boom)):
        assert inbound(payload=big, signature='sha256=' + '0' * 64).status_code == 413
    # And a correctly signed oversized body is refused too.
    assert inbound(payload=big).status_code == 413


def test_outbound_signature_binds_a_timestamp(setup):
    """Inbound rejects a body-only signature as replayable; outbound owes
    subscribers the same construction."""
    response = Mock(status=200)
    with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', ('8.8.8.8', 443))]):
        with patch('patient_portal.webhooks.urllib3.HTTPSConnectionPool') as pool:
            pool.return_value.urlopen.return_value = response
            send_webhook('https://subscriber.example/events', {'id': 'x'}, 'shhh', 'delivery-1')
    headers = pool.return_value.urlopen.call_args.kwargs['headers']
    body = pool.return_value.urlopen.call_args.kwargs['body']
    ts = headers['X-HealthKey-Timestamp']
    assert ts.isdigit() and abs(time.time() - int(ts)) < 60
    assert headers['X-HealthKey-Signature'] == compute_hmac_signature(body, 'shhh', ts)
    # The body-only signature must no longer verify: that is the replayable one.
    assert headers['X-HealthKey-Signature'] != compute_hmac_signature(body, 'shhh')


def test_unknown_source_still_runs_the_comparison(setup):
    """An unknown source id must not answer measurably sooner than a known one
    with a bad signature."""
    with patch('patient_portal.api.webhook_views.hmac.compare_digest',
               wraps=hmac.compare_digest) as compare:
        assert inbound(source='no-such-source', signature='sha256=' + '0' * 64).status_code == 401
    assert compare.called, 'the signature comparison was short-circuited away'


def test_embedded_beat_is_off_unless_asked_for(tmp_path):
    """Beat is a singleton; a default of on breaks the moment a worker service
    gets a second replica."""
    import os
    import subprocess

    root = Path(__file__).resolve().parent.parent
    celery = tmp_path / 'celery'
    celery.write_text('#!/bin/bash\nprintf "%s" "$*"\n')
    celery.chmod(0o755)
    env = {'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'CELERY_BROKER_URL': 'redis://example.invalid',
           'DATABASE_URL': 'postgresql://example.invalid', 'SECRET_KEY': 'test-only'}
    result = subprocess.run(['bash', str(root / 'start-worker.sh')],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--beat' not in result.stdout


@pytest.mark.parametrize('raw', ['[]', '"x"', '{oops', '{"s": {}}', '{"s": {"secret": "k"}}'])
def test_inbound_sources_config_is_rejected_at_startup(raw):
    """A valid non-object passes json.loads and then raises AttributeError on
    every inbound request instead of failing at boot."""
    from django.core.exceptions import ImproperlyConfigured

    from promop.settings import _webhook_inbound_sources

    with pytest.raises(ImproperlyConfigured):
        _webhook_inbound_sources(raw)


def test_prune_walks_forward_instead_of_rescanning(setup):
    """Each batch starts after the last pk rather than re-running LIMIT from the
    beginning of the same unindexed scan."""
    from io import StringIO

    from django.core.management import call_command

    org, other, person, user, subscription = setup
    stale = timezone.now() - timedelta(days=90)
    for _ in range(5):
        delivery = WebhookDelivery.objects.create(
            subscription=subscription, payload={'id': 'x'}, status='delivered')
        WebhookDelivery.objects.filter(pk=delivery.pk).update(
            created_at=stale, next_attempt_at=stale, delivered_at=stale)
    out = StringIO()
    with CaptureQueriesContext(connection) as queries:
        call_command('prune_webhooks', batch_size=2, stdout=out)
    assert 'pruned 5' in out.getvalue()
    assert not WebhookDelivery.objects.exists()
    # The batch SELECTs must carry an advancing lower bound. Without it every
    # batch re-runs LIMIT from the start of the same scan, which is what makes
    # the old version quadratic — and deleting the rows looks identical.
    selects = [q['sql'] for q in queries.captured_queries
               if q['sql'].lstrip().upper().startswith('SELECT')
               and 'webhookdelivery' in q['sql'].lower()]
    bounded = [sql for sql in selects if '"id" > ' in sql]
    assert len(selects) >= 3, selects
    assert len(bounded) >= 2, selects


def test_subscription_url_is_validated_by_the_model_not_only_the_api(setup):
    """A shell or a data migration must not be able to write an SSRF target."""
    from django.core.exceptions import ValidationError as DjangoValidationError

    org = setup[0]
    for url in ('http://10.0.0.1/hook', 'https://192.168.0.5/hook',
                'https://user:pw@example.org/hook', 'https://example.org:8443/hook'):
        subscription = WebhookSubscription(organization=org, url=url, event_types=['lab.updated'])
        with pytest.raises(DjangoValidationError) as error:
            subscription.full_clean()
        assert 'url' in error.value.message_dict, url
    WebhookSubscription(
        organization=org, url='https://subscriber.example/events',
        event_types=['lab.updated'],
    ).full_clean()


def test_a_relayed_event_carries_the_source_it_came_from(setup):
    """Each hop mints a fresh id, so the dedup cannot see a cycle; the marker
    lets the partner that fed us recognise its own event coming back."""
    response = inbound()
    assert response.status_code == 202
    delivery = WebhookDelivery.objects.filter(
        payload__type='lab.updated', payload__origin__isnull=False).first()
    assert delivery is not None
    assert delivery.payload['origin'] == {'source': 'lab-system', 'event_id': 'event-1'}


def test_an_event_we_originate_carries_no_origin_marker(setup):
    org, other, person, user, subscription = setup
    publish_event(org.pk, 'patient.changed', {'person_id': person.person_id})
    delivery = WebhookDelivery.objects.filter(payload__type='patient.changed').first()
    assert delivery is not None
    assert 'origin' not in delivery.payload


def test_retention_is_scheduled_where_the_deployment_runs_beat(settings):
    """The docs ask for a daily prune; beat is the process that has to run it."""
    from celery.schedules import crontab

    entry = settings.CELERY_BEAT_SCHEDULE['prune-webhook-history']
    assert entry['task'] == 'patient_portal.tasks.prune_webhook_history'
    assert isinstance(entry['schedule'], crontab)


def test_the_retention_task_runs_the_command(setup):
    from patient_portal.tasks import prune_webhook_history

    org, other, person, user, subscription = setup
    stale = timezone.now() - timedelta(days=90)
    delivery = WebhookDelivery.objects.create(
        subscription=subscription, payload={'id': 'x'}, status='delivered')
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        created_at=stale, next_attempt_at=stale, delivered_at=stale)
    prune_webhook_history()
    assert not WebhookDelivery.objects.filter(pk=delivery.pk).exists()


def test_the_batch_sentinel_is_typed_to_each_models_key(setup):
    """WebhookDelivery is keyed by UUID and InboundWebhookEvent by an integer."""
    import uuid as uuid_module

    from django.db import models as django_models

    from patient_portal.models import InboundWebhookEvent as Inbound

    assert isinstance(WebhookDelivery._meta.pk, django_models.UUIDField)
    assert not isinstance(Inbound._meta.pk, django_models.UUIDField)
    # The nil UUID sorts below every generated one, so the first batch sees the
    # whole table rather than skipping rows.
    assert uuid_module.UUID(int=0) < uuid_module.uuid4()


@pytest.fixture
def trusted_professional(setup):
    """A doctor at org A holding an organization trust into org B.

    This is the shape that made a trust an egress authority: the trust is
    granted so the professional can work with B's patients, and it also reached
    subscription creation, which names where B's patient events are sent.
    """
    from omop_core.models import OrgTrust

    org, other, person, user, subscription = setup
    professional = Identity.objects.create_user(email='doctor@trusted.example')
    GroupAccess.objects.create(identity=professional, org=other, role='doctor')
    OrgTrust.objects.create(granting_org=org, trusted_org=other)
    client = APIClient()
    # A session, so what this fixture measures is the trust rule and not
    # the interactive-session rule that writes also have to clear.
    client.force_login(professional)
    return client, org, other, professional


def test_a_trust_no_longer_reaches_subscription_creation(trusted_professional):
    client, org, other, professional = trusted_professional
    from omop_core.services.access import get_admin_orgs, get_direct_admin_orgs

    # The trust still grants data access; it no longer grants egress config.
    assert org in get_admin_orgs(professional)
    assert org not in get_direct_admin_orgs(professional)
    response = client.post('/api/v1/webhooks/subscriptions/', {
        'organization': org.pk, 'url': 'https://attacker.example/collect',
        'event_types': ['patient.changed'],
    }, format='json')
    # 403 exactly, so this still fails when the permission class stops checking:
    # the serializer and the narrowed queryset refuse a trust too, with 400 and
    # 404, and a range that accepts those cannot tell the gate from its backstops.
    assert response.status_code == 403, response.data
    assert not WebhookSubscription.objects.filter(url='https://attacker.example/collect').exists()


def test_a_deactivated_organization_has_no_direct_admin(setup):
    """One rule, asked two ways, must answer the same.

    `get_direct_admin_orgs` and `has_explicit_org_admin_access` are the same
    policy — a direct org_admin grant, trusts excluded — written independently
    on two branches. They had drifted: only the second excluded a deactivated
    organization, so switching an org off left its admin able to redirect its
    events.
    """
    from omop_core.services.access import get_direct_admin_orgs, has_explicit_org_admin_access

    org, _, _, user, subscription = setup
    assert org in get_direct_admin_orgs(user)
    assert has_explicit_org_admin_access(user, org.slug)

    Organization.objects.filter(pk=org.pk).update(is_active=False)
    org.refresh_from_db()
    assert org not in get_direct_admin_orgs(user)
    assert not has_explicit_org_admin_access(user, org.slug)

    client = APIClient()
    client.force_login(user)
    response = client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                            {'url': 'https://elsewhere.example/collect'}, format='json')
    assert response.status_code in (403, 404), response.data
    subscription.refresh_from_db()
    assert subscription.url == 'https://subscriber.example/events'


def test_a_trust_cannot_redirect_or_delete_an_existing_subscription(trusted_professional):
    client, org, other, professional = trusted_professional
    subscription = WebhookSubscription.objects.get(organization=org)

    # 403 when the caller administers nothing directly, 404 once the narrowed
    # queryset is the only thing hiding the row — both are refusals, and which
    # one fires is not a property worth pinning.
    patched = client.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                           {'url': 'https://attacker.example/collect'}, format='json')
    assert patched.status_code in (403, 404), patched.data
    assert client.delete(
        f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code in (403, 404)
    subscription.refresh_from_db()
    assert subscription.url == 'https://subscriber.example/events'
    # Same reason as above: removal is a mark, so the absence of the mark is
    # what shows the refusal took effect.
    assert subscription.deleted_at is None and subscription.active


def test_a_trust_still_reads_the_subscriptions_it_could_always_see(trusted_professional):
    client, org, other, professional = trusted_professional
    listing = client.get('/api/v1/webhooks/subscriptions/')
    assert listing.status_code == 200
    rows = listing.data['results'] if isinstance(listing.data, dict) else listing.data
    urls = [row['url'] for row in rows]
    # Narrowing reads would hide an organization's egress configuration from
    # someone it has already trusted with its patients — the wrong direction
    # for review. What they do not get is the part a receiver may be treating
    # as a credential; see
    # test_a_reader_who_cannot_change_the_destination_does_not_see_it.
    assert 'https://subscriber.example/***' in urls


def test_a_patient_copy_announces_one_event_per_table_not_one_per_row(setup):
    """copy_patient writes a whole patient at once, and signals cannot see it.

    Left to them, an import fires one event per copied row, and `--replace`
    first fires one `deleted` per row of the data being replaced — an operator
    re-syncing a cohort would queue a delivery per clinical row, each
    announcing a removal that did not happen. The copy announces its own work
    instead, the same way the bulk API and FHIR-sync writers do.
    """
    from omop_core.models import Measurement
    from omop_core.services.patient_transfer import apply_patient, read_patient
    from omop_core.signals import suppress_patient_record_refresh
    from tests.factories import ConceptFactory, MeasurementFactory

    org, other, person, user, subscription = setup
    concept = ConceptFactory(concept_name='Haemoglobin', concept_code='718-7')
    with suppress_patient_record_refresh():
        for _ in range(5):
            MeasurementFactory(person=person, measurement_concept=concept)
        PatientDocument.objects.create(person=person, doc_type='OTHER', title='Referral')
    payload = read_patient('default', person.pk)

    WebhookDelivery.objects.all().delete()
    stats = apply_patient(payload, org, target_person_id=420002)
    assert stats.created['Measurement'] == 5

    events = [d.payload for d in WebhookDelivery.objects.all()]
    assert all(e['data']['person_id'] == 420002 for e in events), events
    # One per table that received rows, and nothing per row. Each carries the
    # event type that table's writes always carry — a subscriber listening only
    # for document.received still hears about a document that arrived by copy.
    assert sorted((e['type'], e['data']['resource_type'], e['data']['operation'],
                   e['data']['count']) for e in events) == [
        ('document.received', 'omop_core.patientdocument', 'bulk_saved', 1),
        ('lab.updated', 'omop_core.measurement', 'bulk_saved', 5),
        ('patient.changed', 'omop_core.patientrecord', 'bulk_saved', 1),
        ('patient.changed', 'omop_core.person', 'bulk_saved', 1),
    ]

    # --replace deletes the patient's rows before writing them again under new
    # ids, so a subscriber mirroring by id is told both halves — but once per
    # table, not once per row. That bound is the whole point: the same patient
    # with twenty thousand measurements still costs one pair of events.
    WebhookDelivery.objects.all().delete()
    apply_patient(payload, org, target_person_id=420002, replace=True)
    replaced = [d.payload for d in WebhookDelivery.objects.all()]
    assert sorted((e['data']['resource_type'], e['data']['operation'], e['data']['count'])
                  for e in replaced) == [
        ('omop_core.measurement', 'bulk_deleted', 5),
        ('omop_core.measurement', 'bulk_saved', 5),
        ('omop_core.patientdocument', 'bulk_deleted', 1),
        ('omop_core.patientdocument', 'bulk_saved', 1),
        ('omop_core.patientrecord', 'bulk_deleted', 1),
        ('omop_core.patientrecord', 'bulk_saved', 1),
        ('omop_core.person', 'bulk_deleted', 1),
        ('omop_core.person', 'bulk_saved', 1),
    ]
    assert Measurement.objects.filter(person_id=420002).count() == 5

    # And a table that shrinks rather than emptying says how many went.
    payload['rows']['omop_core.Measurement'] = payload['rows']['omop_core.Measurement'][:2]
    WebhookDelivery.objects.all().delete()
    apply_patient(payload, org, target_person_id=420002, replace=True)
    shrunk = [(e['data']['operation'], e['data']['count'])
              for e in (d.payload for d in WebhookDelivery.objects.all())
              if e['data']['resource_type'] == 'omop_core.measurement']
    assert sorted(shrunk) == [('bulk_deleted', 5), ('bulk_saved', 2)]
    assert Measurement.objects.filter(person_id=420002).count() == 2


def test_a_replace_that_moves_a_patient_tells_the_organization_that_lost_them(setup):
    """--replace may name a different organization, which moves the patient.

    The aggregates go to the organization that now holds the data. Without a
    word to the one it left, that tenant's subscriber keeps a patient it no
    longer has — and this is the one case where rows really were deleted from
    an organization rather than rewritten under it.
    """
    from omop_core.services.patient_transfer import apply_patient, read_patient
    from omop_core.signals import suppress_patient_record_refresh
    from tests.factories import ConceptFactory, MeasurementFactory

    org, other, person, user, subscription = setup  # `subscription` belongs to org
    gaining = WebhookSubscription.objects.create(
        organization=other, url='https://gaining.example/events',
        event_types=['patient.changed', 'lab.updated'],
    )
    with suppress_patient_record_refresh():
        MeasurementFactory(person=person, measurement_concept=ConceptFactory())
    payload = read_patient('default', person.pk)

    WebhookDelivery.objects.all().delete()
    apply_patient(payload, other, target_person_id=person.pk, replace=True)

    # Addressed to the organization that actually held the rows, under the
    # person_id it knew them by — and nothing about the arrival, which is not
    # its patient any more.
    departure = [d.payload for d in WebhookDelivery.objects.filter(subscription=subscription)]
    assert sorted((e['data']['resource_type'], e['data']['operation'], e['data']['count'])
                  for e in departure) == [
        ('omop_core.measurement', 'bulk_deleted', 1),
        ('omop_core.patientrecord', 'bulk_deleted', 1),
        ('omop_core.person', 'bulk_deleted', 1),
    ], departure
    assert all(e['data']['person_id'] == person.pk for e in departure)
    # The organization that now holds the patient hears the arrival instead,
    # and is told nothing about rows it never had.
    arrival = [d.payload for d in WebhookDelivery.objects.filter(subscription=gaining)]
    assert sorted((e['data']['resource_type'], e['data']['operation'])
                  for e in arrival) == [
        ('omop_core.measurement', 'bulk_saved'),
        ('omop_core.patientrecord', 'bulk_saved'),
        ('omop_core.person', 'bulk_saved'),
    ], arrival


def test_a_replace_that_empties_a_table_says_so(setup):
    """The other direction: the source dropped this patient's labs.

    The copy correctly deletes the five rows here and brings none. Told only
    what arrived, a subscriber would mirror five measurements that no longer
    exist — worse than the per-row storm this replaced, which at least said
    something.
    """
    from omop_core.models import Measurement
    from omop_core.services.patient_transfer import apply_patient, read_patient
    from omop_core.signals import suppress_patient_record_refresh
    from tests.factories import ConceptFactory, MeasurementFactory

    org, other, person, user, subscription = setup
    with suppress_patient_record_refresh():
        for _ in range(5):
            MeasurementFactory(person=person, measurement_concept=ConceptFactory())
        emptied = Person.objects.create(person_id=420004)
        PatientRecord.objects.create(person=emptied, organization=org)
    payload = read_patient('default', emptied.pk)  # the same patient, without labs

    WebhookDelivery.objects.all().delete()
    apply_patient(payload, org, target_person_id=person.pk, replace=True)
    assert not Measurement.objects.filter(person_id=person.pk).exists()

    events = [d.payload for d in WebhookDelivery.objects.all()]
    assert ('patient.changed', 'omop_core.measurement', 'bulk_deleted', 5) in [
        (e['type'], e['data']['resource_type'], e['data']['operation'], e['data']['count'])
        for e in events
    ], events


def test_a_copy_from_a_source_without_a_patient_record_still_announces_one(setup):
    """refresh_patient_record derives the record here whether or not the source
    had one to copy, so the write happened and the read model changed."""
    from omop_core.services.patient_transfer import apply_patient, read_patient
    from omop_core.signals import suppress_patient_record_refresh

    org, other, person, user, subscription = setup
    with suppress_patient_record_refresh():
        recordless = Person.objects.create(person_id=420005)
    payload = read_patient('default', recordless.pk)
    assert not payload['rows']['omop_core.PatientRecord']

    WebhookDelivery.objects.all().delete()
    stats = apply_patient(payload, org, target_person_id=420006)
    assert stats.created['PatientRecord'] == 0
    assert 'omop_core.patientrecord' in [
        d.payload['data']['resource_type'] for d in WebhookDelivery.objects.all()
    ]


def test_replacing_an_unassigned_patient_tells_no_tenant(setup):
    """No organization held those rows, so none is told they went.

    Addressing the deletions to the organization receiving the copy would tell
    a tenant that data it never had was removed — and how much of it there was.
    """
    from omop_core.services.patient_transfer import apply_patient, read_patient
    from omop_core.signals import suppress_patient_record_refresh
    from tests.factories import ConceptFactory, MeasurementFactory

    org, other, person, user, subscription = setup
    with suppress_patient_record_refresh():
        unassigned = Person.objects.create(person_id=420007)
        PatientRecord.objects.create(person=unassigned, organization=None)
        for _ in range(3):
            MeasurementFactory(person=unassigned, measurement_concept=ConceptFactory())
        donor = Person.objects.create(person_id=420008)
        PatientRecord.objects.create(person=donor, organization=org)
    payload = read_patient('default', donor.pk)

    WebhookDelivery.objects.all().delete()
    apply_patient(payload, org, target_person_id=unassigned.pk, replace=True)

    events = [d.payload for d in WebhookDelivery.objects.all()]
    assert not [e for e in events if e['data']['operation'] == 'bulk_deleted'], events
    # The arrival is announced, because the copy does assign an organization.
    assert {e['data']['resource_type'] for e in events} == {
        'omop_core.person', 'omop_core.patientrecord',
    }


def test_a_dry_run_copy_announces_nothing(setup):
    """The outbox row is written inside the copy's transaction, so a rollback
    takes it back — without that, a dry run would tell subscribers about data
    this instance does not have."""
    from omop_core.services.patient_transfer import apply_patient, read_patient
    from omop_core.signals import suppress_patient_record_refresh
    from tests.factories import ConceptFactory, MeasurementFactory

    org, other, person, user, subscription = setup
    with suppress_patient_record_refresh():
        MeasurementFactory(person=person, measurement_concept=ConceptFactory())
    payload = read_patient('default', person.pk)

    WebhookDelivery.objects.all().delete()
    apply_patient(payload, org, target_person_id=420003, dry_run=True)
    assert not Person.objects.filter(person_id=420003).exists()
    assert not WebhookDelivery.objects.exists()


def test_a_delegated_machine_credential_cannot_configure_egress(setup):
    """The org admin's own authority, presented without the org admin.

    A subscription is a long-lived egress channel and create() discloses its
    signing secret, so administering one is credential administration — the act
    ServiceApplicationViewSet already requires an interactive session for. An
    OAuth2 access token this admin delegated to a third-party application
    carries `patient/*.write`, and without this rule the application could turn
    that scoped, revocable, expiring grant into a permanent feed of the
    organization's patient events pointed at a URL of its own choosing.
    """
    from oauth2_provider.models import AccessToken, Application

    org, other, person, user, subscription = setup
    # The real credential shape, not a bare string: an authorization-code grant
    # this admin approved for a third-party application, carrying the write
    # scope and their own identity.
    app = Application.objects.create(
        name='Third-party integration', client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE, user=user,
    )
    token = AccessToken.objects.create(
        application=app, user=user, token='delegated-egress-token',
        scope='patient/*.write', expires=timezone.now() + timedelta(hours=1),
    )
    machine = APIClient()
    machine.force_authenticate(user, token=token)
    with patch('patient_portal.api.webhook_views.validate_webhook_url'):
        created = machine.post('/api/v1/webhooks/subscriptions/', {
            'organization': org.pk, 'url': 'https://third-party.example/events',
            'event_types': ['patient.changed'],
        }, format='json')
    assert created.status_code == 403, created.data
    assert not WebhookSubscription.objects.filter(
        url='https://third-party.example/events').exists()
    assert machine.patch(f'/api/v1/webhooks/subscriptions/{subscription.pk}/',
                         {'url': 'https://third-party.example/events'},
                         format='json').status_code == 403
    assert machine.delete(
        f'/api/v1/webhooks/subscriptions/{subscription.pk}/').status_code == 403
    subscription.refresh_from_db()
    assert subscription.url == 'https://subscriber.example/events'
    # Only writes moved. A header-authenticated caller still reads the listing
    # on exactly the terms it always did — scope-checked, no secret disclosed —
    # which test_csrf_protected_endpoint_still_serves_its_real_callers pins.


def test_a_direct_org_admin_still_administers_its_own_subscriptions(setup):
    org, other, person, user, subscription = setup
    client = APIClient()
    client.force_login(user)  # live org_admin grant on org
    response = client.post('/api/v1/webhooks/subscriptions/', {
        'organization': org.pk, 'url': 'https://partner.example/events',
        'event_types': ['patient.changed'],
    }, format='json')
    assert response.status_code == 201, response.data
    assert response.data['secret']
    created = WebhookSubscription.objects.get(url='https://partner.example/events')
    assert client.patch(f'/api/v1/webhooks/subscriptions/{created.pk}/',
                        {'active': False}, format='json').status_code == 200
