"""DRF authentication backends.

PartnerAuthentication delegates to pluggable token providers configured
in PARTNER_AUTH_PROVIDERS.  Each provider first gets a lightweight
can_handle() check (unverified JWT payload inspection — no secrets,
no external calls) before the real verify() is invoked.

Verified tokens are cached for up to 60 seconds so repeated requests
with the same Bearer token skip provider.verify() and DB lookups.
"""
from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache as django_cache
from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed

from patient_portal.models import Identity

from .permissions import SERVICE_TOKEN
from .providers import get_providers
from .providers.base import TokenClaims, decode_jwt_unverified

logger = logging.getLogger(__name__)


def _token_cache_key(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"auth:partner:{digest}"


class PartnerAuthentication(BaseAuthentication):

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None

        token = header[7:]

        cached = self._from_cache(token)
        if cached is not None:
            return cached

        providers = get_providers()
        if not providers:
            return None

        unverified = decode_jwt_unverified(token)

        for provider in providers:
            if not provider.can_handle(token, unverified):
                continue

            try:
                claims = provider.verify(token)
            except AuthenticationFailed:
                raise
            except Exception:
                logger.warning(
                    "partner_auth: %s.verify failed",
                    type(provider).__name__,
                )
                continue

            if claims is None:
                continue

            identity = self._get_or_create_identity(claims)
            if not identity.is_active:
                return None
            _ensure_person(identity, claims)
            self._to_cache(token, identity.pk, claims)
            return (identity, claims)

        return None

    @staticmethod
    def _from_cache(token: str):
        data = django_cache.get(_token_cache_key(token))
        if data is None:
            return None
        try:
            identity = Identity.objects.get(pk=data["pk"])
        except Identity.DoesNotExist:
            return None
        if not identity.is_active:
            return None
        claims = TokenClaims(**data["claims"])
        return (identity, claims)

    @staticmethod
    def _to_cache(token: str, identity_pk: int, claims: TokenClaims):
        django_cache.set(
            _token_cache_key(token),
            {
                "pk": identity_pk,
                "claims": {
                    "issuer": claims.issuer,
                    "sub": claims.sub,
                    "email": claims.email,
                    "name": claims.name,
                    "raw": claims.raw,
                },
            },
            timeout=settings.AUTH_TOKEN_CACHE_TTL,
        )

    def authenticate_header(self, request):
        return "Bearer"

    @staticmethod
    def _get_or_create_identity(claims: TokenClaims) -> Identity:
        identity, created = Identity.objects.get_or_create_from_claims(claims)
        if created:
            if claims.email:
                identity.email = claims.email
            if claims.name:
                identity.name = claims.name
            identity.set_unusable_password()
            identity.save(update_fields=["email", "name", "password"])
            _claim_placeholder_access(identity, claims.email)
            logger.info(
                "partner_auth: provisioned identity %d (%s|%s)",
                identity.pk, claims.issuer, claims.sub,
            )
        elif claims.email and not identity.email:
            identity.email = claims.email
            if claims.name and not identity.name:
                identity.name = claims.name
                identity.save(update_fields=["email", "name"])
            else:
                identity.save(update_fields=["email"])
            _claim_placeholder_access(identity, claims.email)
        elif claims.email:
            _claim_placeholder_access(identity, claims.email)
        return identity


def _ensure_person(identity, claims=None):
    """Auto-provision an OMOP Person + PatientInfo + PatientUser."""
    from patient_portal.services import resolve_or_create_person

    email = ""
    if claims:
        email = claims.email or ""
    elif identity.email:
        email = identity.email

    resolve_or_create_person(identity, email=email)


def _claim_placeholder_access(identity: Identity, email: str | None) -> None:
    """Move invite grants from an unusable local placeholder to a real login identity."""
    if not email or identity.issuer == "urn:local":
        return

    from omop_core.models import GroupAccess

    placeholders = Identity.objects.filter(
        email__iexact=email,
        issuer="urn:local",
    ).exclude(pk=identity.pk)

    role_rank = {"org_admin": 3, "doctor": 2, "navigator": 1}
    for placeholder in placeholders:
        if placeholder.has_usable_password():
            continue

        for grant in list(GroupAccess.objects.filter(identity=placeholder)):
            existing = GroupAccess.objects.filter(
                identity=identity,
                org=grant.org,
                group=grant.group,
            ).first()
            if existing:
                if role_rank.get(existing.role, 0) < role_rank.get(grant.role, 0):
                    existing.role = grant.role
                    existing.granted_by = grant.granted_by
                    existing.expires_at = grant.expires_at
                    existing.save(update_fields=["role", "granted_by", "expires_at"])
                grant.delete()
            else:
                grant.identity = identity
                grant.save(update_fields=["identity"])


class ServiceTokenAuthentication(BaseAuthentication):
    """Authenticate service-to-service calls via a pre-shared Bearer token."""

    def authenticate(self, request):
        import hmac

        secret = getattr(settings, "SERVICE_AUTH_TOKEN", "").strip()
        if not secret:
            return None

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None

        if not hmac.compare_digest(header[7:], secret):
            return None

        identity, created = Identity.objects.get_or_create(
            issuer='urn:service', sub='hk-labs-sync',
        )
        if created:
            identity.set_unusable_password()
            identity.save(update_fields=['password'])

        return (identity, SERVICE_TOKEN)

    def authenticate_header(self, request):
        return "Bearer"


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """SessionAuthentication without the built-in CSRF enforcement."""

    def enforce_csrf(self, request):
        return
