"""DRF authentication backends.

PartnerAuthentication delegates to pluggable token providers configured
in PARTNER_AUTH_PROVIDERS.  Each provider first gets a lightweight
can_handle() check (unverified JWT payload inspection — no secrets,
no external calls) before the real verify() is invoked.
"""
from __future__ import annotations

import logging
import traceback

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from rest_framework.authentication import BaseAuthentication, SessionAuthentication

from .providers import get_providers
from .providers.base import decode_jwt_unverified

logger = logging.getLogger(__name__)

User = get_user_model()


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
            except Exception:
                logger.error(
                    "partner_auth: %s.verify raised:\n%s",
                    type(provider).__name__,
                    traceback.format_exc(),
                )
                raise

            if claims is None:
                continue

            field, value = provider.user_lookup(claims)
            user = self._get_or_create(provider, claims, field, value)
            return (user, None)

        return None

    @staticmethod
    def _get_or_create(provider, claims, field, value):
        created = False
        try:
            user = User.objects.get(**{field: value})
        except User.DoesNotExist:
            defaults = provider.provision_defaults(claims)
            email = defaults.pop("email", value if field == "email" else f"{value}@partner.local")
            try:
                user = User.objects.create_user(
                    username=email,
                    email=email,
                    **defaults,
                )
                user.set_unusable_password()
                user.save(update_fields=["password"])
                created = True
            except IntegrityError:
                user = User.objects.get(**{field: value})

        _ensure_person(user)
        if created:
            logger.info("partner_auth: provisioned user %d (%s)", user.pk, user.email)
        return user


def _ensure_person(user):
    """Auto-provision an OMOP Person + PatientInfo for a newly created user."""
    from omop_core.models import PatientInfo, Person

    if PatientInfo.objects.filter(email=user.email).exists():
        return

    last = Person.objects.order_by("-person_id").first()
    new_id = (last.person_id + 1) if last else 1000

    person = Person.objects.create(
        person_id=new_id,
        year_of_birth=1900,
        gender_source_value="unknown",
        race_source_value="unknown",
        ethnicity_source_value="unknown",
    )
    PatientInfo.objects.create(person=person, email=user.email)
    logger.info(
        "partner_auth: auto-provisioned Person %d + PatientInfo for %s",
        new_id, user.email,
    )


class ServiceTokenAuthentication(BaseAuthentication):
    """Authenticate service-to-service calls via a pre-shared Bearer token.

    Matches the token in the Authorization header against SERVICE_AUTH_TOKEN.
    Skipped when SERVICE_AUTH_TOKEN is empty (disabled by default).
    """

    def authenticate(self, request):
        from django.conf import settings

        secret = getattr(settings, "SERVICE_AUTH_TOKEN", "")
        if not secret:
            return None

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None

        if header[7:] != secret:
            return None

        user = User.objects.filter(is_superuser=True).first()
        if not user:
            return None

        return (user, "service-token")


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """SessionAuthentication without the built-in CSRF enforcement."""

    def enforce_csrf(self, request):
        return
