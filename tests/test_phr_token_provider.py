"""PhrTokenProvider — verification of phr-issued JWTs (JWKS + introspection)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework.exceptions import AuthenticationFailed

from patient_portal.api.providers.base import decode_jwt_unverified
from patient_portal.api.providers.phr import PhrTokenProvider, _JWKSCache, _jwks_cache

ISSUER = "healthkey-phr"


@pytest.fixture(autouse=True)
def _phr_settings(settings):
    settings.PHR_ISSUER = ISSUER
    settings.PHR_JWKS_URL = "http://phr.test/api/v1/auth/jwks/"
    settings.PHR_INTROSPECT_URL = "http://phr.test/api/v1/auth/introspect/"
    settings.PHR_JWKS_CACHE_TTL = 3600


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_of(key, kid="test-kid"):
    numbers = key.public_key().public_numbers()

    def b64url(v):
        import base64

        raw = v.to_bytes((v.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
            "n": b64url(numbers.n), "e": b64url(numbers.e)}


def _sign(key, claims, kid="test-kid", **headers):
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid, **headers})


def _access_claims(**extra):
    now = int(time.time())
    return {
        "iss": ISSUER, "token_type": "access", "user_id": 42,
        "email": "pat@example.com", "identity_level": "ial1",
        "iat": now, "exp": now + 300, **extra,
    }


def _mock_jwks_response(key, kid="test-kid"):
    resp = MagicMock()
    resp.json.return_value = {"keys": [_jwk_of(key, kid)]}
    resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def _fresh_cache():
    _jwks_cache._keys = {}
    _jwks_cache._fetched_at = 0.0
    yield


def test_can_handle_matches_phr_issuer_only():
    provider = PhrTokenProvider()
    assert provider.can_handle("t", {"iss": ISSUER}) is True
    assert provider.can_handle("t", {"iss": "https://securetoken.google.com/x"}) is False
    assert provider.can_handle("t", None) is False


def test_verify_valid_rs256_token(rsa_key):
    token = _sign(rsa_key, _access_claims())
    with patch("patient_portal.api.providers.phr.requests.get",
               return_value=_mock_jwks_response(rsa_key)):
        claims = PhrTokenProvider().verify(token)
    assert claims is not None
    assert claims.issuer == ISSUER
    assert claims.sub == "42"
    assert claims.email == "pat@example.com"


def test_verify_expired_token_raises(rsa_key):
    token = _sign(rsa_key, _access_claims(exp=int(time.time()) - 10))
    with patch("patient_portal.api.providers.phr.requests.get",
               return_value=_mock_jwks_response(rsa_key)):
        with pytest.raises(AuthenticationFailed):
            PhrTokenProvider().verify(token)


def test_verify_rejects_refresh_tokens(rsa_key):
    token = _sign(rsa_key, _access_claims(token_type="refresh"))
    with patch("patient_portal.api.providers.phr.requests.get",
               return_value=_mock_jwks_response(rsa_key)):
        assert PhrTokenProvider().verify(token) is None


def test_verify_rejects_wrong_signature(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _sign(other, _access_claims())  # signed by a different key
    with patch("patient_portal.api.providers.phr.requests.get",
               return_value=_mock_jwks_response(rsa_key)):
        assert PhrTokenProvider().verify(token) is None


def test_hs256_token_falls_back_to_introspection():
    token = jwt.encode(_access_claims(), "shared-secret", algorithm="HS256")
    active = MagicMock()
    active.json.return_value = {
        "active": True, "user_id": 42, "email": "pat@example.com",
        "identity_level": "ial1", "iss": ISSUER,
    }
    active.raise_for_status.return_value = None
    with patch("patient_portal.api.providers.phr.requests.post",
               return_value=active) as post:
        claims = PhrTokenProvider().verify(token)
    post.assert_called_once()
    assert claims is not None and claims.sub == "42"


def test_introspection_inactive_returns_none():
    token = jwt.encode(_access_claims(), "shared-secret", algorithm="HS256")
    inactive = MagicMock()
    inactive.json.return_value = {"active": False}
    inactive.raise_for_status.return_value = None
    with patch("patient_portal.api.providers.phr.requests.post",
               return_value=inactive):
        assert PhrTokenProvider().verify(token) is None


def test_jwks_cache_refreshes_on_unknown_kid(rsa_key):
    cache = _JWKSCache()
    with patch("patient_portal.api.providers.phr.requests.get",
               return_value=_mock_jwks_response(rsa_key, kid="k1")) as get:
        assert cache.get("k1", "http://phr.test/jwks/", 3600) is not None
        get.assert_called_once()
        # Unknown kid triggers exactly one refetch
        assert cache.get("k2", "http://phr.test/jwks/", 3600) is None
        assert get.call_count == 2


def test_provider_is_registered(settings):
    assert (
        "patient_portal.api.providers.phr.PhrTokenProvider"
        in settings.PARTNER_AUTH_PROVIDERS
    )


def test_unverified_routing_payload():
    token = jwt.encode(_access_claims(), "x", algorithm="HS256")
    payload = decode_jwt_unverified(token)
    assert payload is not None and payload["iss"] == ISSUER
