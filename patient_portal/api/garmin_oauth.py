"""Connecting a Garmin account by OAuth, and Garmin's webhook (#2802).

Five routes, all on ``/api/v1/`` only. The rules are in
``omop_core.services.garmin_oauth``.

| Route | Who | What |
|---|---|---|
| ``GET garmin/connection/`` | a signed-in patient | whether it is set up here and whether they are connected |
| ``DELETE garmin/connection/`` | a signed-in patient | disconnect: Garmin is told, the connection is removed |
| ``POST garmin/oauth/start/`` | a signed-in patient | the URL of Garmin's consent page for them |
| ``GET garmin/oauth/callback/`` | Garmin's redirect | stores the connection, returns the patient to their record |
| ``POST garmin/webhooks/<secret>/`` | Garmin | pushed summaries, deregistrations, permission changes |

A connection is the person's own act: the three patient routes want the person
signed in (``is_interactive_session``), never a service credential or a token
they delegated to an application.
"""
import hashlib
import hmac
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from omop_core.models import GarminConnection, Person, WearableUpload
from omop_core.services import garmin_oauth
from patient_portal.consent import RECORD_OFF, record_permitted

from .permissions import is_interactive_session

logger = logging.getLogger(__name__)

# Where the callback sends the patient: their own record, which reads the
# `garmin` parameter, opens the Wearables tab and says what happened.
RETURN_PATH = '/?garmin={outcome}'
# How often one consent may come back with a code. Each is a request to
# Garmin's token endpoint; a real return is one, a reload a second.
CALLBACK_TRIES_PER_STATE = 5
# Garmin batches users and a daily summary carries a day of heart-rate samples,
# so a push runs to megabytes: far past DATA_UPLOAD_MAX_MEMORY_SIZE, which is
# sized for forms. Refusing one would only have Garmin send it again.
MAX_PUSH_BYTES = 10 * 1024 * 1024


class GarminConnectThrottle(UserRateThrottle):
    """Starting or ending a connection, per signed-in user. Each one is a
    round trip to Garmin; a patient needs a handful, nobody needs hundreds."""
    scope = 'garmin_connect'
    rate = '30/hour'

    def allow_request(self, request, view):
        # Reading the status is what the Wearables tab does every time it opens.
        return request.method == 'GET' or super().allow_request(request, view)


def _own_person(request):
    """The signed-in patient's person, or the Response that refuses."""
    from patient_portal.models import PatientUser

    if not is_interactive_session(request):
        return None, Response({'error': 'Connecting a Garmin account is for a signed-in patient.'},
                              status=status.HTTP_403_FORBIDDEN)
    try:
        return PatientUser.objects.get(identity=request.user).person, None
    except PatientUser.DoesNotExist:
        return None, Response({'error': 'No patient record linked to your account'},
                              status=status.HTTP_403_FORBIDDEN)


def _describe(connection):
    return {
        'configured': garmin_oauth.configured(),
        'connected': connection is not None,
        'connected_at': connection.connected_at.isoformat() if connection else None,
        'last_push_at': (connection.last_push_at.isoformat()
                         if connection and connection.last_push_at else None),
        'permissions': list(connection.permissions or []) if connection else [],
    }


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
@throttle_classes([GarminConnectThrottle])
def connection(request):
    """The caller's own Garmin connection: read it, or end it."""
    person, refusal = _own_person(request)
    if refusal is not None:
        return refusal
    if request.method == 'GET':
        return Response(_describe(GarminConnection.objects.filter(person=person).first()))

    # Garmin's terms: a partner that offers "Disconnect" must delete the user's
    # registration, so Garmin stops sending. The row is locked across the call
    # because a token refresh retires the stored refresh token.
    with transaction.atomic():
        row = GarminConnection.objects.select_for_update().filter(person=person).first()
        if row is None:
            return Response({'disconnected': True, 'garmin_notified': False})
        notified = True
        try:
            garmin_oauth.deregister(garmin_oauth.usable_access_token(row))
        except garmin_oauth.GarminOAuthError as exc:
            # The patient asked to stop: it stops here whatever Garmin says.
            # Pushes for an account with no connection are dropped, and the
            # page tells them to remove the app in Garmin Connect too.
            notified = False
            logger.warning('garmin_deregister_failed person_id=%s status=%s',
                           person.person_id, exc.status)
        row.delete()
    logger.info('garmin_disconnected person_id=%s garmin_notified=%s', person.person_id, notified)
    return Response({'disconnected': True, 'garmin_notified': notified})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([GarminConnectThrottle])
