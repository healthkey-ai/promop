"""DRF authentication backends.

PartnerAuthentication delegates to pluggable token providers configured
in PARTNER_AUTH_PROVIDERS.  Each provider first gets a lightweight
can_handle() check (unverified JWT payload inspection — no secrets,
no external calls) before the real verify() is invoked.

Verified tokens are cached for up to AUTH_TOKEN_CACHE_TTL seconds (default 60)
so repeated requests with the same Bearer token skip provider.verify() and DB
lookups.  A cache hit honours the token's own ``exp`` where the provider gives
one, and such an entry never outlives the token it came from — see issue #759 /
audit finding F21.  The TTL bounds the window in which a provider-side
revocation is not yet visible; that window cannot be closed without a round-trip
to the provider.

Where a provider returns no ``exp`` the cache falls back to the TTL alone, and
F21 is mitigated rather than closed for those tokens.  RFC 7662 makes ``exp``
optional in an introspection response, so the PHR non-RS256 path can land here.
"""
from __future__ import annotations

import hashlib
import logging
import time

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
                raise AuthenticationFailed("Account is disabled.")
            _ensure_person(identity, claims)
            self._to_cache(token, identity.pk, claims)
            return (identity, claims)

        return None

    @staticmethod
    def _token_expiry(claims_raw) -> int | None:
        """Return the token's ``exp`` as a unix timestamp, or None if absent.

        Providers hand back the decoded claim set as ``raw``. JWT issuers carry
        ``exp`` there, but an RFC 7662 introspection response need not — ``exp``
        is optional in that spec — so this returns None for those and the caller
        falls back to the cache TTL alone.
        """
        if not isinstance(claims_raw, dict):
            return None
        exp = claims_raw.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        return int(exp)

    @classmethod
    def _from_cache(cls, token: str):
        data = django_cache.get(_token_cache_key(token))
        if data is None:
            return None

        claim_data = dict(data["claims"])
        claim_data.setdefault("email_verified", False)

        # A cache hit is not a licence to skip expiry (audit finding F21, #759).
        # Verification happened when the entry was written; the token has been
        # ageing since. Without this an expired — or provider-revoked — token
        # keeps authenticating for the rest of the TTL window.
        expiry = cls._token_expiry(claim_data.get("raw"))
        if expiry is not None and time.time() >= expiry:
            django_cache.delete(_token_cache_key(token))
            return None

        try:
            identity = Identity.objects.get(pk=data["pk"])
        except Identity.DoesNotExist:
            return None
        if not identity.is_active:
            raise AuthenticationFailed("Account is disabled.")
        claims = TokenClaims(**claim_data)
        return (identity, claims)

    @classmethod
    def _to_cache(cls, token: str, identity_pk: int, claims: TokenClaims):
        # Never let the entry outlive the token. The configured TTL bounds the
        # revocation window — which cannot be closed without asking the provider
        # — but a token expiring sooner than that must take its cache entry with
        # it, so the two windows cannot compound.
        timeout = settings.AUTH_TOKEN_CACHE_TTL
        expiry = cls._token_expiry(claims.raw)
        if expiry is not None:
            remaining = expiry - int(time.time())
            if remaining <= 0:
                return
            timeout = min(timeout, remaining)

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
                    "email_verified": claims.email_verified,
                },
            },
            timeout=timeout,
        )

    def authenticate_header(self, request):
        return "Bearer"

    @staticmethod
    def _get_or_create_identity(claims: TokenClaims) -> Identity:
        identity, created = Identity.objects.get_or_create_from_claims(claims)
        if created:
            if claims.email and claims.email_verified:
                identity.email = claims.email
            if claims.name:
                identity.name = claims.name
            identity.set_unusable_password()
            identity.save(update_fields=["email", "name", "password"])
            _claim_placeholder_access(identity, claims.email, claims.email_verified)
            logger.info(
                "partner_auth: provisioned identity %d (%s|%s)",
                identity.pk, claims.issuer, claims.sub,
            )
        elif claims.email and claims.email_verified and not identity.email:
            identity.email = claims.email
            if claims.name and identity.name != claims.name:
                identity.name = claims.name
                identity.save(update_fields=["email", "name"])
            else:
                identity.save(update_fields=["email"])
            _claim_placeholder_access(identity, claims.email, claims.email_verified)
        elif claims.email and claims.email_verified:
            _claim_placeholder_access(identity, claims.email, claims.email_verified)
            if claims.name and identity.name != claims.name:
                identity.name = claims.name
                identity.save(update_fields=["name"])
        return identity


def _ensure_person(identity, claims=None):
    """Auto-provision an OMOP Person + PatientRecord + PatientUser."""
    from patient_portal.services import resolve_or_create_person

    email = ""
    if claims:
        email = claims.email or ""
        email_verified = claims.email_verified
    elif identity.email:
        email = identity.email
        email_verified = identity.is_local
    else:
        email_verified = False

    resolve_or_create_person(identity, email=email, email_verified=email_verified)


def _claim_placeholder_access(
    identity: Identity,
    email: str | None,
    email_verified: bool = False,
) -> None:
    """Move invite grants from an unusable local placeholder to a real login identity."""
    if not email or not email_verified or identity.issuer == "urn:local":
        return

    from omop_core.models import GroupAccess

    placeholders = Identity.objects.filter(
        email__iexact=email,
        issuer="urn:local",
    ).exclude(pk=identity.pk)

    role_rank = {"org_admin": 3, "doctor": 2, "analyst": 1, "patient": 0}
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
                    existing.redirect_url = grant.redirect_url if grant.role == "analyst" else ""
                    existing.save(
                        update_fields=["role", "granted_by", "expires_at", "redirect_url"]
                    )
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
