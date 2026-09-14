import hashlib
import hmac
import json
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import SimpleRateThrottle
from drf_spectacular.utils import extend_schema

from omop_core.models import Organization, PatientRecord
from omop_core.services.access import get_admin_orgs
from patient_portal.models import InboundWebhookEvent, WebhookDelivery, WebhookSubscription
from patient_portal.webhooks import (
    EVENT_TYPES, INBOUND_HANDLERS, compute_hmac_signature, validate_webhook_url,
)
from .permissions import ScopedTokenPermission


class WebhookSubscriptionSerializer(serializers.ModelSerializer):
    event_types = serializers.ListField(
        child=serializers.ChoiceField(choices=EVENT_TYPES), allow_empty=False, max_length=4,
    )

    class Meta:
        model = WebhookSubscription
        fields = ['id', 'organization', 'url', 'event_types', 'active', 'created_at']
        read_only_fields = ['id', 'created_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        self.fields['organization'].queryset = (
            get_admin_orgs(request.user) if request else Organization.objects.none()
        )

    def validate_url(self, value):
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
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if not get_admin_orgs(request.user).exists():
            return False
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
                  'error', 'created_at', 'delivered_at']


class WebhookSubscriptionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, WebhookManagementPermission]
    serializer_class = WebhookSubscriptionSerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return WebhookSubscription.objects.filter(
            organization__in=get_admin_orgs(self.request.user),
        ).order_by('pk')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = serializer.save()
        return Response({**serializer.data, 'secret': subscription.secret}, status=status.HTTP_201_CREATED)

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
    throttle_classes = []  # Apply the source quota after authentication below.

    @extend_schema(request=InboundEventSerializer, responses={202: dict, 200: dict})
    def post(self, request):
        if not settings.WEBHOOKS_ENABLED:
            return Response({'detail': 'Webhooks are disabled.'}, status=503)
        source_id = request.headers.get('X-HealthKey-Source', '')
        source = settings.WEBHOOK_INBOUND_SOURCES.get(source_id, {})
        secret = source.get('secret', '')
        signature = request.headers.get('X-HealthKey-Signature', '')
        body = request.body
        timestamp = request.headers.get('X-HealthKey-Timestamp', '')
        try:
            fresh = (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12
                     and abs(time.time() - int(timestamp)) <= 300)
        except ValueError:
            fresh = False
        if (not fresh or not secret or len(source_id) > 100 or not hmac.compare_digest(
                signature.encode(), compute_hmac_signature(body, secret, timestamp).encode())):
            return Response({'detail': 'Invalid webhook signature.'}, status=401)
        self.source_id = source_id
        throttle = InboundWebhookThrottle()
        if not throttle.allow_request(request, self):
            self.throttled(request, throttle.wait())
        organization = Organization.objects.filter(slug=source.get('organization'), is_active=True).first()
        if organization is None:
            return Response({'detail': 'Invalid webhook source.'}, status=401)
        if len(body) > 65536:
            return Response({'detail': 'Webhook payload too large.'}, status=413)
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
            if not PatientRecord.objects.filter(
                person_id=event_data['data']['person_id'], organization=organization,
            ).exists():
                return Response({'detail': 'Unknown patient for this source.'}, status=400)
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
