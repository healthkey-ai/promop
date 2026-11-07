"""The family auth API: phr's endpoint contract, served for PROMOP accounts.

Mounted at /api/v1/family/auth/ (PROMOP's own /api/v1/auth/login/ is its
session login), so a phr installation sets its API base to
``https://<promop>/api/v1/family`` and calls the same paths it calls on
itself:

    POST login/       {email, password} -> {access, refresh}
    POST refresh/     {refresh}         -> {access, refresh}   (rotated)
    POST logout/      {refresh}         -> 205
    GET  me/                            -> the user (phr's AppUser)
    GET  jwks/                          -> RFC 7517 key set (RS256)
    POST introspect/  {token}           -> RFC 7662 {active, ...claims}

Sign-up is PROMOP's public org sign-up (POST /api/v1/orgs/<slug>/patient-signup/),
which verifies the email before the account can log in.
"""
import logging

from django.contrib.auth import authenticate
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from patient_portal.models import Identity

from . import tokens
from .authentication import INVALID_TOKEN, FamilyTokenAuthentication

logger = logging.getLogger(__name__)

INVALID = {"detail": "No active account found with the given credentials"}


class _Throttle(SimpleRateThrottle):
    """Per client address: login, refresh and introspect are unauthenticated."""
    scope = "family_auth"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def _unavailable():
    return Response({"detail": "Family auth is not configured."}, status=status.HTTP_404_NOT_FOUND)


def _authenticate(request, email: str, password: str):
    """The same credential check as PROMOP's own login: local accounts by
    email, lockout in the backend, no account-state enumeration."""
    user = authenticate(request, username=email, password=password)
    if user is None:
        identity = Identity.objects.filter(email__iexact=email).first()
        if identity is not None:
            user = authenticate(request, username=identity.uid, password=password)
    return user


def _blocked_unverified(identity) -> bool:
    """PROMOP's login rule: a self-signed-up account logs in only after
    verifying its email. Admin-created and invited accounts are unaffected."""
    from omop_core.models import GroupAccess

    return (identity.has_usable_password() and not identity.has_verified_email
            and GroupAccess.objects.filter(identity=identity, pending_email_verification=True).exists())


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([_Throttle])
def login(request):
    if not tokens.is_configured():
        return _unavailable()
    email = (request.data.get("email") or "").strip()
    password = request.data.get("password") or ""
    if not email or not password:
        return Response({"detail": "Email and password are required."}, status=status.HTTP_400_BAD_REQUEST)
    identity = _authenticate(request, email, password)
    if identity is None or not identity.is_active:
        return Response(INVALID, status=status.HTTP_401_UNAUTHORIZED)
    if _blocked_unverified(identity):
        from patient_portal.api.email_verification import send_verification_email

        send_verification_email(identity)
        return Response({
            "detail": "Please verify your email address before signing in. "
                      "We have sent a new verification link to your email.",
            "code": "email_not_verified",
        }, status=status.HTTP_403_FORBIDDEN)
    pair = tokens.issue_pair(identity)
    return Response({"access": pair["access"], "refresh": pair["refresh"]})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([_Throttle])
def refresh(request):
    if not tokens.is_configured():
        return _unavailable()
    try:
        _, pair = tokens.rotate(request.data.get("refresh") or "")
    except tokens.FamilyAuthError:
        # Expired, revoked or reused all read the same to the caller.
        return Response({"detail": INVALID_TOKEN, "code": "token_not_valid"}, status=status.HTTP_401_UNAUTHORIZED)
    return Response({"access": pair["access"], "refresh": pair["refresh"]})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def logout(request):
    if tokens.is_configured():
        tokens.revoke(request.data.get("refresh") or "")
    return Response(status=status.HTTP_205_RESET_CONTENT)


@api_view(["GET"])
@authentication_classes([FamilyTokenAuthentication])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(tokens.user_document(request.user))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def jwks(request):
    return Response(tokens.jwks_document())


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([_Throttle])
def introspect(request):
    """For siblings that want a revocation-aware check: an inactive or
    deleted identity reads as inactive even while its token is unexpired."""
    try:
        claims = tokens.decode_access(request.data.get("token") or "")
    except tokens.FamilyAuthError:
        return Response({"active": False})
    identity = Identity.objects.filter(pk=claims.get("user_id"), uid=claims["sub"], is_active=True).first()
    if identity is None:
        return Response({"active": False})
    current = tokens.identity_claims(identity)
    return Response({
        "active": True,
        **current,
        "exp": claims["exp"],
        "iss": claims["iss"],
        "aud": claims["aud"],
    })
