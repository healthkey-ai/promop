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
from patient_portal.service_applications import ALLOWED_SCOPES, issue_token
from .permissions import IsStaffPermission, ScopedTokenPermission, is_machine_request


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
        return ' '.join(sorted(scopes))


class TokenIssueSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=160)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError('Expiration must be in the future.')
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
            raise ValidationError('Enable the application before creating a token.')
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
