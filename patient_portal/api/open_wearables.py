"""Routes a phone app built on the Open Wearables SDK talks to (#2810).

There is no Open Wearables server. The SDK is given a host and posts to fixed
paths under ``{host}/api/v1/``; these views answer at those paths, so the
app's host is this deployment. Rules and the protocol:
``omop_core/services/open_wearables.py``.

Two audiences:

* **The patient, signed in to this site** (``connection``, ``invitation_code``):
  makes a short code on the Wearables tab, sees what is connected, disconnects.
* **The phone** (everything else): redeems the code for tokens, then syncs
  with a bearer token. These views read no session and set no cookie; a phone
  is known by its token alone, and a token names one connection, so what a
  phone sends can only reach the record of the patient who made its code.

What a sync stores goes through the wearable writer the file uploads use
(``PatientRecordViewSet._write_wearable_samples``), so the rows, the units and
the upload history are the ``export.zip`` upload's.
"""
import json
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

from omop_core.models import (
    OpenWearablesConnection, OpenWearablesDevice, Person,
)
from omop_core.services import open_wearables
from patient_portal.consent import RECORD_OFF, record_permitted

from .garmin_oauth import _derive, _fold_history
from .mapping_votes import client_address
from .permissions import is_interactive_session

logger = logging.getLogger(__name__)

NOT_FOUND = 'Invitation code not found or expired.'
BAD_TOKEN = 'Invalid or expired token.'
WRONG_USER = 'Token does not match user_id.'
UNKNOWN_PROVIDER = 'Unknown provider. Expected one of: apple, health_connect, samsung.'
#: A small body: a code or a refresh token.
MAX_CREDENTIAL_BYTES = 4096


# ── Limits ──────────────────────────────────────────────────────────────────

class OpenWearablesPatientThrottle(UserRateThrottle):
    """Making codes and disconnecting, per signed-in user. Reading the status
    is what the Wearables tab does every time it opens, so it is not counted."""
    scope = 'open_wearables_patient'
    rate = '30/hour'

    def allow_request(self, request, view):
        return request.method == 'GET' or super().allow_request(request, view)


class _AddressThrottle(SimpleRateThrottle):
    """A public route, per client address as the deployment's proxies report
    it (``mapping_votes.client_address``): a code is eight characters, and
    this is what stands between a stranger and guessing one."""

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': client_address(request)}


class RedeemThrottle(_AddressThrottle):
    scope = 'open_wearables_redeem'
    rate = '30/hour'


class RefreshThrottle(_AddressThrottle):
    scope = 'open_wearables_refresh'
    rate = '600/hour'


class DeviceThrottle(SimpleRateThrottle):
    """One signed-in phone, whatever addresses it syncs from."""
    scope = 'open_wearables_device'
    rate = '1200/hour'

    def __init__(self, device_id):
        self.device_id = device_id
        super().__init__()

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.device_id}


def _json(data, status_code=200):
    response = JsonResponse(data, status=status_code)
    response['Cache-Control'] = 'no-store'
    # The answer is the whole story: Django's request log would otherwise
    # record every refused token as a warning with the path.
    response._has_been_logged = True
    return response


def _limited(throttle, request):
    """The 429 for a caller over ``throttle``'s rate, or None."""
    if throttle.allow_request(request, None):
        return None
    response = _json({'detail': 'Too many requests. Try again later.'}, 429)
    wait = throttle.wait()
    if wait:
        response['Retry-After'] = str(int(wait) + 1)
    return response


def _off():
    return HttpResponse(status=404)


# ── The patient ─────────────────────────────────────────────────────────────

def _own_person(request):
    """The signed-in patient's person, or the Response that refuses."""
    from patient_portal.models import PatientUser

    if not is_interactive_session(request):
        return None, Response({'error': 'Connecting a phone is for a signed-in patient.'},
                              status=status.HTTP_403_FORBIDDEN)
    try:
        return PatientUser.objects.get(identity=request.user).person, None
    except PatientUser.DoesNotExist:
        return None, Response({'error': 'No patient record linked to your account'},
                              status=status.HTTP_403_FORBIDDEN)


def _host():
    return (settings.APP_BASE_URL or '').rstrip('/')


