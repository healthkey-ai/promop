from __future__ import annotations

import abc
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def decode_jwt_unverified(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without signature verification.

    Used only for routing — deciding which provider should handle the
    token before any secrets or external calls are involved.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


@dataclass
class TokenClaims:
    """Normalized result of a successful token verification."""

    uid: str
    email: str
    raw: dict[str, Any]


class TokenProvider(abc.ABC):
    """Abstract base for partner authentication providers.

    Each concrete provider knows how to verify a specific kind of bearer
    token (Firebase ID token, a foreign JWT signed with a shared secret,
    an opaque OAuth2 access token, etc.) and map it to a local user
    via a lookup field on the User model.
    """

    @abc.abstractmethod
    def can_handle(self, token: str, unverified_payload: dict[str, Any] | None) -> bool:
        """Lightweight check — does this token *look like* it belongs to
        this provider?  No secrets used, no external calls."""

    @abc.abstractmethod
    def verify(self, token: str) -> TokenClaims | None:
        """Return normalized claims if *token* is valid, or None if
        verification fails.  Raise AuthenticationFailed for tokens that
        are recognised but invalid/expired."""

    @abc.abstractmethod
    def user_lookup(self, claims: TokenClaims) -> tuple[str, str]:
        """Return (field_name, field_value) used to find or create the
        local User.  Example: ("email", "user@example.com")."""

    def provision_defaults(self, claims: TokenClaims) -> dict[str, Any]:
        """Extra field defaults when auto-creating a new User."""
        defaults: dict[str, Any] = {}
        if claims.email:
            defaults["email"] = claims.email
        return defaults
