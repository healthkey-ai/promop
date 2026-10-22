"""Configured service principals; secrets never become request.auth or log fields."""
from dataclasses import dataclass
import json
import re

from django.core.exceptions import ImproperlyConfigured, ValidationError


ALLOWED_SCOPES = frozenset({
    'patient/*.read', 'patient/*.write', 'user/*.read', 'user/*.write',
    'system/*.read', 'system/etl.write',
})


def validate_service_scopes(value):
    """Field validator for ServiceApplication.scopes.

    The serializer and the token importer checked the cap; Django admin did
    not. As a field validator this covers every path that runs `full_clean()`
    — model forms, so Django admin — and DRF, which copies model validators
    onto the serializer field. A direct `create()`/`save()`/`update()` still
    bypasses it, as with any Django validator.

    Referenced by name in migration 0018: renaming or moving this function
    breaks `migrate` for every app. Leave a stub if it ever moves.
    """
    unsupported = set((value or '').split()) - ALLOWED_SCOPES
    if unsupported:
        raise ValidationError(
            'Unsupported service scope(s): %(scopes)s',
            params={'scopes': ' '.join(sorted(unsupported))},
        )


@dataclass(frozen=True)
class ServiceCredential:
    service_id: str
    scope: str


def parse_service_tokens(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ImproperlyConfigured("SERVICE_AUTH_TOKENS must be a JSON object.") from None
    # Validate at startup, including when no request has arrived yet.
    service_credentials(value)
    return value


def service_credentials(config, legacy_token=None, legacy_scopes="patient/*.read"):
    """Return validated (secret, credential) pairs, including the legacy grant."""
    if not isinstance(config, dict):
        raise ImproperlyConfigured("SERVICE_AUTH_TOKENS must be a JSON object.")
    entries = []
    for service_id, grant in config.items():
        if (not isinstance(service_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", service_id)
                or not isinstance(grant, dict)
                or set(grant) - {"token", "scopes"}):
            raise ImproperlyConfigured("Invalid SERVICE_AUTH_TOKENS service entry.")
        secret = grant.get("token")
        scope = grant.get("scopes", "patient/*.read")
        if (not isinstance(secret, str) or not secret or not secret.isascii()
                or any(c.isspace() for c in secret) or not isinstance(scope, str)):
            raise ImproperlyConfigured("Service tokens must be nonempty ASCII secrets with string scopes.")
        entries.append((secret, ServiceCredential(service_id, scope)))
    if legacy_token and legacy_token.strip():
        if "hk-labs-sync" in config:
            raise ImproperlyConfigured("Disable SERVICE_AUTH_TOKEN before configuring hk-labs-sync.")
        entries.append((legacy_token.strip(), ServiceCredential("hk-labs-sync", legacy_scopes)))
    if len({secret for secret, _ in entries}) != len(entries):
        raise ImproperlyConfigured("Each service credential must have a distinct token.")
    return entries