def _describe(connection):
    now = timezone.now()
    devices = connection.devices.filter(expires_at__gt=now).count() if connection else 0
    return {
        'enabled': open_wearables.enabled(),
        'connected': devices > 0,
        'devices': devices,
        'code_pending': bool(connection and connection.code_hash
                             and connection.code_expires_at and connection.code_expires_at > now),
        'last_sync_at': (connection.last_sync_at.isoformat()
                         if connection and connection.last_sync_at else None),
        'providers': sorted((connection.synced_until or {}).keys()) if connection else [],
        # What the patient types into the app as the server address.
        'host': _host(),
    }


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle, OpenWearablesPatientThrottle])
def connection(request):
    """Whether a phone is connected to the caller's own record, or disconnect them all.

    Disconnecting signs every phone out and forgets the running day totals.
    The values already stored stay: they are the patient's data, removed from
    the upload history like any other upload.
    """
    person, refused = _own_person(request)
    if refused is not None:
        return refused
    if request.method == 'GET':
        return Response(_describe(OpenWearablesConnection.objects.filter(person=person).first()))
    OpenWearablesConnection.objects.filter(person=person).delete()
    return Response(_describe(None))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle, OpenWearablesPatientThrottle])
def invitation_code(request):
    """Make the code the patient types into the app. Shown once.

    A new code replaces the one before it. ``time_zone`` is the browser's IANA
    zone: HealthKit rarely says where a record was taken, so it decides which
    day a late-evening record belongs to.
    """
    if not open_wearables.enabled():
        return Response({'error': 'Connecting a phone is not set up on this server.'},
                        status=status.HTTP_404_NOT_FOUND)
    person, refused = _own_person(request)
    if refused is not None:
        return refused
    if not record_permitted(person):
        return Response({'error': RECORD_OFF}, status=status.HTTP_403_FORBIDDEN)
    zone = open_wearables.clean_time_zone(
        request.data.get('time_zone') if isinstance(request.data, dict) else None)
    code, digest = open_wearables.new_code()
    expires_at = timezone.now() + open_wearables.CODE_LIFETIME
    with transaction.atomic():
        Person.objects.select_for_update(no_key=True).filter(pk=person.pk).first()
        row, _created = OpenWearablesConnection.objects.get_or_create(
            person=person, defaults={'user_id': uuid.uuid4(), 'created_by': request.user})
        row.code_hash = digest
        row.code_expires_at = expires_at
        if zone:
            row.time_zone = zone
        row.save(update_fields=['code_hash', 'code_expires_at', 'time_zone'])
    return Response({**_describe(row), 'code': code, 'code_expires_at': expires_at.isoformat()},
                    status=status.HTTP_201_CREATED)


# ── The phone: credentials ──────────────────────────────────────────────────

def _small_body(request):
    """A small JSON object from the stream, or None."""
    raw = request.read(MAX_CREDENTIAL_BYTES + 1)
    if len(raw) > MAX_CREDENTIAL_BYTES:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _tokens(device, refresh_token=None):
    data = {
        'access_token': open_wearables.access_token(device),
        'token_type': 'bearer',
        'expires_in': open_wearables.ACCESS_TOKEN_SECONDS,
    }
    if refresh_token is not None:
        data['refresh_token'] = refresh_token
    return data


@csrf_exempt
@require_POST
def redeem(request):
    """Exchange a connection code for tokens. Public: the code is the proof.

    One answer for every code that does not work (unknown, used, expired,
    malformed, or the record has since been switched off).
    """
    if not open_wearables.enabled():
        return _off()
    refused = _limited(RedeemThrottle(), request)
    if refused is not None:
        return refused
    body = _small_body(request)
    digest = open_wearables.code_digest(body.get('code')) if body else None
    if digest is None:
        return _json({'detail': NOT_FOUND}, 404)
    now = timezone.now()
    with transaction.atomic():
        row = (OpenWearablesConnection.objects.select_for_update()
               .filter(code_hash=digest, code_expires_at__gt=now).select_related('person').first())
        if row is None or not record_permitted(row.person):
            return _json({'detail': NOT_FOUND}, 404)
        # Single use: a second phone needs a second code.
        row.code_hash = ''
        row.code_expires_at = None
        row.save(update_fields=['code_hash', 'code_expires_at'])
        # A patient who keeps reinstalling does not collect live tokens.
        keep = list(row.devices.order_by('-created_at', '-id')
                    .values_list('pk', flat=True)[:open_wearables.MAX_DEVICES - 1])
        row.devices.exclude(pk__in=keep).delete()
        refresh_token, refresh_hash = open_wearables.new_refresh_token()
        device = OpenWearablesDevice.objects.create(
            connection=row, refresh_hash=refresh_hash, last_used_at=now,
            expires_at=now + open_wearables.REFRESH_TOKEN_LIFETIME)
    return _json({**_tokens(device, refresh_token), 'user_id': str(row.user_id)})


