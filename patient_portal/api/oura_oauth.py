"""Connecting an Oura account by OAuth, and syncing from Oura's API (#2900).

Four routes, all on ``/api/v1/`` only. The rules are in
``omop_core.services.oura_oauth``.

| Route | Who | What |
|---|---|---|
| ``GET oura/connection/`` | a signed-in patient | whether it is set up here and whether they are connected |
| ``DELETE oura/connection/`` | a signed-in patient | disconnect: the token is revoked at Oura, the connection removed |
| ``POST oura/oauth/start/`` | a patient signed in to this site | the URL of Oura's consent page for them |
| ``GET oura/oauth/callback/`` | the same browser, back from Oura | stores the connection, returns the patient to their record |
| ``POST oura/sync/`` | a signed-in patient | reads their days from Oura and stores them |

A connection is the person's own act: every route wants the person signed in
(``is_interactive_session``), never a service credential or a token they
delegated to an application. Connecting goes further and wants this site's own
session, because the callback is a page load and a session cookie is the only
thing a page load carries. Oura pushes nothing here, so there is no webhook.
"""
import hmac
import logging
import time

from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from omop_core.models import OuraConnection, Person
from omop_core.services import garmin_oauth, oura_oauth
from omop_core.services.oura_parser import samples_from_documents
from patient_portal.consent import RECORD_OFF, record_permitted

from .garmin_oauth import _derive, _fold_history
from .permissions import is_interactive_session

logger = logging.getLogger(__name__)

# Where the callback sends the patient: their own record, which reads the
# `oura` parameter, opens the Wearables tab and says what happened.
RETURN_PATH = '/?oura={outcome}'
# The session key holding the nonce of the consent this browser started.
SESSION_NONCE = 'oura_oauth_nonce'


class OuraConnectThrottle(UserRateThrottle):
    """Starting or ending a connection, per signed-in user. Each one is a
    round trip to Oura; a patient needs a handful, nobody needs hundreds.
    Counted beside the project's per-user limit, which covers the reads."""
    scope = 'oura_connect'
    rate = '30/hour'

    def allow_request(self, request, view):
        # Reading the status is what the Wearables tab does every time it opens.
        return request.method == 'GET' or super().allow_request(request, view)


class OuraSyncThrottle(UserRateThrottle):
    """A sync reads four endpoints at Oura, whose limit is shared by every
    patient of this deployment's app."""
    scope = 'oura_sync'
    rate = '20/hour'


def _own_person(request):
    """The signed-in patient's person, or the Response that refuses."""
    from patient_portal.models import PatientUser

    if not is_interactive_session(request):
        return None, Response({'error': 'Connecting an Oura account is for a signed-in patient.'},
                              status=status.HTTP_403_FORBIDDEN)
    try:
        return PatientUser.objects.get(identity=request.user).person, None
    except PatientUser.DoesNotExist:
        return None, Response({'error': 'No patient record linked to your account'},
                              status=status.HTTP_403_FORBIDDEN)


def _describe(connection):
    return {
        'configured': oura_oauth.configured(),
        'connected': connection is not None,
        'connected_at': connection.connected_at.isoformat() if connection else None,
        'last_sync_at': (connection.last_sync_at.isoformat()
                         if connection and connection.last_sync_at else None),
        'synced_through': (connection.synced_through.isoformat()
                           if connection and connection.synced_through else None),
    }


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle, OuraConnectThrottle])
def connection(request):
    """The caller's own Oura connection: read it, or end it."""
    person, refusal = _own_person(request)
    if refusal is not None:
        return refusal
    if request.method == 'GET':
        return Response(_describe(OuraConnection.objects.filter(person=person).first()))

    # The patient asked to stop: it stops here whatever Oura says. The token is
    # revoked so it stops working there too. The row is locked across the call
    # because a token refresh retires the stored refresh token.
    with transaction.atomic():
        row = OuraConnection.objects.select_for_update().filter(person=person).first()
        if row is None:
            return Response({'disconnected': True, 'oura_notified': False})
        notified = True
        try:
            oura_oauth.revoke(oura_oauth.usable_access_token(row))
        except oura_oauth.OuraOAuthError as exc:
            notified = False
            logger.warning('oura_revoke_failed person_id=%s status=%s', person.person_id, exc.status)
        row.delete()
    logger.info('oura_disconnected person_id=%s oura_notified=%s', person.person_id, notified)
    return Response({'disconnected': True, 'oura_notified': notified})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle, OuraConnectThrottle])
