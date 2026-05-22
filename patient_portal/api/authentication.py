"""DRF authentication backends.

PartnerAuthentication delegates to pluggable token providers configured
in PARTNER_AUTH_PROVIDERS.  Each provider first gets a lightweight
can_handle() check (unverified JWT payload inspection — no secrets,
no external calls) before the real verify() is invoked.
"""
from __future__ import annotations

import logging

from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed

from patient_portal.models import Identity

from .providers import get_providers
from .providers.base import TokenClaims, decode_jwt_unverified

logger = logging.getLogger(__name__)


class PartnerAuthentication(BaseAuthentication):

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None

        token = header[7:]
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
            _ensure_person(identity, claims)
            return (identity, claims)

        return None

    def authenticate_header(self, request):
        return "Bearer"

    @staticmethod
    def _get_or_create_identity(claims: TokenClaims) -> Identity:
        identity, created = Identity.objects.get_or_create_from_claims(claims)
        if created:
            identity.set_unusable_password()
            identity.save(update_fields=["password"])
            logger.info(
                "partner_auth: provisioned identity %d (%s|%s)",
                identity.pk, claims.issuer, claims.sub,
            )
        return identity


def _ensure_person(identity, claims=None):
    """Auto-provision an OMOP Person + PatientInfo + PatientUser."""
    from django.db import transaction
    from omop_core.models import PatientInfo, Person
    from patient_portal.models import PatientUser

    if PatientUser.objects.filter(identity=identity).exists():
        return

    email = ""
    if claims:
        email = claims.email or ""
    elif identity.email:
        email = identity.email

    if email:
        pi = PatientInfo.objects.filter(email=email).first()
        if pi:
            PatientUser.objects.get_or_create(identity=identity, defaults={'person': pi.person})
            return

    with transaction.atomic():
        last = Person.objects.select_for_update().order_by("-person_id").first()
        new_id = (last.person_id + 1) if last else 1000

        person = Person.objects.create(
            person_id=new_id,
            year_of_birth=1900,
            gender_source_value="unknown",
            race_source_value="unknown",
            ethnicity_source_value="unknown",
        )
        if email:
            PatientInfo.objects.create(person=person, email=email)

        PatientUser.objects.create(
            identity=identity,
            person=person,
        )
    logger.info(
        "partner_auth: auto-provisioned Person %d for identity pk=%d",
        new_id, identity.pk,
    )


class ServiceTokenAuthentication(BaseAuthentication):
    """Authenticate service-to-service calls via a pre-shared Bearer token."""

    def authenticate(self, request):
        import hmac
        from django.conf import settings

        secret = getattr(settings, "SERVICE_AUTH_TOKEN", "").strip()
        if not secret:
            return None

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None

        if not hmac.compare_digest(header[7:], secret):
            return None

        identity = Identity.objects.filter(is_superuser=True).first()
        if not identity:
            return None

        return (identity, "service-token")

    def authenticate_header(self, request):
        return "Bearer"


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """SessionAuthentication without the built-in CSRF enforcement."""

    def enforce_csrf(self, request):
        return