@csrf_exempt
@require_POST
def refresh(request):
    """A new access token for a phone's refresh token.

    The refresh token is not rotated: a phone that never received the answer
    (a lost connection) would be signed out for good, and what the token can
    do is add wearable values to one record until its patient disconnects it.
    """
    if not open_wearables.enabled():
        return _off()
    refused = _limited(RefreshThrottle(), request)
    if refused is not None:
        return refused
    body = _small_body(request)
    digest = open_wearables.refresh_digest(body.get('refresh_token')) if body else None
    now = timezone.now()
    device = (OpenWearablesDevice.objects.select_related('connection')
              .filter(refresh_hash=digest, expires_at__gt=now).first()) if digest else None
    if device is None:
        return _json({'detail': BAD_TOKEN}, 401)
    device.last_used_at = now
    device.expires_at = now + open_wearables.REFRESH_TOKEN_LIFETIME
    device.save(update_fields=['last_used_at', 'expires_at'])
    return _json(_tokens(device))


def _device(request, user_id):
    """The signed-in phone a request's bearer token names, or the refusal.

    401 for a token that is missing, expired or no longer names a phone (the
    SDK refreshes on 401 and tries once more); 403 for a good token used on
    another user's path.
    """
    header = request.META.get('HTTP_AUTHORIZATION', '')
    scheme, _, token = header.partition(' ')
    claims = open_wearables.read_access_token(token.strip()) if scheme.lower() == 'bearer' else None
    if claims is None:
        return None, _json({'detail': BAD_TOKEN}, 401)
    token_user, device_id = claims
    if not open_wearables.same_user(token_user, str(user_id).lower()):
        return None, _json({'detail': WRONG_USER}, 403)
    device = (OpenWearablesDevice.objects.select_related('connection')
              .filter(pk=device_id, connection__user_id=token_user,
                      expires_at__gt=timezone.now()).first())
    if device is None:
        return None, _json({'detail': BAD_TOKEN}, 401)
    refused = _limited(DeviceThrottle(device.pk), request)
    if refused is not None:
        return None, refused
    return device, None


# ── The phone: data ─────────────────────────────────────────────────────────

def _ingest(device, provider, device_type, batch):
    """Count one batch into the patient's days and write the days that changed.

    Returns ``(person to re-derive or None, what happened)``. The patient is
    locked first and the connection second, with NO KEY UPDATE on the patient,
    as the writer asks: a clinical insert for the person is not held up.
    """
    from rest_framework.response import Response as DrfResponse

    from omop_core.services.mappings import resolve_wearable_mappings

    from .views import PatientRecordViewSet

    now = timezone.now()
    label = f'{open_wearables.HISTORY_LABELS[device_type]} {now:%Y-%m}'
    outcome = {'stored': 0, 'repeated': 0, 'late': 0, 'unmapped': 0}
    # A metric with no approved mapping is not counted at all: a day counted
    # now and never written would not be written when its mapping is approved
    # either, since only a day that grows is written.
    mapped = {key for key, concept in resolve_wearable_mappings(device_type).items() if concept}
    readings = [reading for reading in batch.readings if reading.metric_key in mapped]
    outcome['unmapped'] = len(batch.readings) - len(readings)

    with transaction.atomic():
        person = (Person.objects.select_for_update(no_key=True, of=('self',))
                  .filter(open_wearables_connection=device.connection_id).first())
        row = (OpenWearablesConnection.objects.select_for_update()
               .filter(pk=device.connection_id).first())
        if person is None or row is None or not row.devices.filter(pk=device.pk).exists():
            return None, None       # disconnected while this request was read
        if not record_permitted(person):
            return None, outcome    # the record is off: nothing is kept
        open_wearables.close_idle_days(row, now)
        applied = open_wearables.apply_readings(row, device_type, readings, now)
        outcome.update(stored=applied.counted, repeated=applied.repeated, late=applied.late)
        row.last_sync_at = now
        if batch.newest is not None:
            newest = batch.newest.isoformat()
            synced = dict(row.synced_until or {})
            if newest > synced.get(provider, ''):
                synced[provider] = newest
            row.synced_until = synced
        row.save(update_fields=['last_sync_at', 'synced_until'])
        if not applied.samples:
            return None, outcome
        # A day's value here is the day's total so far, so it replaces what
        # this device's uploads and syncs wrote for that day (replace_daily).
        result = PatientRecordViewSet()._write_wearable_samples(
            row.created_by, person, device_type, applied.samples, label, True)
        if isinstance(result, DrfResponse):
            # The server cannot store wearable rows at all (its type concept
            # is missing). Everything above is rolled back; the phone retries.
            raise RuntimeError(f'wearable rows cannot be stored (status {result.status_code})')
        _fold_history(person, label, result, device_type)
    changed = bool(result.get('samples_created') or result.get('replaced'))
    return (person if changed else None), outcome