def oauth_start(request):
    """The URL of Oura's consent page for the caller's own record.

    The consent's nonce is left in the caller's session, and the callback
    finishes only for a browser that holds it: the state in the URL names the
    record, but a URL can be handed to somebody else.
    """
    person, refusal = _own_person(request)
    if refusal is not None:
        return refusal
    if not isinstance(getattr(request, 'successful_authenticator', None), SessionAuthentication):
        return Response({'error': 'Connect your Oura account from this site, signed in here.'},
                        status=status.HTTP_403_FORBIDDEN)
    if not record_permitted(person):
        return Response({'error': RECORD_OFF}, status=status.HTTP_403_FORBIDDEN)
    try:
        state = oura_oauth.new_state(person.person_id, request.user.pk)
        url = oura_oauth.authorize_url(state)
    except oura_oauth.OuraOAuthError as exc:
        return Response({'error': exc.message}, status=exc.status)
    request.session[SESSION_NONCE] = oura_oauth.read_state(state).nonce
    return Response({'authorize_url': url})


def _back(outcome):
    response = HttpResponseRedirect(RETURN_PATH.format(outcome=outcome))
    # The request URL held a one-time code: it is not passed on as a referrer
    # (and never_cache on the view keeps the answer out of any cache).
    response['Referrer-Policy'] = 'no-referrer'
    return response


def _abandon(access_token, person_id):
    """An access token that is not kept (Oura issued it and the account is not
    being connected here after all, or a new connection replaced it): revoke it,
    or it would stay good at Oura for nothing."""
    try:
        oura_oauth.revoke(access_token)
    except oura_oauth.OuraOAuthError as exc:
        logger.warning('oura_revoke_failed person_id=%s status=%s', person_id, exc.status)


@never_cache
@require_GET
def oauth_callback(request):
    """Oura sends the patient back here with a code, or without one.

    It finishes only for the browser that started: signed in as the account
    the state names, and holding that consent's nonce in its session. Without
    that, a consent link sent to somebody else would attach *their* Oura
    account to the sender's record, and a state read from a log or a history
    would attach the reader's account to its owner's. The nonce is spent on
    the first return, so a state is good for one exchange.
    """
    read = oura_oauth.read_state(request.GET.get('state'))
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
        # The patient declined on Oura's page, or Oura reported an error.
        return _back('denied')

    person_id = read.person_id
    try:
        tokens = oura_oauth.exchange_code(code, read.verifier)
    except oura_oauth.OuraOAuthError as exc:
        logger.warning('oura_connect_failed person_id=%s status=%s', person_id, exc.status)
        return _back('error')

    from patient_portal.models import PatientUser

    # Oura is only called outside the lock: a call here would hold the
    # patient's ETL and clinical writes for as long as Oura takes.
    superseded = None      # an earlier connection's access token, revoked after the commit
    refused = False
    with transaction.atomic():
        # Person first, then the connection: the order a sync takes them in.
        person = Person.objects.select_for_update(no_key=True).filter(pk=person_id).first()
        # Asked again here: minutes passed on Oura's page. The account that
        # started this must still be this person's own.
        if (person is None or not record_permitted(person)
                or not PatientUser.objects.filter(identity_id=read.identity_id, person=person).exists()):
            refused = True
        else:
            row = OuraConnection.objects.select_for_update().filter(person=person).first() \
                or OuraConnection(person=person)
            if row.pk:
                # Connecting again, perhaps another Oura account: the earlier
                # token is revoked as stored (an expired one is already dead),
                # and the first sync reads the recent days afresh.
                superseded = garmin_oauth.decrypt(row.access_token)
            row.last_sync_at = None
            row.synced_through = None
            row.connected_by_id = read.identity_id
            row.connected_at = timezone.now()
            oura_oauth.store_tokens(row, tokens)
            row.save()
    if refused:
        _abandon(tokens.access_token, person_id)
        return _back('error')
    if superseded:
        _abandon(superseded, person_id)
    logger.info('oura_connected person_id=%s', person_id)
    return _back('connected')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle, OuraSyncThrottle])
