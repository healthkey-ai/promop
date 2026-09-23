"""Signed notifications and a transactional webhook outbox."""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import urllib3
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

from omop_core.models import PatientRecord
from patient_portal.models import WebhookDelivery, WebhookSubscription

logger = logging.getLogger(__name__)
EVENT_TYPES = ('patient.changed', 'lab.updated', 'document.received', 'foundation.synced')
# Which event a save carries, by table. Shared by the signal and the bulk
# publisher: a subscriber that asked only for document.received hears about a
# document however it was written, and not only when it arrived one at a time.
_SAVE_EVENT_TYPES = {'measurement': 'lab.updated', 'patientdocument': 'document.received'}
# The patient tables a subscriber hears about. One list, so a bulk writer that
# announces its own work cannot cover a different set than the signals do.
PATIENT_EVENT_MODELS = (
    'omop_core.Person', 'omop_core.PatientRecord', 'omop_core.Measurement',
    'omop_core.PatientDocument', 'omop_core.ConditionOccurrence',
    'omop_core.DrugExposure', 'omop_core.Observation', 'omop_core.ProcedureOccurrence',
    'omop_core.PatientTrialEnrollment', 'omop_oncology.Episode',
)
_suppress_events = ContextVar('suppress_webhook_events', default=False)


@contextmanager
def suppress_webhook_events():
    token = _suppress_events.set(True)
    try:
        yield
    finally:
        _suppress_events.reset(token)


