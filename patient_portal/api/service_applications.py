"""Staff application/token administration, with one-time secret disclosure."""
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings

from patient_portal.models import ServiceAccessToken, ServiceApplication
from patient_portal.service_applications import ALLOWED_SCOPES, MAX_TOKEN_LIFETIME, issue_token
from .permissions import (
    IsStaffPermission, ScopedTokenPermission, is_interactive_session, is_machine_request,
)


class ServiceAccessTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceAccessToken
        fields = ['id', 'label', 'suffix', 'created_at', 'created_by', 'expires_at',
                  'last_used_at', 'revoked_at', 'revoked_by']
        read_only_fields = fields


class ServiceApplicationSerializer(serializers.ModelSerializer):
    tokens = ServiceAccessTokenSerializer(many=True, read_only=True)

    class Meta:
        model = ServiceApplication
        fields = ['id', 'name', 'service_id', 'description', 'owner_contact', 'scopes',
                  'is_active', 'created_at', 'updated_at', 'tokens']
        read_only_fields = ['created_at', 'updated_at', 'tokens']

    def validate_service_id(self, value):
        if self.instance is not None and value != self.instance.service_id:
            raise serializers.ValidationError('Service ID cannot change; it identifies historical audit records.')
        return value

    def validate_scopes(self, value):
        scopes = set(value.split())
        if scopes - ALLOWED_SCOPES:
            raise serializers.ValidationError('Select supported service scopes.')
        if not scopes and self.instance is not None and self.instance.tokens.exists():
            # Blank is storable — migration 0019 seeds the legacy kill switch that
            # way on purpose — but clearing it after a token exists reaches the
            # same dead end as issuing one on a scopeless application: a live
            # token that grants nothing, with the environment fallback already
            # refused.
            raise serializers.ValidationError(
                'Clearing scopes would leave this application\'s tokens granting '
                'nothing. Revoke them first, or choose scopes.')
        return ' '.join(sorted(scopes))


class TokenIssueSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=160)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_expires_at(self, value):
        # None is not "no expiry" any more: issue_token substitutes the maximum,
        # so the form's blank field yields a bounded token rather than a
        # permanent one. This only rejects an explicit value outside the bound.
        if value is None:
            return value
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError('Expiration must be in the future.')
        if value > now + MAX_TOKEN_LIFETIME:
            raise serializers.ValidationError(
                f'Expiration cannot be more than {MAX_TOKEN_LIFETIME.days} days away.')
        return value


class ServiceApplicationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsStaffPermission, ScopedTokenPermission]
    # Token administration must enforce CSRF even in deployments retaining the
    # legacy CSRF-exempt session backend for other API endpoints.
    authentication_classes = [
        SessionAuthentication if issubclass(backend, SessionAuthentication) else backend
        for backend in api_settings.DEFAULT_AUTHENTICATION_CLASSES
    ]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    serializer_class = ServiceApplicationSerializer
    queryset = ServiceApplication.objects.prefetch_related('tokens').all()
    pagination_class = None

    def check_permissions(self, request):
        super().check_permissions(request)
        if is_machine_request(request):
            raise PermissionDenied('An authenticated staff user is required.')
        # IsStaffPermission only proves the *user* is staff. A third-party SMART
        # application holding that user's delegated `patient/*.write` grant would
        # otherwise convert it into a self-issued, cross-patient service token that
        # outlives the grant — the escalation this viewset exists to prevent.
        if not is_interactive_session(request):
            raise PermissionDenied(
                'Service credential administration requires an interactive staff session.')

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Pragma'] = 'no-cache'
        return response

    @action(detail=True, methods=['post'], url_path='tokens')
    @transaction.atomic
    def create_token(self, request, pk=None):
        application = self.get_object()
        if not application.is_active:
            raise ValidationError({'is_active': ['Enable the application before creating a token.']})
        if not application.scopes.strip():
            # Issuing here is a cutover: check_environment_fallback refuses the
            # environment grant as soon as this application has any token, and a
            # token carrying no scopes grants nothing — so the integration would
            # go down and its replacement would not work. hk-labs-sync is seeded
            # scopeless on purpose (migration 0019), which makes this reachable.
            raise ValidationError({'scopes': [
                'Set the application scopes before issuing a token: a token with no '
                'scopes grants nothing, and issuing one stops any environment '
                'credential for this service.']})
        serializer = TokenIssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record, secret = issue_token(application, actor=request.user, **serializer.validated_data)
        return Response({**ServiceAccessTokenSerializer(record).data, 'token': secret},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path=r'tokens/(?P<token_id>[0-9]+)/revoke')
    @transaction.atomic
    def revoke_token(self, request, pk=None, token_id=None):
        application = self.get_object()
        from django.shortcuts import get_object_or_404
        record = get_object_or_404(ServiceAccessToken, application=application, pk=token_id)
        if record.revoked_at is None:
            record.revoked_at = timezone.now()
            record.revoked_by = request.user
            record.save(update_fields=['revoked_at', 'revoked_by'])
        return Response(ServiceAccessTokenSerializer(record).data)
