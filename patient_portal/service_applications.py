"""Issue and verify application tokens; never persist or log their plaintext."""
import hashlib
import secrets
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from patient_portal.models import ServiceAccessToken, ServiceApplication
from patient_portal.service_tokens import ServiceCredential

ALLOWED_SCOPES = frozenset({
    'patient/*.read', 'patient/*.write', 'user/*.read', 'user/*.write',
    'system/*.read', 'system/etl.write',
})


def token_digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_token(application, label, *, actor=None, expires_at=None):
    secret = secrets.token_urlsafe(48)
    record = ServiceAccessToken.objects.create(
        application=application, label=label, digest=token_digest(secret),
        suffix=secret[-4:], created_by=actor, expires_at=expires_at,
    )
    return record, secret


def stored_credential(secret):
    record = ServiceAccessToken.objects.select_related('application').filter(
        digest=token_digest(secret),
    ).first()
    if record is None:
        return None
    now = timezone.now()
    if (record.revoked_at is not None or not record.application.is_active
            or (record.expires_at is not None and record.expires_at <= now)):
        # A known but revoked key must never fall through to environment grants.
        raise AuthenticationFailed('Service credential is inactive.')
    ServiceAccessToken.objects.filter(pk=record.pk).filter(
        Q(last_used_at__isnull=True) | Q(last_used_at__lt=now - timedelta(minutes=5)),
    ).update(last_used_at=now)
    return ServiceCredential(record.application.service_id, record.application.scopes)


def check_environment_fallback(credential):
    application = ServiceApplication.objects.filter(service_id=credential.service_id).first()
    if application is not None and (not application.is_active or application.tokens.exists()):
        # Once an application has managed tokens, its environment keys cannot
        # reappear after rotation, revocation, or a scope edit.
        raise AuthenticationFailed('Use a managed token for this service application.')
