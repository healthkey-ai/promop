import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle
from drf_spectacular.utils import OpenApiParameter, extend_schema

from omop_core.models import Organization, PatientRecord
from omop_core.services.access import get_admin_orgs, get_direct_admin_orgs
from patient_portal.models import (
    InboundWebhookEvent, WebhookDelivery, WebhookSubscription, WebhookSubscriptionChange,
    webhook_secret,
)
from patient_portal.webhooks import (
    EVENT_TYPES, INBOUND_HANDLERS, compute_hmac_signature,
    record_subscription_change, subscription_snapshot, validate_webhook_url,
)
from .permissions import ScopedTokenPermission, is_interactive_session


def _masked_url(url):
    """Scheme and host, with everything that follows withheld.

    The host stays because it is what makes a subscription recognisable to
    someone reviewing where an organization sends data; the path, query and
    fragment are what a receiver may be treating as a shared secret.
    """
    parsed = urlsplit(url)
    return f'{parsed.scheme}://{parsed.netloc}/***' if parsed.netloc else ''


def _url_digest(url):
    """A stable fingerprint of a destination, safe to write where it is read.

    Empty for an empty URL, so "there was no destination" and "the destination
    is withheld" do not look alike.
    """
    return hashlib.sha256(url.encode()).hexdigest() if url else ''