def oauth_start(request):
    """The URL of Garmin's consent page for the caller's own record."""
    person, refusal = _own_person(request)
    if refusal is not None:
        return refusal
    if not record_permitted(person):
        return Response({'error': RECORD_OFF}, status=status.HTTP_403_FORBIDDEN)
    try:
        url = garmin_oauth.authorize_url(garmin_oauth.new_state(person.person_id, request.user.pk))
    except garmin_oauth.GarminOAuthError as exc:
        return Response({'error': exc.message}, status=exc.status)
    return Response({'authorize_url': url})


def _back(outcome):
    response = HttpResponseRedirect(RETURN_PATH.format(outcome=outcome))
    # The request URL held a one-time code: it is not passed on as a referrer
    # (and never_cache on the view keeps the answer out of any cache).
    response['Referrer-Policy'] = 'no-referrer'
    return response


@never_cache
@require_GET
def oauth_callback(request):
    """Garmin sends the patient back here with a code, or without one.

    Whose connection this is comes from the signed state, not from the session:
    the browser may arrive without one. A browser that *is* signed in as
    somebody else is refused, so a link built from one person's state cannot
    be used to attach a Garmin account while another person is at the keyboard.
    """
    read = garmin_oauth.read_state(request.GET.get('state'))
    if read is None:
        return _back('error')
    person_id, identity_id, verifier = read
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and user.pk != identity_id:
        return _back('error')
    code = request.GET.get('code')
    if not code:
        # The patient declined on Garmin's page, or Garmin reported an error.
        return _back('denied')
    # A state is good for fifteen minutes; it is not a way to send Garmin's
    # token endpoint any number of guesses in that time.
    tries_key = 'garmin-oauth-callback:' + hashlib.sha256(request.GET['state'].encode()).hexdigest()
    cache.add(tries_key, 0, garmin_oauth.STATE_MAX_AGE_SECONDS)
    try:
        tries = cache.incr(tries_key)
    except ValueError:      # evicted between the two calls
        tries = 1
    if tries > CALLBACK_TRIES_PER_STATE:
        return _back('error')

    try:
        tokens = garmin_oauth.exchange_code(code, verifier)
        garmin_user_id = garmin_oauth.fetch_user_id(tokens.access_token)
        permissions = garmin_oauth.fetch_permissions(tokens.access_token)
    except garmin_oauth.GarminOAuthError as exc:
        logger.warning('garmin_connect_failed person_id=%s status=%s', person_id, exc.status)
        return _back('error')

    from patient_portal.models import PatientUser

    with transaction.atomic():
        person = Person.objects.select_for_update(no_key=True).filter(pk=person_id).first()
        # Asked again here: minutes passed on Garmin's page. The account that
        # started this must still be this person's own.
        if (person is None or not record_permitted(person)
                or not PatientUser.objects.filter(identity_id=identity_id, person=person).exists()):
            return _back('error')
        # A push names only the Garmin user, so one account feeds one record.
        if GarminConnection.objects.filter(garmin_user_id=garmin_user_id).exclude(person=person).exists():
            logger.warning('garmin_connect_account_in_use person_id=%s', person_id)
            return _back('in_use')
        row = GarminConnection.objects.select_for_update().filter(person=person).first() \
            or GarminConnection(person=person)
        if row.pk and row.garmin_user_id != garmin_user_id:
            # They connected a different Garmin account: the one before is told
            # to stop, or it would keep sending pushes nobody here receives.
            try:
                garmin_oauth.deregister(garmin_oauth.usable_access_token(row))
            except garmin_oauth.GarminOAuthError as exc:
                logger.warning('garmin_deregister_failed person_id=%s status=%s', person_id, exc.status)
        row.garmin_user_id = garmin_user_id
        row.permissions = permissions
        row.connected_by_id = identity_id
        garmin_oauth.store_tokens(row, tokens)
        row.save()
    logger.info('garmin_connected person_id=%s permissions=%s', person_id, permissions)
    return _back('connected')


# ── Webhook ─────────────────────────────────────────────────────────────────

def _fold_history(person, label, result):
    """One history row per patient per day of pushes, not one per push.

    Garmin pushes whenever the watch syncs. The writer records each write as
    an upload; the first push of a day keeps its row and later ones are folded
    into it, and a push that changed nothing leaves no row at all.
    """
    rows = list(WearableUpload.objects.filter(
        person=person, device_type='garmin', filename=label).order_by('id'))
    if not rows:
        return
    newest = rows[-1]
    if not result.get('samples_created') and not result.get('replaced'):
        newest.delete()
        return
    if len(rows) == 1:
        return
    first = rows[0]
    first.sample_summary = list(first.sample_summary or []) + list(newest.sample_summary or [])
    first.samples_created += newest.samples_created
    first.duplicates_skipped += newest.duplicates_skipped
    first.save(update_fields=['sample_summary', 'samples_created', 'duplicates_skipped'])
    newest.delete()


