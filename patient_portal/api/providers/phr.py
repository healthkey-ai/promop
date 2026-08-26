from __future__ import annotations

import logging
import time
from typing import Any

import jwt
import requests
from django.conf import settings
from jwt import PyJWK
from rest_framework.exceptions import AuthenticationFailed

from .base import TokenClaims, TokenProvider

logger = logging.getLogger(__name__)


class _JWKSCache:
    """Fetch-once JWKS cache with TTL and kid-miss refresh."""

    def __init__(self) -> None:
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0

    def get(self, kid: str | None, url: str, ttl: int) -> PyJWK | None:
        stale = time.monotonic() - self._fetched_at > ttl
        if stale or (kid is not None and kid not in self._keys) or not self._keys:
            self._refresh(url)
        if kid is not None:
            return self._keys.get(kid)
        # Token has no kid header — usable only when exactly one key is published.
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    def _refresh(self, url: str) -> None:
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
        except Exception as exc:
            logger.warning("phr JWKS fetch failed (%s): %s", url, exc)
            return
        self._keys = {k["kid"]: PyJWK(k) for k in keys if k.get("kid")}
        self._fetched_at = time.monotonic()


_jwks_cache = _JWKSCache()


class PhrTokenProvider(TokenProvider):
    """Verify JWTs issued by the phr identity service.

    phr owns the user accounts for the service family. RS256 tokens are
    verified offline against phr's JWKS document; anything else (e.g. an
    HS256 dev deployment) falls back to phr's RFC 7662 introspection
    endpoint. Identity maps as (issuer=PHR_ISSUER, sub=<phr user id>).
    """

    def can_handle(self, token: str, unverified_payload: dict[str, Any] | None) -> bool:
        if unverified_payload is None:
            return False
        return unverified_payload.get("iss", "") == settings.PHR_ISSUER

    def verify(self, token: str) -> TokenClaims | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return None

        if header.get("alg") == "RS256":
            claims = self._verify_jwks(token, header.get("kid"))
        else:
            claims = self._verify_introspect(token)
        if claims is None:
            return None

        # Only access tokens grant API access — refresh tokens are for phr.
        if claims.get("token_type") not in (None, "access"):
            return None

        sub = claims.get("user_id") or claims.get("sub")
        if sub is None:
            return None

        return TokenClaims(
            issuer=settings.PHR_ISSUER,
            sub=str(sub),
            email=claims.get("email", ""),
            name=None,
            raw=claims,
            email_verified=bool(claims.get("email_verified", False)),
        )

    def _verify_jwks(self, token: str, kid: str | None) -> dict[str, Any] | None:
        key = _jwks_cache.get(kid, settings.PHR_JWKS_URL, settings.PHR_JWKS_CACHE_TTL)
        if key is None:
            logger.warning("phr JWKS has no usable key (kid=%s)", kid)
            return None
        audience = getattr(settings, "PHR_AUDIENCE", "")
        if not audience:
            logger.warning("phr audience validation is not configured")
            return None
        try:
            return jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=settings.PHR_ISSUER,
                audience=audience,
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("phr token expired")
        except jwt.InvalidTokenError as exc:
            logger.debug("phr token rejected: %s", exc)
            return None

    def _verify_introspect(self, token: str) -> dict[str, Any] | None:
        try:
            resp = requests.post(
                settings.PHR_INTROSPECT_URL, json={"token": token}, timeout=5
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("phr introspection failed: %s", exc)
            return None
        if not data.get("active"):
            return None
        return data