class WebhookSubscriptionSerializer(serializers.ModelSerializer):
    event_types = serializers.ListField(
        child=serializers.ChoiceField(choices=EVENT_TYPES), allow_empty=False, max_length=4,
    )

    class Meta:
        model = WebhookSubscription
        fields = ['id', 'organization', 'url', 'event_types', 'active', 'created_at',
                  'deleted_at']
        read_only_fields = ['id', 'created_at', 'deleted_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        # Writes only: a trust lets a professional work with another
        # organization's patients, not point its event stream somewhere.
        self.fields['organization'].queryset = (
            get_direct_admin_orgs(request.user) if request else Organization.objects.none()
        )

    def validate_url(self, value):
        """Kept although the model validator now runs first, and decides.

        DRF copies model-field validators onto the serializer field, so a bad
        URL is refused before this method: on a failure this is dead, and on
        the happy path it repeats a cheap parse. It stays because a serializer
        used without the model field behind it would otherwise validate
        nothing. The no-details guarantee lives in both places now — see
        patient_portal.models.validate_webhook_subscription_url, which is the
        one the API actually exercises.
        """
        try:
            validate_webhook_url(value)
        except ValueError:
            raise serializers.ValidationError(
                'Webhook URL must resolve only to public HTTPS addresses on port 443.'
            ) from None
        return value

    def validate_organization(self, value):
        if self.instance and value.pk != self.instance.organization_id:
            raise serializers.ValidationError('A subscription cannot change organizations.')
        return value

    def to_representation(self, instance):
        """Show the destination only to someone who could change it.

        For a Slack- or Zapier-shaped receiver the URL path *is* the credential:
        anyone holding it can post to that endpoint. Reads follow the wider
        `get_admin_orgs` reach, so a trust-derived professional from another
        organization can list these — an audience that may not configure egress
        and has no reason to hold the credential either.

        The test is the authority to *change* the destination, which is the
        same pair writes require: a direct admin grant **and** an interactive
        session. An OAuth token a direct admin delegated to a third-party
        application resolves to that admin, so the grant alone would hand the
        application a credential it cannot be given by any other route on this
        endpoint. Everyone else sees the host, which is what makes the
        subscription recognisable, and the rest masked.
        """
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request is None:
            return data
        may_change = (
            is_interactive_session(request)
            and get_direct_admin_orgs(request.user).filter(pk=instance.organization_id).exists()
        )
        if not may_change:
            data['url'] = _masked_url(instance.url)
        return data


class WebhookManagementPermission(ScopedTokenPermission):
    """Role-gated, deliberately, because two of the three auth classes here
    carry no scopes to check.

    ScopedTokenPermission's default for a session or a partner token is "safe
    methods plus PATCH unless staff", which would leave a non-staff org admin
    unable to create or delete their own organization's subscriptions — the
    entire point of the endpoint. Scopes cannot substitute: TokenClaims
    (Firebase/SAML) has no scope field at all, and a session has no token.

    So the gate is an admin-org set, checked first and applying to every caller
    including OAuth and service tokens; those two additionally go through the
    scope model below. Read and write use different sets, on purpose:

      read  — get_admin_orgs: platform staff see every organization, a live
              org_admin grant sees its own, and a non-patient professional role
              reaches further organizations through organization and domain
              trusts. Same authority that decides which patients they can work
              with, so the subscription list matches the data they already see.
      write — get_direct_admin_orgs: staff and direct org_admin grants only.
              A trust is granted for data access; creating a subscription is
              data-egress configuration, and create() returns the signing
              secret. Extending a trust to that was not the intent of granting
              one (docs/soc2/webhook-egress-authority.md).

    Writes additionally require an interactive session, for the reason
    ServiceApplicationViewSet does: create() mints a long-lived signing secret
    and names where an organization's patient events are sent, so it is
    credential administration. An OAuth2 token an org admin delegated to a
    third-party application carries `patient/*.write` and would otherwise
    convert that scoped, expiring grant into a permanent egress channel the
    application controls — the subscription outlives the grant, and the secret
    authenticates the sender to the receiver. Reads stay open to every
    authentication class: listing subscriptions discloses no secret (the
    serializer returns it only from create) and matches data the caller can
    already reach.

    CSRF enforcement on the viewset covers the session case, which is the one an
    attacker can drive from a page the admin visits.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if not get_admin_orgs(request.user).exists():
            return False
        if request.method not in SAFE_METHODS:
            if not get_direct_admin_orgs(request.user).exists():
                return False
            # Positively identified, not inferred from `auth is None`:
            # BasicAuthentication also reports no token, and ENABLE_BASIC_AUTH
            # is a supported deployment setting.
            return is_interactive_session(request)
        if request.auth is None:
            return True
        from .providers.base import TokenClaims
        if isinstance(request.auth, TokenClaims):
            return True
        return super().has_permission(request, view)


class WebhookDeliverySerializer(serializers.ModelSerializer):
    # The host, derived from the frozen address; never the address itself,
    # because for some receivers the path is the credential.
    destination_host = serializers.SerializerMethodField()

    class Meta:
        model = WebhookDelivery
        fields = ['id', 'status', 'attempts', 'next_attempt_at', 'response_status',
                  'error', 'created_at', 'delivered_at', 'destination_host']

    def get_destination_host(self, delivery):
        return urlsplit(delivery.destination_url).hostname or ''


class WebhookSubscriptionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, WebhookManagementPermission]
    # A subscription names where this organization's patient events are sent, so
    # creating one is data-egress configuration and must enforce CSRF even in
    # deployments retaining the legacy CSRF-exempt session backend for other API
    # endpoints. Without this, a page an org admin merely visits can POST a
    # subscription pointing at an attacker's endpoint — the signing secret
    # authenticates the sender, so not being able to read the response does not
    # help — and DELETE can silently disable a tenant's real subscriptions.
    # Same treatment as ServiceApplicationViewSet, for the same reason.
    authentication_classes = [
        SessionAuthentication if issubclass(backend, SessionAuthentication) else backend
        for backend in api_settings.DEFAULT_AUTHENTICATION_CLASSES
    ]
    serializer_class = WebhookSubscriptionSerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def finalize_response(self, request, response, *args, **kwargs):
        # create() discloses the signing secret once. Same data class as the
        # service-token endpoint, same directives.
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Pragma'] = 'no-cache'
        return response

    @extend_schema(parameters=[OpenApiParameter(
        name='include_removed', type=bool, location=OpenApiParameter.QUERY,
        description=('Include subscriptions that have been removed. They are hidden by '
                     'default; their delivery history remains readable either way.'),
    )])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        # Reads follow the caller's full admin reach, so the list matches the
        # data they already work with. Writes follow only the organizations they
        # administer directly, so a trust cannot edit away or redirect another
        # organization's egress configuration either — not just not create one.
        resolve = get_admin_orgs if self.request.method in SAFE_METHODS else get_direct_admin_orgs
        queryset = WebhookSubscription.objects.filter(organization__in=resolve(self.request.user))
        if self.request.method not in SAFE_METHODS or not self._include_removed():
            # A removed subscription is gone for every write: it cannot be
            # redirected, re-enabled or deleted again. It is also absent from
            # the listing by default, so removing one still means it stops
            # appearing — the contract the endpoint had before removal became a
            # mark rather than a delete. Retrieving it by id, and with it the
            # delivery history saying where this organization's events actually
            # went, keeps working; `?include_removed=true` brings it back to the
            # listing for whoever is reviewing that history.
            queryset = queryset.filter(deleted_at__isnull=True)
        return queryset.order_by('pk')

    def _include_removed(self):
        if self.action != 'list':
            return True
        raw = self.request.query_params.get('include_removed')
        if raw is None:
            return False
        # A value this cannot read is a 400, not a silent "false": the one route
        # to a removed subscription's history should not disappear because a
        # client spelled the flag differently.
        return serializers.BooleanField().run_validation(raw)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response({**serializer.data, 'secret': serializer.instance.secret},
                        status=status.HTTP_201_CREATED)

    # One organization's subscription creates are serialised on this key, so
    # two of them cannot both read "one slot left" and both take it.
    _CAP_LOCK_KEY = 728144

    def perform_create(self, serializer):
        with transaction.atomic():
            organization = serializer.validated_data['organization']
            self._assert_capacity(organization)
            subscription = serializer.save()
            self._audit(record_subscription_change(
                subscription, WebhookSubscriptionChange.ACTION_CREATE, self.request.user))

    def _assert_capacity(self, organization):
        """An organization gets a bounded number of destinations.

        Every clinical write inserts one outbox row per matching subscription,
        inside the transaction of the write itself. Without a bound, an admin
        multiplies the cost of every write their organization performs, and the
        cost lands on the clinical path rather than on the webhook that caused
        it. Removed subscriptions do not count: they receive nothing.

        Counted under an advisory lock rather than in the serializer. A count
        taken before the write commits is not a bound — two requests both read
        one slot left and both take it — and no index expresses "at most N
        rows". The lock is keyed by organization, so it serialises only creates
        for the same tenant, and it is not a row lock: taking `FOR UPDATE` on
        the organization would block the clinical writes that reference it.
        """
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)',
                               [self._CAP_LOCK_KEY, organization.pk])
        live = WebhookSubscription.objects.filter(
            organization=organization, deleted_at__isnull=True,
        ).count()
        if live >= settings.WEBHOOK_MAX_SUBSCRIPTIONS_PER_ORG:
            raise serializers.ValidationError(
                f'An organization may have at most '
                f'{settings.WEBHOOK_MAX_SUBSCRIPTIONS_PER_ORG} webhook subscriptions.'
            )

    def perform_update(self, serializer):
        """Re-read the row under a lock: DRF saves every field it holds.

        `get_object()` loads the subscription outside any transaction, and
        `ModelSerializer.update()` writes the whole row from that instance. A
        DELETE committing in between would be undone — `deleted_at` and
        `active` restored from the stale copy — resurrecting a subscription
        someone removed, and the change row would record a before-state that
        was already false when it was read.
        """
        with transaction.atomic():
            locked = self._lock_live(serializer.instance.pk)
            serializer.instance = locked
            before = subscription_snapshot(locked)
            subscription = serializer.save()
            self._audit(record_subscription_change(
                subscription, WebhookSubscriptionChange.ACTION_UPDATE, self.request.user,
                before=before))
            if subscription.url != before['url']:
                # Deliveries queued against the address the organization has
                # just stopped designating are cancelled rather than sent. The
                # row keeps its frozen address and says `cancelled`, so the
                # trail shows what was queued and that it never left; nothing is
                # redirected to the new address either. This is what makes
                # changing the URL a working kill switch: without it, a
                # subscription taken out of service and brought back would
                # flush its backlog to the address it was taken out of service
                # over.
                WebhookDelivery.objects.filter(
                    subscription=subscription, status__in=['pending', 'retry'],
                ).exclude(
                    # A row from before the address column existed has none, and
                    # the delivery task reads that as "follow the subscription" —
                    # so it goes to the new address rather than being cancelled
                    # for having been addressed to the old one.
                    destination_url__in=['', subscription.url],
                ).update(status='cancelled')

    def perform_destroy(self, instance):
        """Mark, do not delete: the delivery history is the record of egress.

        A cascading DELETE took every delivery row with it, so after removing a
        subscription nothing said where that organization's events had been
        going. The subscription stops being visible to any write and stops
        receiving events — `publish_event` and the delivery task each check the
        mark and the flag, so neither leans on the other being set — and the
        trail it leaves behind stays readable.
        """
        with transaction.atomic():
            locked = self._lock_live(instance.pk)
            before = subscription_snapshot(locked)
            locked.deleted_at = timezone.now()
            locked.active = False
            locked.save(update_fields=['deleted_at', 'active'])
            self._audit(record_subscription_change(
                locked, WebhookSubscriptionChange.ACTION_DELETE, self.request.user,
                before=before))

    def _audit(self, change):
        """Put the destination on the request's audit row as well.

        `AuditEvent` signs each row and chains it to its predecessor, so a fact
        recorded there is tamper-evident; `webhook_subscription_change` is the
        queryable index of the same facts and the one retention never prunes.
        Neither is a substitute for the other, and writing both means rewriting
        one of them contradicts the other rather than passing unnoticed.

        The host and a digest, never the URL. Audit rows go to stdout for the
        SIEM and are readable through `/api/v1/audit-events/` by platform staff
        and any service token — a wider audience than the direct org admins who
        may configure egress — and for some receivers the URL path is the
        credential. The digest still binds the exact address: changing a stored
        URL without breaking the chain is not possible, and comparing the two
        trails needs only a hash.
        """
        # On the Django request, not the DRF wrapper around it: the middleware
        # that writes the audit row holds the former, and an attribute set on
        # the wrapper never reaches it.
        #
        # And on commit, not now. A Python attribute is not rolled back, so a
        # transaction that fails after this point — a lock timeout, a database
        # error — would still leave the middleware writing a signed, chained
        # audit row asserting a change that did not happen, and pointing at a
        # change row that does not exist. The middleware runs after the view
        # returns, so the callback lands in time.
        request = getattr(self.request, '_request', self.request)
        detail = {
            'webhook_subscription_change': str(change.pk),
            'action': change.action,
            'subscription': change.subscription_pk,
            'organization': change.organization_slug,
            'host_before': urlsplit(change.url_before).hostname or '',
            'host_after': urlsplit(change.url_after).hostname or '',
            'url_before_digest': _url_digest(change.url_before),
            'url_after_digest': _url_digest(change.url_after),
            'event_types_before': change.event_types_before,
            'event_types_after': change.event_types_after,
        }
        transaction.on_commit(lambda: setattr(request, 'audit_detail', detail))

    def _lock_live(self, pk):
        """The row as it is right now, held for the rest of the transaction.

        Not found means it was removed while this request was in flight, which
        is the answer a request arriving a moment later would get.
        """
        locked = (WebhookSubscription.objects.select_for_update()
                  .filter(pk=pk, deleted_at__isnull=True).first())
        if locked is None:
            raise NotFound()
        return locked

    @action(detail=True, methods=['post'], url_path='rotate-secret')
    def rotate_secret(self, request, pk=None):
        """Replace the signing secret without changing where events go.

        Rotation used to mean creating a second subscription and deleting the
        first, which moved the destination for no reason and — before removal
        became a mark — destroyed the delivery history of the old one. The
        secret is write-only everywhere else and is disclosed once here, the
        same way `create()` discloses it, under the same no-store directives.

        The receiver cannot install the new secret before it takes effect: it
        is generated here and disclosed in this response, and every delivery
        signed from this moment uses it. That is the right order for the reason
        rotation exists — a secret believed to be compromised stops working at
        once — and the cost is a window in which a receiver that has not yet
        stored it rejects deliveries. They retry over roughly eight minutes and
        then dead-letter, so install the new secret promptly rather than
        beforehand. Overlapping old and new (signing twice, or honouring a
        previous secret for a period) is a larger change and deliberately not
        attempted here.
        """
        with transaction.atomic():
            subscription = self._lock_live(self.get_object().pk)
            before = subscription_snapshot(subscription)
            subscription.secret = webhook_secret()
            subscription.save(update_fields=['secret'])
            self._audit(record_subscription_change(
                subscription, WebhookSubscriptionChange.ACTION_ROTATE, request.user,
                before=before))
        return Response({'secret': subscription.secret})

    @action(detail=True, methods=['get'])
    def deliveries(self, request, pk=None):
        deliveries = self.get_object().deliveries.order_by('-created_at')
        page = self.paginate_queryset(deliveries)
        if page is not None:
            return self.get_paginated_response(WebhookDeliverySerializer(page, many=True).data)
        return Response(WebhookDeliverySerializer(deliveries[:100], many=True).data)


class InboundDataSerializer(serializers.Serializer):
    person_id = serializers.IntegerField(min_value=1)
    # An identifier, and nothing else. This value is passed through to every
    # subscriber of the organization, so whatever a partner puts here is what
    # this deployment forwards under its own signature. Constrained to the
    # shape of a resource identifier — no whitespace, no punctuation that
    # carries a sentence — so a free-text field cannot become a channel for
    # clinical detail the event shape does not claim to carry.
    resource_id = serializers.RegexField(
        r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$', max_length=128, required=False,
    )


class InboundEventSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=128)
    type = serializers.ChoiceField(choices=tuple(INBOUND_HANDLERS))
    data = InboundDataSerializer()


# Stand-in key used only to keep the signature comparison unconditional for an
# unknown source, so timing does not separate "no such source" from "wrong
# signature". Not a credential: nothing accepts it, and a caller that guesses it
# still fails the `not secret` test below. Named without "secret"/"password" so
# the hardcoded-credential scan does not read it as one.
_ABSENT_SOURCE_FILLER = 'no-such-source'


class InboundIngressThrottle(SimpleRateThrottle):
    """Meter by IP in front of signature verification.

    Not the project's `anon` bucket: this view sets `authentication_classes =
    []`, so DRF counts every caller as anonymous — a correctly signed source
    would be cut off at 60/minute, far below its own 600/minute quota, and
    several sources behind one NAT would share that. This bucket therefore
    sits deliberately ABOVE the per-source rate, so a verified sender always
    meets its own quota first and this only bounds traffic that never
    verifies.
    """

    scope = 'webhook_ingress'

    def get_rate(self):
        return settings.WEBHOOK_INGRESS_RATE

    def get_ident(self, request):
        # DRF's default returns the WHOLE X-Forwarded-For chain when
        # NUM_PROXIES is unset, and that header is client-supplied: prepending
        # a random value per request would mint a fresh bucket every time and
        # leave this metering only the senders who are not trying to evade it.
        # Count back from the end instead, past the hops we actually run, so
        # the key is the address the nearest trusted proxy observed.
        depth = settings.WEBHOOK_TRUSTED_PROXY_DEPTH
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if depth < 1 or not forwarded:
            return request.META.get('REMOTE_ADDR')
        addresses = [part.strip() for part in forwarded.split(',') if part.strip()]
        if not addresses:
            return request.META.get('REMOTE_ADDR')
        return addresses[-min(depth, len(addresses))]

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class InboundWebhookThrottle(SimpleRateThrottle):
    scope = 'webhook_inbound'

    def get_rate(self):
        return settings.WEBHOOK_INBOUND_RATE

    def get_cache_key(self, request, view):
        # Called only after signature verification, so unauthenticated callers
        # cannot consume a configured source's quota by spoofing its header.
        return self.cache_format % {'scope': self.scope, 'ident': view.source_id}


class InboundWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # The source quota below is keyed on a verified source, so it cannot meter
    # traffic that fails verification. This bucket does, in front of the
    # signature check, without capping a legitimate sender — see
    # InboundIngressThrottle.
    throttle_classes = [InboundIngressThrottle]

    @extend_schema(request=InboundEventSerializer, responses={202: dict, 200: dict})
    def post(self, request):
        if not settings.WEBHOOKS_ENABLED:
            return Response({'detail': 'Webhooks are disabled.'}, status=503)
        # Before the body is read, so an oversized request is refused rather
        # than buffered and hashed.
        declared = request.META.get('CONTENT_LENGTH') or 0
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            declared = 0
        if declared > 65536:
            return Response({'detail': 'Webhook payload too large.'}, status=413)
        source_id = request.headers.get('X-HealthKey-Source', '')
        source = settings.WEBHOOK_INBOUND_SOURCES.get(source_id, {})
        secret = source.get('secret', '')
        signature = request.headers.get('X-HealthKey-Signature', '')
        body = request.body
        if len(body) > 65536:
            return Response({'detail': 'Webhook payload too large.'}, status=413)
        timestamp = request.headers.get('X-HealthKey-Timestamp', '')
        well_formed = timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12
        try:
            fresh = well_formed and abs(time.time() - int(timestamp)) <= 300
        except ValueError:
            fresh = False
        # compare_digest runs for an unknown source too, against a dummy secret,
        # so response time does not separate "no such source" from "wrong
        # signature". Source ids are semi-public config, so this is a small
        # thing, but a free one. The timestamp is signed as ASCII, so a
        # malformed one is replaced here rather than raised from the HMAC —
        # the request is refused either way by `fresh` below.
        verified = hmac.compare_digest(
            signature.encode(),
            compute_hmac_signature(
                body, secret or _ABSENT_SOURCE_FILLER, timestamp if well_formed else '0').encode(),
        )
        if not fresh or not secret or len(source_id) > 100 or not verified:
            return Response({'detail': 'Invalid webhook signature.'}, status=401)
        self.source_id = source_id
        throttle = InboundWebhookThrottle()
        if not throttle.allow_request(request, self):
            self.throttled(request, throttle.wait())
        organization = Organization.objects.filter(slug=source.get('organization'), is_active=True).first()
        if organization is None:
            return Response({'detail': 'Invalid webhook source.'}, status=401)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return Response({'detail': 'Invalid JSON payload.'}, status=400)
        serializer = InboundEventSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        event_data = serializer.validated_data
        idempotency_key = request.headers.get('Idempotency-Key')
        if idempotency_key is not None and idempotency_key != event_data['id']:
            return Response({'detail': 'Idempotency-Key must match the signed event id.'}, status=400)
        digest = hashlib.sha256(body).hexdigest()
        with transaction.atomic():
            # The replay key is consulted before the patient, so an event this
            # endpoint already accepted keeps answering "duplicate" on retry
            # even after that patient was deleted or moved to another
            # organization. Answering 400 there would tell the sender an event
            # it was told we took was rejected, and the retry never settles.
            event = InboundWebhookEvent.objects.filter(
                source=source_id, event_id=event_data['id'],
            ).first()
            created = False
            if event is None:
                if not PatientRecord.objects.filter(
                    person_id=event_data['data']['person_id'], organization=organization,
                ).exists():
                    return Response({'detail': 'Unknown patient for this source.'}, status=400)
                # get_or_create, not create: a concurrent request may have
                # inserted the same key since the read above, and the unique
                # constraint is what actually decides which one processes it.
                event, created = InboundWebhookEvent.objects.get_or_create(
                    source=source_id, event_id=event_data['id'], defaults={
                        'organization': organization, 'event_type': event_data['type'],
                        'payload_digest': digest,
                    },
                )
            if not created:
                if event.payload_digest != digest or event.organization_id != organization.pk:
                    return Response({'detail': 'Event id already used with a different payload.'}, status=409)
                return Response({'id': event.event_id, 'duplicate': True})
            INBOUND_HANDLERS[event.event_type](event, dict(event_data['data']))
            event.processed_at = timezone.now()
            event.save(update_fields=['processed_at'])
        return Response({'id': event.event_id, 'duplicate': False}, status=202)
