from django.utils import timezone
from rest_framework.permissions import BasePermission

from .providers.base import TokenClaims


def get_request_org(request):
    """
    Return the Organization associated with the current OAuth2 token, or None.

    Returns None (no scoping) for:
      - superusers (can see all orgs)
      - session-authenticated requests (backward compat)
      - partner-auth requests (Firebase, SAML — no org scoping)
      - service clients not linked to any organization
    """
    if request.user and request.user.is_superuser:
        return None
    token = getattr(request, 'auth', None)
    if token is None or isinstance(token, TokenClaims):
        return None
    try:
        return token.application.org_profile.organization
    except AttributeError:
        return None

_SAFE_METHODS = frozenset(('GET', 'HEAD', 'OPTIONS'))
_READ_SCOPES = frozenset(('patient/*.read', 'user/*.read'))
_WRITE_SCOPES = frozenset(('patient/*.write', 'user/*.write'))


class ScopedTokenPermission(BasePermission):
    """
    Enforces SMART on FHIR read/write scopes based on HTTP method.

    Safe methods   (GET, HEAD, OPTIONS) → patient/*.read  or user/*.read
    Unsafe methods (POST, PUT, PATCH, DELETE) → patient/*.write or user/*.write

    Partner-auth (Firebase, SAML) and session-authenticated users bypass
    scope checks — they are already authenticated via token verification
    or session cookie.

    NOTE: patient population scoping (multi-tenant isolation per HealthTree
    integration) is tracked separately under HKI-SEC-04 and HKI-AUTH-04.
    """

    def has_permission(self, request, view):
        token = request.auth

        # Partner auth, session auth, or service-token: no OAuth2 scopes to check.
        # TODO(security): these paths get full read+write with no scope enforcement.
        if token is None or token == "service-token" or isinstance(token, TokenClaims):
            return bool(request.user and request.user.is_authenticated)

        if not hasattr(token, 'scope') or timezone.now() >= token.expires:
            return False

        token_scopes = frozenset(token.scope.split())

        if request.method in _SAFE_METHODS:
            return bool(token_scopes & _READ_SCOPES)
        return bool(token_scopes & _WRITE_SCOPES)