@csrf_exempt
@require_POST
def sync(request, user_id):
    """One batch from the SDK: records, sleep stages, workouts.

    Answers 202 in the SDK's own shape. A body the reader cannot use at all is
    a 400 or 413; a record it cannot use is counted in the answer and the rest
    of the batch is kept. A failure of this server's own is a 503, which the
    SDK retries: counting a record twice is harmless.
    """
    if not open_wearables.enabled():
        return _off()
    device, refused = _device(request, user_id)
    if refused is not None:
        return refused
    raw = request.read(open_wearables.MAX_SYNC_BYTES + 1)
    if len(raw) > open_wearables.MAX_SYNC_BYTES:
        return _json({'detail': 'Request body too large.'}, 413)
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return _json({'detail': 'Send a JSON object with "provider" and "data".'}, 400)
    provider = open_wearables.PROVIDER_SLUGS.get(str(body.get('provider') or '').strip().lower())
    if provider is None:
        return _json({'detail': UNKNOWN_PROVIDER}, 400)
    answer = {'status_code': 202, 'response': 'Sync received', 'user_id': str(user_id)}
    if provider in open_wearables.IGNORED_PROVIDERS:
        return _json({**answer, 'ignored': True}, 202)
    device_type = open_wearables.PROVIDER_DEVICE[provider]
    try:
        batch = open_wearables.read_sync(body, device_type, time_zone=device.connection.time_zone)
    except open_wearables.SyncError as exc:
        return _json({'detail': exc.message}, exc.status)
    try:
        person, outcome = _ingest(device, provider, device_type, batch)
    except Exception:
        logger.exception('open_wearables_sync_failed connection_id=%s', device.connection_id)
        return _json({'detail': 'Could not store the sync. Try again.'}, 503)
    if outcome is None:
        return _json({'detail': BAD_TOKEN}, 401)
    if person is not None:
        _derive(person, 'open_wearables_sync')
    return _json({**answer, **outcome, 'rejected': batch.rejected,
                  'unrecognized': batch.unrecognized}, 202)


@require_http_methods(['GET'])
def sync_status(request, user_id):
    """Where the server says this user's sync stands for one provider.

    Only ``syncedUntil`` is known here: this deployment keeps day totals, not
    the per-type export ranges an Open Wearables server tracks.
    """
    if not open_wearables.enabled():
        return _off()
    device, refused = _device(request, user_id)
    if refused is not None:
        return refused
    provider = open_wearables.PROVIDER_SLUGS.get(str(request.GET.get('provider') or '').strip().lower())
    if provider is None:
        return _json({'detail': UNKNOWN_PROVIDER}, 400)
    return _json({
        'provider': provider,
        'historicalSyncCompleted': None,
        'syncedUntil': (device.connection.synced_until or {}).get(provider),
        'dataTypes': [],
    })


@csrf_exempt
@require_POST
def logs(request, user_id):
    """The SDK's own diagnostics. Accepted so the SDK does not retry them, and
    dropped unread: a phone's logs are not this deployment's to keep."""
    if not open_wearables.enabled():
        return _off()
    _device_row, refused = _device(request, user_id)
    if refused is not None:
        return refused
    return _json({'status_code': 202, 'response': 'Accepted'}, 202)


@csrf_exempt
@require_http_methods(['DELETE'])
def disconnect(request, user_id, provider):
    """The user signed out in the app: this phone's tokens stop working.

    Other phones on the connection, the day totals and the stored values stay.
    """
    if not open_wearables.enabled():
        return _off()
    device, refused = _device(request, user_id)
    if refused is not None:
        return refused
    if open_wearables.PROVIDER_SLUGS.get(provider.strip().lower()) is None:
        return _json({'detail': UNKNOWN_PROVIDER}, 400)
    OpenWearablesDevice.objects.filter(pk=device.pk).delete()
    response = HttpResponse(status=204)
    response['Cache-Control'] = 'no-store'
    return response
