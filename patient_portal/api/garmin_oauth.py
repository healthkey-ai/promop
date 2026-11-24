"""Connecting a Garmin account by OAuth, and Garmin's webhook (#2802).

Five routes, all on ``/api/v1/`` only. The rules are in
``omop_core.services.garmin_oauth``.

| Route | Who | What |
|---|---|---|
| ``GET garmin/connection/`` | a signed-in patient | whether it is set up here and whether they are connected |
| ``DELETE garmin/connection/`` | a signed-in patient | disconnect: Garmin is told, the connection is removed |
| ``POST garmin/oauth/start/`` | a patient signed in to this site | the URL of Garmin's consent page for them |
| ``GET garmin/oauth/callback/`` | the same browser, back from Garmin | stores the connection, returns the patient to their record |
| ``POST garmin/webhooks/<secret>/`` | Garmin | pushed summaries, deregistrations, permission changes |

A connection is the person's own act: the patient routes want the person
signed in (``is_interactive_session``), never a service credential or a token
they delegated to an application. Connecting goes further and wants this
site's own session, because the callback is a page load and a session cookie
is the only thing a page load carries.
"""
import hmac
import json
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
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
# The session key holding the nonce of the consent this browser started.
SESSION_NONCE = 'garmin_oauth_nonce'
# Garmin batches users and a daily summary carries a day of heart-rate samples,
# so a push runs to megabytes: far past DATA_UPLOAD_MAX_MEMORY_SIZE, which is
# sized for forms. Refusing one would only have Garmin send it again.
MAX_PUSH_BYTES = 10 * 1024 * 1024


class GarminConnectThrottle(UserRateThrottle):
    """Starting or ending a connection, per signed-in user. Each one is a
    round trip to Garmin; a patient needs a handful, nobody needs hundreds.
    Counted beside the project's per-user limit, which covers the reads."""
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
@throttle_classes([UserRateThrottle, GarminConnectThrottle])
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
@throttle_classes([UserRateThrottle, GarminConnectThrottle])
def oauth_start(request):
    """The URL of Garmin's consent page for the caller's own record.

    The consent's nonce is left in the caller's session, and the callback
    finishes only for a browser that holds it: the state in the URL names the
    record, but a URL can be handed to somebody else.
    """
    person, refusal = _own_person(request)
    if refusal is not None:
        return refusal
    if not isinstance(getattr(request, 'successful_authenticator', None), SessionAuthentication):
        return Response({'error': 'Connect your Garmin account from this site, signed in here.'},
                        status=status.HTTP_403_FORBIDDEN)
    if not record_permitted(person):
        return Response({'error': RECORD_OFF}, status=status.HTTP_403_FORBIDDEN)
    try:
        state = garmin_oauth.new_state(person.person_id, request.user.pk)
        url = garmin_oauth.authorize_url(state)
    except garmin_oauth.GarminOAuthError as exc:
        return Response({'error': exc.message}, status=exc.status)
    request.session[SESSION_NONCE] = garmin_oauth.read_state(state).nonce
    return Response({'authorize_url': url})


def _back(outcome):
    response = HttpResponseRedirect(RETURN_PATH.format(outcome=outcome))
    # The request URL held a one-time code: it is not passed on as a referrer
    # (and never_cache on the view keeps the answer out of any cache).
    response['Referrer-Policy'] = 'no-referrer'
    return response


def _abandon(tokens, person_id):
    """Garmin registered the account, and it is not being connected here after
    all: tell Garmin, or it would send pushes nobody receives."""
    try:
        garmin_oauth.deregister(tokens.access_token)
    except garmin_oauth.GarminOAuthError as exc:
        logger.warning('garmin_deregister_failed person_id=%s status=%s', person_id, exc.status)


