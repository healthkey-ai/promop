"""Family tokens: RS256 JWTs PROMOP issues for its own identities.

The contract is the one phr has always exposed (docs/family-auth.md), so a
phr installation, and any sibling that verifies phr's tokens, can point at
PROMOP instead:

- access: ``iss`` (FAMILY_AUTH_ISSUER), ``aud`` (FAMILY_AUTH_AUDIENCE),
  ``sub`` (the Identity's uid), ``user_id``, ``email``, ``email_verified``,
  ``identity_level``, ``claims`` {ADMIN, MEDICAL_RECORDS}, ``person_id``.
- refresh: rotated on every use and recorded in RefreshToken.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import RefreshToken

ALGORITHM = "RS256"


class FamilyAuthError(Exception):
    """A family token that can't be accepted; the message is safe to return."""


def is_configured() -> bool:
    return bool(settings.FAMILY_AUTH_PRIVATE_KEY and settings.FAMILY_AUTH_ISSUER)


def _public_key_pem() -> str:
    if settings.FAMILY_AUTH_PUBLIC_KEY:
        return settings.FAMILY_AUTH_PUBLIC_KEY
    key = serialization.load_pem_private_key(settings.FAMILY_AUTH_PRIVATE_KEY.encode(), password=None)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk() -> dict:
    numbers = serialization.load_pem_public_key(_public_key_pem().encode()).public_numbers()
    jwk = {"kty": "RSA", "e": _b64url_uint(numbers.e), "n": _b64url_uint(numbers.n)}
    # RFC 7638 thumbprint as the key id, so a rotated key gets a new kid.
    canonical = json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()
    kid = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()
    return {**jwk, "use": "sig", "alg": ALGORITHM, "kid": kid}


def jwks_document() -> dict:
    return {"keys": [_jwk()]} if is_configured() else {"keys": []}


def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.FAMILY_AUTH_PRIVATE_KEY, algorithm=ALGORITHM,
                      headers={"kid": _jwk()["kid"]})


def _names(identity) -> tuple[str, str]:
    first, _, last = (identity.name or "").strip().partition(" ")
    return first, last.strip()


def person_id_of(identity) -> int | None:
    from patient_portal.models import PatientUser

    return PatientUser.objects.filter(identity=identity).values_list("person_id", flat=True).first()


def role_claims(identity) -> dict:
    """phr's two roles. ADMIN is PROMOP staff; MEDICAL_RECORDS is the
    "MEDICAL_RECORDS" group, granted in PROMOP's admin."""
    return {
        "ADMIN": bool(identity.is_staff),
        "MEDICAL_RECORDS": identity.groups.filter(name="MEDICAL_RECORDS").exists(),
    }


def identity_claims(identity) -> dict:
    verified = identity.has_verified_email
    return {
        "sub": identity.uid,
        "user_id": identity.pk,
        "email": identity.email,
        "email_verified": verified,
        # phr's levels: ial1 is a verified email. An unverified one is below it.
        "identity_level": "ial1" if verified else "ial0",
        "claims": role_claims(identity),
        "person_id": person_id_of(identity),
    }


def user_document(identity) -> dict:
    """The user as phr's /auth/me/ returns it (AppUser)."""
    claims = identity_claims(identity)
    first, last = _names(identity)
    return {
        "id": identity.pk,
        "email": identity.email,
        "first_name": first,
        "last_name": last,
        "identity_level": claims["identity_level"],
        "email_verified": claims["email_verified"],
        "is_admin": claims["claims"]["ADMIN"],
        "has_medical_records": claims["claims"]["MEDICAL_RECORDS"],
        "claims": claims["claims"],
        "person_id": claims["person_id"],
    }


