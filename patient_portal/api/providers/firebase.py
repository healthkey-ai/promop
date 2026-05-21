from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

from .base import TokenClaims, TokenProvider

logger = logging.getLogger(__name__)

FIREBASE_ISS_PREFIX = "https://securetoken.google.com/"


class FirebaseTokenProvider(TokenProvider):
    """Verify Firebase ID tokens via firebase-admin SDK."""

    def can_handle(self, token: str, unverified_payload: dict[str, Any] | None) -> bool:
        if unverified_payload is None:
            return False
        iss = unverified_payload.get("iss", "")
        return iss.startswith(FIREBASE_ISS_PREFIX)

    def verify(self, token: str) -> TokenClaims | None:
        try:
            from firebase_admin import auth as firebase_auth
        except ImportError:
            return None

        check_revoked = not getattr(settings, "FIREBASE_SKIP_REVOCATION_CHECK", False)
        try:
            decoded = firebase_auth.verify_id_token(token, check_revoked=check_revoked)
        except firebase_auth.ExpiredIdTokenError:
            raise AuthenticationFailed("Firebase token expired")
        except firebase_auth.RevokedIdTokenError:
            raise AuthenticationFailed("Firebase token revoked")
        except firebase_auth.InvalidIdTokenError:
            return None
        except Exception as exc:
            logger.debug("Firebase verify failed (%s), skipping", type(exc).__name__)
            return None

        return TokenClaims(
            uid=decoded["uid"],
            email=decoded.get("email", ""),
            raw=decoded,
        )

    def user_lookup(self, claims: TokenClaims) -> tuple[str, str]:
        return ("email", claims.email)