@never_cache
@require_GET
def oauth_callback(request):
    """Garmin sends the patient back here with a code, or without one.

    It finishes only for the browser that started: signed in as the account
    the state names, and holding that consent's nonce in its session. Without
    that, a consent link sent to somebody else would attach *their* Garmin
    account to the sender's record, and a state read from a log or a history
    would attach the reader's account to its owner's. The nonce is spent on
    the first return, so a state is good for one exchange.
    """
    read = garmin_oauth.read_state(request.GET.get('state'))
    if read is None:
        return _back('error')
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated or not user.is_active or user.pk != read.identity_id:
        return _back('error')
    held = request.session.get(SESSION_NONCE)
    if not isinstance(held, str) or not hmac.compare_digest(held.encode(), read.nonce.encode()):
        return _back('error')
    del request.session[SESSION_NONCE]
    code = request.GET.get('code')
    if not code:
        # The patient declined on Garmin's page, or Garmin reported an error.
        return _back('denied')

    person_id = read.person_id
    try:
        tokens = garmin_oauth.exchange_code(code, read.verifier)
    except garmin_oauth.GarminOAuthError as exc:
        logger.warning('garmin_connect_failed person_id=%s status=%s', person_id, exc.status)
        return _back('error')
    try:
        garmin_user_id = garmin_oauth.fetch_user_id(tokens.access_token)
        permissions = garmin_oauth.fetch_permissions(tokens.access_token)
    except garmin_oauth.GarminOAuthError as exc:
        logger.warning('garmin_connect_failed person_id=%s status=%s', person_id, exc.status)
        _abandon(tokens, person_id)
        return _back('error')

    from patient_portal.models import PatientUser

    with transaction.atomic():
        # Person first, then the connection: the order a push takes them in.
        person = Person.objects.select_for_update(no_key=True).filter(pk=person_id).first()
        # Asked again here: minutes passed on Garmin's page. The account that
        # started this must still be this person's own.
        if (person is None or not record_permitted(person)
                or not PatientUser.objects.filter(identity_id=read.identity_id, person=person).exists()):
            _abandon(tokens, person_id)
            # The registration just deleted may have been this person's.
            GarminConnection.objects.filter(person_id=person_id, garmin_user_id=garmin_user_id).delete()
            return _back('error')
        # A push names only the Garmin user, so one account feeds one record.
        if GarminConnection.objects.filter(garmin_user_id=garmin_user_id).exclude(person=person).exists():
            logger.warning('garmin_connect_account_in_use person_id=%s', person_id)
            return _back('in_use')
        row = GarminConnection.objects.select_for_update().filter(person=person).first() \
            or GarminConnection(person=person)
        changed_account = bool(row.pk) and row.garmin_user_id != garmin_user_id
        if changed_account:
            # They connected a different Garmin account: the one before is told
            # to stop, or it would keep sending pushes nobody here receives.
            try:
                garmin_oauth.deregister(garmin_oauth.usable_access_token(row))
            except garmin_oauth.GarminOAuthError as exc:
                logger.warning('garmin_deregister_failed person_id=%s status=%s', person_id, exc.status)
        if changed_account or not row.pk:
            row.last_push_at = None
            row.day_readings = {}
        row.garmin_user_id = garmin_user_id
        row.permissions = permissions
        row.connected_by_id = read.identity_id
        row.connected_at = timezone.now()
        garmin_oauth.store_tokens(row, tokens)
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:
            # Another record connected the same Garmin account a moment ago.
            logger.warning('garmin_connect_account_in_use person_id=%s', person_id)
            return _back('in_use')
    logger.info('garmin_connected person_id=%s permissions=%s', person_id, permissions)
    return _back('connected')


# ── Webhook ─────────────────────────────────────────────────────────────────

def _fold_history(person, label, result, device_type='garmin'):
    """One history row per patient per month of pushes, not one per push.

    Garmin pushes whenever the watch syncs. The writer records each write as
    an upload; the first push of a month keeps its row and later ones are
    folded into it, and a push that changed nothing leaves no row at all. A
    folded row lists a metric and day once, with the value now stored: an
    entry a later push replaced named a row that is gone.
    """
    rows = list(WearableUpload.objects.filter(
        person=person, device_type=device_type, filename=label).order_by('id'))
    if not rows:
        return
    newest = rows[-1]
    if not result.get('samples_created') and not result.get('replaced'):
        newest.delete()
        return
    if len(rows) == 1:
        return
    first = rows[0]
    fresh = list(newest.sample_summary or [])
    replaced = {(entry.get('metric'), entry.get('date')) for entry in fresh}
    kept = [entry for entry in (first.sample_summary or [])
            if (entry.get('metric'), entry.get('date')) not in replaced]
    first.sample_summary = kept + fresh
    first.samples_created = sum(1 for entry in first.sample_summary if entry.get('row_id') is not None)
    first.duplicates_skipped = 0
    first.save(update_fields=['sample_summary', 'samples_created', 'duplicates_skipped'])
    newest.delete()


def _keep_tokens_alive(garmin_user_id):
    """Renew a connection's tokens when a push shows it live and they are old.

    Nothing reads Garmin for a push connection, so nothing else would refresh
    them, and an expired refresh token cannot tell Garmin about a disconnect.
    In a transaction of its own, after the push's rows are committed: a refresh
    retires the old refresh token at Garmin, so the new one must not be lost
    to a rollback of anything else. Only the connection's row is locked while
    Garmin is asked.
    """
    with transaction.atomic():
        row = GarminConnection.objects.select_for_update().filter(garmin_user_id=garmin_user_id).first()
        if row is None:
            return
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