def issue_pair(identity) -> dict:
    """A fresh access + refresh pair for an identity that just proved itself."""
    now = timezone.now()
    access_exp = now + timedelta(seconds=settings.FAMILY_AUTH_ACCESS_SECONDS)
    refresh_exp = now + timedelta(seconds=settings.FAMILY_AUTH_REFRESH_SECONDS)
    base = {"iss": settings.FAMILY_AUTH_ISSUER, "iat": int(now.timestamp())}
    access = _encode({
        **base, **identity_claims(identity),
        "aud": list(settings.FAMILY_AUTH_AUDIENCE),
        "token_type": "access",
        "jti": uuid.uuid4().hex,
        "exp": int(access_exp.timestamp()),
    })
    jti = uuid.uuid4().hex
    RefreshToken.objects.create(jti=jti, identity=identity, expires_at=refresh_exp)
    refresh = _encode({
        **base,
        # Only PROMOP consumes refresh tokens.
        "aud": settings.FAMILY_AUTH_ISSUER,
        "sub": identity.uid,
        "user_id": identity.pk,
        "token_type": "refresh",
        "jti": jti,
        "exp": int(refresh_exp.timestamp()),
    })
    return {"access": access, "refresh": refresh, "jti": jti}


def _decode(token: str, *, audience, token_type: str) -> dict:
    if not is_configured():
        raise FamilyAuthError("Family auth is not configured.")
    try:
        claims = jwt.decode(
            token, _public_key_pem(), algorithms=[ALGORITHM], audience=audience,
            issuer=settings.FAMILY_AUTH_ISSUER,
            options={"require": ["exp", "iss", "aud", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise FamilyAuthError("Token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise FamilyAuthError("Invalid token.") from exc
    if claims.get("token_type") != token_type:
        raise FamilyAuthError("Wrong token type.")
    return claims


def decode_access(token: str) -> dict:
    return _decode(token, audience=list(settings.FAMILY_AUTH_AUDIENCE), token_type="access")


def is_family_token(token: str) -> bool:
    """Whether a bearer token claims to be one of ours (unverified)."""
    if not is_configured():
        return False
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss") == settings.FAMILY_AUTH_ISSUER
    except jwt.PyJWTError:
        return False


def rotate(refresh: str) -> tuple[object, dict]:
    """Spend a refresh token for a new pair. A revoked one presented again
    means it leaked: every live refresh token of that identity is revoked."""
    claims = _decode(refresh, audience=settings.FAMILY_AUTH_ISSUER, token_type="refresh")
    reused = None
    with transaction.atomic():
        row = (RefreshToken.objects.select_for_update().select_related("identity")
               .filter(jti=claims["jti"], identity_id=claims.get("user_id")).first())
        if row is None:
            raise FamilyAuthError("Invalid token.")
        identity = row.identity
        if row.revoked_at is not None:
            reused = identity
    if reused is not None:
        # Outside the transaction: raising inside it would roll the revocation back.
        revoke_all(reused)
        raise FamilyAuthError("Token has been revoked.")
    with transaction.atomic():
        row = RefreshToken.objects.select_for_update().get(pk=row.pk)
        if row.revoked_at is not None:
            raise FamilyAuthError("Token has been revoked.")
        if not row.is_active or not identity.is_active or identity.uid != claims["sub"]:
            raise FamilyAuthError("Invalid token.")
        pair = issue_pair(identity)
        row.revoked_at = timezone.now()
        row.replaced_by = pair["jti"]
        row.save(update_fields=["revoked_at", "replaced_by"])
    return identity, pair


def revoke(refresh: str) -> None:
    """Log out one session. An invalid token is ignored: logging out never fails."""
    try:
        claims = _decode(refresh, audience=settings.FAMILY_AUTH_ISSUER, token_type="refresh")
    except FamilyAuthError:
        return
    RefreshToken.objects.filter(jti=claims["jti"], revoked_at__isnull=True).update(revoked_at=timezone.now())


def revoke_all(identity) -> None:
    RefreshToken.objects.filter(identity=identity, revoked_at__isnull=True).update(revoked_at=timezone.now())