def _keep_tokens_alive(row):
    """Renew a connection's tokens when a push shows it live and they are old.

    Nothing reads Garmin for a push connection, so nothing else would refresh
    them, and an expired refresh token cannot tell Garmin about a disconnect.
    """
    updated = row.tokens_updated_at
    if updated is not None and timezone.now() - updated < garmin_oauth.REFRESH_AFTER:
        return
    refresh = garmin_oauth.decrypt(row.refresh_token)
    if not refresh:
        return
    try:
        garmin_oauth.store_tokens(row, garmin_oauth.refresh_tokens(refresh))
    except garmin_oauth.GarminOAuthError as exc:
        logger.warning('garmin_token_refresh_failed person_id=%s status=%s', row.person_id, exc.status)
        # Not tried again on every push today.
        row.tokens_updated_at = timezone.now() - garmin_oauth.REFRESH_AFTER + timedelta(days=1)
    row.save(update_fields=['access_token', 'refresh_token', 'access_expires_at',
                            'refresh_expires_at', 'tokens_updated_at'])


def _ingest(garmin_user_id, samples):
    """Write one pushed user's samples to the patient connected as that user.

    Returns the person to re-derive, or None when nothing changed. Samples for
    a Garmin user nobody here is connected as are dropped.
    """
    from rest_framework.response import Response as DrfResponse

    from .views import PatientRecordViewSet

    label = f'Garmin Connect push {timezone.now().date().isoformat()}'
    with transaction.atomic():
        row = (GarminConnection.objects.select_for_update()
               .filter(garmin_user_id=garmin_user_id).select_related('person').first())
        if row is None:
            return None
        person = row.person
        # The writer's own rule: one wearable write per patient at a time.
        Person.objects.select_for_update(no_key=True).filter(pk=person.pk).first()
        row.last_push_at = timezone.now()
        row.save(update_fields=['last_push_at'])
        _keep_tokens_alive(row)
        if not samples or not record_permitted(person):
            return None
        # A day's pushed value is the day's total so far, so it replaces what
        # Garmin uploads and syncs wrote for that day (replace_daily).
        result = PatientRecordViewSet()._write_wearable_samples(
            None, person, 'garmin', samples, label, True)
        if isinstance(result, DrfResponse):
            logger.error('garmin_push_not_stored person_id=%s status=%s',
                         person.person_id, result.status_code)
            return None
        _fold_history(person, label, result)
    changed = bool(result.get('samples_created') or result.get('replaced'))
    return person if changed else None


@csrf_exempt
@require_POST
def webhook(request, secret):
    """Garmin's push: summaries, deregistrations and permission changes.

    The secret in the path is the only thing that says this is Garmin (a push
    is not signed), so anything else is a 404 that says nothing. Garmin sends
    again whatever is not answered 200: a body this server cannot read is
    refused once and for all, and a failure of this server's own (the database)
    is a 503 so the same push comes back. Writing is idempotent.
    """
    expected = garmin_oauth.webhook_secret()
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        return HttpResponse(status=404)
    # Read from the stream, with this view's own limit: request.body would
    # apply the form limit.
    raw = request.read(MAX_PUSH_BYTES + 1)
    if len(raw) > MAX_PUSH_BYTES:
        return JsonResponse({'error': 'Body too large.'}, status=413)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return JsonResponse({'error': 'Body is not JSON.'}, status=400)

    push = garmin_oauth.read_push(payload)
    failed = False

    if push.deregistered:
        # The patient removed this app in Garmin Connect.
        removed, _ = GarminConnection.objects.filter(garmin_user_id__in=push.deregistered).delete()
        logger.info('garmin_push_deregistered connections=%d', removed)
    for garmin_user_id, permissions in push.permissions.items():
        GarminConnection.objects.filter(garmin_user_id=garmin_user_id).update(permissions=permissions)

    from omop_core.services.derivation_jobs import get_dispatcher

    for garmin_user_id, samples in push.samples.items():
        if garmin_user_id in push.deregistered:
            continue
        try:
            person = _ingest(garmin_user_id, samples)
        except Exception:
            failed = True
            logger.exception('garmin_push_failed')
            continue
        if person is not None:
            try:
                # Off the request where a worker exists: Garmin wants its 200
                # quickly and a derivation takes seconds.
                get_dispatcher().dispatch(person)
            except Exception:
                logger.exception('garmin_push_derivation_failed person_id=%s', person.person_id)

    if failed:
        return JsonResponse({'error': 'Try again.'}, status=503)
    return JsonResponse({})