def _ingest(garmin_user_id, samples, readings):
    """Write one pushed user's data to the patient connected as that user.

    Returns the person to re-derive, or None when nothing changed. The patient
    is locked first and the connection second, as the callback takes them, and
    the patient with NO KEY UPDATE, as the writer asks: a clinical insert for
    the person is not held up by a push.
    """
    from rest_framework.response import Response as DrfResponse

    from .views import PatientRecordViewSet

    today = timezone.now().date()
    label = f'Garmin Connect push {today:%Y-%m}'
    with transaction.atomic():
        person = (Person.objects.select_for_update(no_key=True, of=('self',))
                  .filter(garmin_connection__garmin_user_id=garmin_user_id).first())
        if person is None:
            return None
        row = (GarminConnection.objects.select_for_update()
               .filter(person=person, garmin_user_id=garmin_user_id).first())
        if row is None or not record_permitted(person):
            return None
        # Pulse ox and respiration: this push's readings join the day's others.
        row.day_readings, averaged = garmin_oauth.merge_readings(row.day_readings, readings, today)
        row.last_push_at = timezone.now()
        row.save(update_fields=['day_readings', 'last_push_at'])
        samples = list(samples) + averaged
        if not samples:
            return None
        # A day's pushed value is the day's total so far, so it replaces what
        # Garmin uploads and syncs wrote for that day (replace_daily).
        result = PatientRecordViewSet()._write_wearable_samples(
            None, person, 'garmin', samples, label, True)
        if isinstance(result, DrfResponse):
            # The server cannot store wearable rows at all (its type concept is
            # missing). Garmin keeps the push for a later try.
            raise RuntimeError(f'wearable rows cannot be stored (status {result.status_code})')
        _fold_history(person, label, result)
    # A push that stored nothing new can still have settled earlier rows (#2835).
    changed = bool(result.get('samples_created') or result.get('replaced') or result.get('settled_rows'))
    return person if changed else None


def _derive(person, source='garmin_push'):
    """Re-derive a patient's record after a push: off the request where a
    worker exists (Garmin wants its 200 quickly), here when queueing fails,
    because the next identical push would find nothing changed and not ask.
    ``source`` names the caller in the log (the phone sync shares this)."""
    from omop_core.services.derivation_jobs import get_dispatcher

    try:
        get_dispatcher().dispatch(person)
        return
    except Exception:
        logger.exception('%s_dispatch_failed person_id=%s', source, person.person_id)
    try:
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)
    except Exception:
        logger.exception('%s_derivation_failed person_id=%s', source, person.person_id)


def _answer(data, status_code=200):
    response = JsonResponse(data, status=status_code)
    # Django logs a 4xx or 5xx answer with the request's path, and this path
    # is the webhook's secret.
    response._has_been_logged = True
    return response


@csrf_exempt
@require_POST
def webhook(request, secret):
    """Garmin's push: summaries, deregistrations and permission changes.

    The secret in the path is the only thing that says this is Garmin (a push
    is not signed), so anything else is a 404 that says nothing. Garmin sends
    again whatever is not answered 200. A failure of this server's own (the
    database) is a 503, so the same push comes back, and writing is
    idempotent. A body that is not JSON, or is too large, is refused: neither
    is something Garmin sends, and both are logged.
    """
    expected = garmin_oauth.webhook_secret()
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        return HttpResponse(status=404)
    # Garmin is reported to name the app it is pushing for in this header. It
    # proves nothing by itself (anyone can send it), but a push naming another
    # app is not ours, so one that does is turned away; one without it is read.
    named = request.headers.get('garmin-client-id')
    if named and not hmac.compare_digest(named.strip().encode(), garmin_oauth.client_id().encode()):
        return HttpResponse(status=404)
    # Read from the stream, with this view's own limit: request.body would
    # apply the form limit.
    raw = request.read(MAX_PUSH_BYTES + 1)
    if len(raw) > MAX_PUSH_BYTES:
        logger.error('garmin_push_too_large limit=%d', MAX_PUSH_BYTES)
        return _answer({'error': 'Body too large.'}, 413)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        logger.error('garmin_push_not_json bytes=%d', len(raw))
        return _answer({'error': 'Body is not JSON.'}, 400)

    push = garmin_oauth.read_push(payload)
    failed = False

    # One query says which of the body's users are connected here; the rest
    # cost nothing more, however many a body names.
    named = (set(push.samples) | set(push.readings) | set(push.permissions)) - push.deregistered
    known = set(GarminConnection.objects.filter(garmin_user_id__in=named)
                .values_list('garmin_user_id', flat=True)) if named else set()

    for garmin_user_id in sorted(known & set(push.permissions)):
        GarminConnection.objects.filter(garmin_user_id=garmin_user_id).update(
            permissions=push.permissions[garmin_user_id])

    for garmin_user_id in sorted(known & (set(push.samples) | set(push.readings))):
        try:
            person = _ingest(garmin_user_id, push.samples.get(garmin_user_id, []),
                             push.readings.get(garmin_user_id, {}))
        except Exception:
            failed = True
            logger.exception('garmin_push_failed')
            continue
        if person is not None:
            _derive(person)
        try:
            _keep_tokens_alive(garmin_user_id)
        except Exception:
            logger.exception('garmin_token_refresh_failed')

    if push.deregistered:
        # The patient removed this app in Garmin Connect. Their data in the
        # same body was not stored.
        removed, _ = GarminConnection.objects.filter(garmin_user_id__in=push.deregistered).delete()
        logger.info('garmin_push_deregistered connections=%d', removed)

    if failed:
        return _answer({'error': 'Try again.'}, 503)
    return _answer({})
