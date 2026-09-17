"""Issue and verify application tokens; never persist or log their plaintext."""
import hashlib
import secrets
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from patient_portal.models import ServiceAccessToken, ServiceApplication
# ALLOWED_SCOPES lives in service_tokens so patient_portal.models can enforce it
# as a field validator without importing this module (which imports the models).
# Re-exported here for the callers that already import it from this module.
from patient_portal.service_tokens import (  # noqa: F401
    ALLOWED_SCOPES as ALLOWED_SCOPES,
    ServiceCredential,
)


def token_digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


# A service credential is a static bearer secret with no refresh step, so its
# lifetime is the whole of its exposure. Bounding only an explicitly supplied
# expiry would refuse a 366-day token while handing out a permanent one, which
# is what omitting the field used to do — and omitting it is the default path
# through the Org Admin form.
MAX_TOKEN_LIFETIME = timedelta(days=365)


def issue_token(application, label, *, actor=None, expires_at=None):
    # Both halves of the bound live here, so the ceiling holds for a caller that
    # does not go through the serializer. The serializer still rejects an
    # out-of-bounds value rather than silently clamping it, because a person who
    # typed a date deserves to be told it was refused.
    ceiling = timezone.now() + MAX_TOKEN_LIFETIME
    if expires_at is not None and timezone.is_naive(expires_at):
        # DRF hands over an aware datetime; a shell or management-command caller
        # may not, and comparing the two raises TypeError. Interpret it in the
        # configured timezone rather than failing on the clamp.
        expires_at = timezone.make_aware(expires_at)
    expires_at = ceiling if expires_at is None else min(expires_at, ceiling)
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
