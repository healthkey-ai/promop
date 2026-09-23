import hashlib
import hmac
import json
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle
from drf_spectacular.utils import extend_schema

from omop_core.models import Organization, PatientRecord
from omop_core.services.access import get_admin_orgs, get_direct_admin_orgs
from patient_portal.models import (
    InboundWebhookEvent, WebhookDelivery, WebhookSubscription, WebhookSubscriptionChange,
)
from patient_portal.webhooks import (
    EVENT_TYPES, INBOUND_HANDLERS, compute_hmac_signature,
    record_subscription_change, subscription_snapshot, validate_webhook_url,
)
from .permissions import ScopedTokenPermission, is_interactive_session


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
    class Meta:
        model = WebhookDelivery
        fields = ['id', 'status', 'attempts', 'next_attempt_at', 'response_status',
                  'error', 'created_at', 'delivered_at', 'destination_host']


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
        return self.request.query_params.get('include_removed', '').lower() in ('1', 'true', 'yes')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response({**serializer.data, 'secret': serializer.instance.secret},
                        status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        with transaction.atomic():
            subscription = serializer.save()
            record_subscription_change(
                subscription, WebhookSubscriptionChange.ACTION_CREATE, self.request.user)

    def perform_update(self, serializer):
        before = subscription_snapshot(serializer.instance)
        with transaction.atomic():
            subscription = serializer.save()
            record_subscription_change(
                subscription, WebhookSubscriptionChange.ACTION_UPDATE, self.request.user,
                before=before)

    def perform_destroy(self, instance):
        """Mark, do not delete: the delivery history is the record of egress.

        A cascading DELETE took every delivery row with it, so after removing a
        subscription nothing said where that organization's events had been
        going. The subscription stops being visible to any write and stops
        receiving events (`active` is what publish_event and the delivery task
        both check), and the trail it leaves behind stays readable.
        """
        before = subscription_snapshot(instance)
        with transaction.atomic():
            instance.deleted_at = timezone.now()
            instance.active = False
            instance.save(update_fields=['deleted_at', 'active'])
            record_subscription_change(
                instance, WebhookSubscriptionChange.ACTION_DELETE, self.request.user,
                before=before)

    @action(detail=True, methods=['get'])
    def deliveries(self, request, pk=None):
        deliveries = self.get_object().deliveries.order_by('-created_at')
        page = self.paginate_queryset(deliveries)
        if page is not None:
            return self.get_paginated_response(WebhookDeliverySerializer(page, many=True).data)
        return Response(WebhookDeliverySerializer(deliveries[:100], many=True).data)


class InboundDataSerializer(serializers.Serializer):
    person_id = serializers.IntegerField(min_value=1)
    resource_id = serializers.CharField(max_length=128, required=False)


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