def sync(request):
    """Read the caller's days from Oura and store them as the export does.

    A day's value from Oura is the day's total, so it replaces what an export
    or an earlier sync wrote for that day (``replace_daily``). The first sync
    reads the last 90 days, a later one from a few days before where the
    last stopped. A sync that follows another within five minutes answers
    ``skipped: "recent"`` and reads nothing.

    JSON body: ``person_id``, the record the page is showing, which is refused
    unless it is the caller's own.
    """
    from .views import PatientRecordViewSet

    person, refusal = _own_person(request)
    if refusal is not None:
        return refusal
    body = request.data if isinstance(request.data, dict) else {}
    shown = body.get('person_id')
    if shown not in (None, '') and str(shown).strip() != str(person.person_id):
        return Response({'error': 'Oura sync writes to your own record only.'},
                        status=status.HTTP_403_FORBIDDEN)
    if not record_permitted(person):
        return Response({'error': RECORD_OFF}, status=status.HTTP_403_FORBIDDEN)
    row = OuraConnection.objects.filter(person=person).first()
    if row is None:
        return Response({'error': 'No Oura account is connected.', 'connected': False},
                        status=status.HTTP_404_NOT_FOUND)
    if oura_oauth.recently_synced(row):
        return Response({'skipped': 'recent', 'samples_created': 0, 'replaced': 0,
                         'last_sync_at': row.last_sync_at.isoformat()})
    if not oura_oauth.configured():
        return Response({'error': oura_oauth.NOT_CONFIGURED}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    # One deadline for the whole request: the refresh and the read share it.
    deadline = time.monotonic() + oura_oauth.BUDGET_SECONDS
    today = timezone.now().date()
    start, end = oura_oauth.sync_window(row.synced_through, today)
    try:
        # The refresh, if one is due, commits on its own before the read: it
        # retires the old refresh token at Oura.
        access = oura_oauth.fresh_access_token(person)
        if access is None:
            return Response({'error': 'No Oura account is connected.', 'connected': False},
                            status=status.HTTP_404_NOT_FOUND)
        fetched = oura_oauth.fetch_days(access, start, end, deadline=deadline)
    except oura_oauth.OuraOAuthError as exc:
        logger.info('oura_sync_refused person_id=%s status=%s upstream=%s',
                    person.person_id, exc.status, exc.upstream)
        return Response({'error': exc.message, 'reconnect': exc.status == 409}, status=exc.status)

    samples = samples_from_documents(fetched.documents)
    label = f'Oura Ring sync {today:%Y-%m}'
    result = {'samples_created': 0, 'duplicates_skipped': 0, 'replaced': 0}
    with transaction.atomic():
        # One wearable write per patient at a time (NO KEY UPDATE, so a
        # clinical insert for this person is not held up), and the record
        # asked again: seconds have passed at Oura.
        Person.objects.select_for_update(no_key=True).filter(pk=person.pk).first()
        if not record_permitted(person):
            return Response({'error': RECORD_OFF}, status=status.HTTP_403_FORBIDDEN)
        # The connection this read was made for, still: the patient may have
        # disconnected, or connected another account (the row is reused, so
        # its pk is the same), while Oura was being read. Locked after the
        # person, the order the callback takes them in.
        live = OuraConnection.objects.select_for_update().filter(
            pk=row.pk, connected_at=row.connected_at).first()
        if live is None:
            return Response({'error': oura_oauth.CHANGED, 'reconnect': False},
                            status=status.HTTP_409_CONFLICT)
        if samples:
            written = PatientRecordViewSet()._write_wearable_samples(
                request.user, person, 'oura', samples, label, True)
            if isinstance(written, Response):
                # The server cannot store wearable rows at all (its type
                # concept is missing).
                logger.error('oura_sync_unstorable status=%s', written.status_code)
                return Response({'error': 'Your Oura data could not be stored.'},
                                status=status.HTTP_503_SERVICE_UNAVAILABLE)
            result = written
            _fold_history(person, label, result, 'oura')
        update = {}
        if not fetched.incomplete:
            # Only a read that finished counts as a sync: "Sync again for the
            # rest" must not be answered "up to date".
            update['last_sync_at'] = timezone.now()
            # A kind Oura refused (a scope switched off, a lapsed membership)
            # leaves its days unread: the window stays where it was, so they
            # are read once Oura allows them.
            if not fetched.unavailable:
                update['synced_through'] = end
        if update:
            OuraConnection.objects.filter(pk=live.pk).update(**update)

    # A sync that stored nothing new can still have settled earlier rows
    # (#2835). Re-derived off the request where a worker exists: a bulk-loaded
    # record takes longer than what is left of the request, and the rows are
    # committed already.
    if result.get('samples_created') or result.get('replaced') or result.get('settled_rows'):
        _derive(person, source='oura_sync')
    result.update({
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'unavailable': fetched.unavailable,
        'incomplete': fetched.incomplete,
    })
    result.setdefault('unmapped_samples', 0)
    result.setdefault('unmapped_metrics', [])
    return Response(result)