def compute_hmac_signature(payload, secret, timestamp=None):
    """Sign the exact wire bytes; callers must not reserialize before verifying."""
    if timestamp is not None:
        payload = str(timestamp).encode('ascii') + b'.' + payload
    return 'sha256=' + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def encode_payload(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()


def validate_webhook_url(url):
    """Validate syntax and literal IPs without performing DNS in HTTP workers."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError
        hostname = parsed.hostname.encode('idna').decode('ascii')
        parsed = parsed._replace(netloc=f'[{hostname}]' if ':' in hostname else hostname)
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass  # Hostname resolution and validation happen in the delivery task.
        else:
            if not _public_address(address):
                raise ValueError
    except (ValueError, OSError, UnicodeError):
        raise ValueError('Webhook URL must resolve only to public HTTPS addresses on port 443.') from None
    return parsed


def _embedded_ipv4(address):
    """The IPv4 addresses an IPv6 address carries inside it, if any."""
    if address.version != 6:
        return
    for wrapper in (address.ipv4_mapped, address.sixtofour):
        if wrapper is not None:
            yield wrapper
    if address.teredo:
        yield from address.teredo


def _public_address(address):
    """Judge the address a packet arrives at, not the notation it was written in.

    ``is_global`` judges the wrapper, and on the interpreter this deploys on it
    judges it wrongly: CPython below 3.12.4 (CVE-2024-4032) reports
    ``2002:7f00:1::`` — the 6to4 form of 127.0.0.1 — as globally reachable, and
    every Render service pinned 3.12.0. The pin moves in this change, but a
    validator whose correctness rests on a patch-level pin in another file is
    one that regresses silently on a rollback, so unwrap the wrappers here too.

    ``ipv4_mapped`` and ``teredo`` are covered by the special-address table on
    the interpreters checked, before the fix and after it. They are unwrapped
    anyway: the rule then holds because of what is written here rather than
    because of a table that has already been wrong once.
    """
    if not (address.is_global and not address.is_multicast and not address.is_reserved):
        return False
    return all(_public_address(embedded) for embedded in _embedded_ipv4(address))


def resolve_webhook_url(url):
    parsed = validate_webhook_url(url)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        # Order by address family, then by address. Sorting the strings put
        # '2606:4700::1111' before '8.8.8.8', so a dual-stack subscriber behind
        # a CDN was reached over IPv6 — from a runtime without IPv6 egress that
        # is a connection error on every attempt, and the stored error is a
        # fixed token, so the cause never reaches the operator. Every address is
        # validated; the order only decides which is tried first.
        ips = sorted({address[4][0] for address in addresses},
                     key=lambda ip: (ipaddress.ip_address(ip).version, ip))
        if not ips or any(not _public_address(ipaddress.ip_address(ip)) for ip in ips):
            raise ValueError
        # Every address is validated, but only the first of each family is
        # tried. The defect is which family gets chosen, not which record
        # within one — choosing between several A records is the subscriber's
        # load balancer's job, and each extra candidate costs another connect
        # timeout inside the task's soft limit.
        first_of_family = {}
        for ip in ips:
            first_of_family.setdefault(ipaddress.ip_address(ip).version, ip)
        ips = list(first_of_family.values())
    except (ValueError, OSError):
        raise ValueError('Webhook URL must resolve only to public HTTPS addresses on port 443.') from None
    return parsed, ips


def send_webhook(url, payload, secret, delivery_id):
    parsed, addresses = resolve_webhook_url(url)
    body = encode_payload(payload)
    # Bind a timestamp into the signed bytes, exactly as the inbound path does.
    # A body-only signature is replayable forever, and the documented mitigation
    # — dedup on X-HealthKey-Delivery — cannot carry that weight on its own: the
    # header is outside the signature, and a retry legitimately reuses the same
    # id, so a replay is indistinguishable from one. No cross-direction forgery
    # risk from sharing the scheme: an outbound body always starts with '{', so
    # it can never be read as the inbound 'digits.' prefix.
    timestamp = str(int(timezone.now().timestamp()))
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    headers = {
        'Host': parsed.netloc, 'Content-Type': 'application/json',
        'X-HealthKey-Signature': compute_hmac_signature(body, secret, timestamp),
        'X-HealthKey-Timestamp': timestamp,
        'X-HealthKey-Delivery': str(delivery_id),
    }
    # Try each validated address in turn, but only while the failure is a
    # connection failure: once a request is on the wire the peer may have acted
    # on it, so a read timeout or a protocol error must not be retried against
    # another address — that would be a second delivery of the same event.
    last = len(addresses) - 1
    for index, address in enumerate(addresses):
        # Pin the validated address while retaining the original hostname for SNI
        # and certificate checks, so DNS rebinding cannot bypass network validation.
        pool = urllib3.HTTPSConnectionPool(
            address, port=443, server_hostname=parsed.hostname,
            assert_hostname=parsed.hostname, cert_reqs='CERT_REQUIRED',
            timeout=urllib3.Timeout(connect=5, read=10), retries=False,
        )
        response = None
        try:
            response = pool.urlopen(
                'POST', target, body=body, headers=headers,
                redirect=False, retries=False, preload_content=False,
            )
            return response.status
        except urllib3.exceptions.ConnectTimeoutError:
            # The stored error is a fixed token, by design — exception text can
            # carry URL credentials. The family is not sensitive and is the one
            # fact an operator needs to tell "unreachable over IPv6" apart from
            # "the subscriber is down".
            logger.warning('Webhook delivery %s: no connection over IPv%s; %s',
                           delivery_id, ipaddress.ip_address(address).version,
                           'trying the next address' if index < last else 'no addresses left')
            # The parent class, deliberately: NewConnectionError subclasses it,
            # and a blackholed route — the IPv6-without-egress case this exists
            # for — times out rather than being refused. Either way the peer was
            # never reached, so the next address is a first attempt, not a
            # resend. Anything raised after the request is on the wire (read
            # timeout, protocol or TLS error) propagates instead.
            if index == last:
                raise
        finally:
            if response is not None:
                response.close()
            pool.close()


def enqueue_delivery(delivery_id, countdown=0):
    from django.conf import settings
    from patient_portal.tasks import deliver_webhook

    if not settings.WEBHOOKS_ENABLED or not settings.CELERY_BROKER_URL:
        return
    try:
        deliver_webhook.apply_async(args=[str(delivery_id)], countdown=countdown, retry=False, ignore_result=True)
    except Exception:
        # A periodic sweep recovers committed outbox rows after broker outages.
        logger.warning('Webhook queue unavailable; delivery retained in outbox')


def publish_event(organization_id, event_type, data, origin=None):
    """Fan an event out to the organization's subscribers.

    ``origin`` marks an event we are relaying rather than originating. Each hop
    mints a fresh event id, so the ``(source, event_id)`` dedup cannot see a
    cycle: an organization subscribed to the partner that feeds it would send
    the partner its own event back. The marker lets that partner recognise its
    own traffic and stop; it does not stop a partner that ignores it, which is
    why the inbound quota remains the hard bound.
    """
    if not settings.WEBHOOKS_ENABLED:
        return
    if event_type not in EVENT_TYPES:
        raise ValueError('Unsupported webhook event type')
    event = {
        'id': str(uuid.uuid4()), 'type': event_type,
        'occurred_at': timezone.now().isoformat(), 'data': data,
    }
    if origin is not None:
        event['origin'] = origin
    for subscription in WebhookSubscription.objects.filter(
        organization_id=organization_id, organization__is_active=True,
        active=True, event_types__contains=[event_type],
    ):
        delivery = WebhookDelivery.objects.create(subscription=subscription, payload=event)
        transaction.on_commit(lambda pk=delivery.pk: enqueue_delivery(pk))


def _relay(event, event_type, data):
    publish_event(event.organization_id, event_type, data, origin={
        'source': event.source, 'event_id': event.event_id,
    })


def handle_lab_update(event, data):
    _relay(event, 'lab.updated', data)


def handle_document_received(event, data):
    _relay(event, 'document.received', data)


def handle_foundation_sync(event, data):
    _relay(event, 'foundation.synced', data)


INBOUND_HANDLERS = {
    'lab.updated': handle_lab_update,
    'document.received': handle_document_received,
    'foundation.synced': handle_foundation_sync,
}


def publish_patient_bulk_change(person_id, model_name, count, operation='bulk_saved',
                                app_label='omop_core', organization_id=None):
    # Honour the suppressor too: a caller that wraps an ingest in
    # suppress_webhook_events() expecting silence would otherwise still get the
    # aggregate. Every current call site publishes outside the block, so this
    # only closes the trap for the next one.
    if not settings.WEBHOOKS_ENABLED or not count or _suppress_events.get():
        return
    # A caller announcing several tables for one patient already knows the
    # organization; without this each table repeats the same lookup.
    if organization_id is None:
        organization_id = (PatientRecord.objects.filter(person_id=person_id)
                           .values_list('organization_id', flat=True).first())
    if organization_id is not None and count:
        event_type = 'patient.changed' if operation == 'bulk_deleted' else _SAVE_EVENT_TYPES.get(
            model_name, 'patient.changed')
        publish_event(organization_id, event_type, {
            'person_id': person_id, 'resource_type': f'{app_label}.{model_name}',
            'operation': operation, 'count': count,
        })


def patient_data_changed(sender, instance, raw=False, signal=None, **kwargs):
    if not settings.WEBHOOKS_ENABLED or raw or _suppress_events.get():
        return
    person_id = getattr(instance, 'person_id', None)
    if person_id is None:
        return
    organization_id = (instance.organization_id if isinstance(instance, PatientRecord)
                       else PatientRecord.objects.filter(person_id=person_id)
                       .values_list('organization_id', flat=True).first())
    if organization_id is None:
        return
    event_type = 'patient.changed'
    if signal is post_save:
        event_type = _SAVE_EVENT_TYPES.get(sender._meta.model_name, event_type)
    publish_event(organization_id, event_type, {
        'person_id': person_id, 'resource_id': str(instance.pk),
        'resource_type': sender._meta.label_lower,
        'operation': 'deleted' if signal is post_delete else 'saved',
    })


def connect_patient_signals():
    from django.apps import apps

    for label in PATIENT_EVENT_MODELS:
        model = apps.get_model(label)
        for signal in (post_save, post_delete):
            signal.connect(patient_data_changed, sender=model, dispatch_uid=f'webhooks.{label}')
