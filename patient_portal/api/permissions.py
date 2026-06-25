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

    Role model for non-OAuth2 auth paths:

      service-token         → full access (trusted backend service)
      is_superuser/is_staff → full access
      other authenticated   → safe methods + PATCH only
                              (read + self-edit; POST/DELETE denied)

    NOTE: patient population scoping (multi-tenant isolation per HealthTree
    integration) is tracked separately under HKI-SEC-04 and HKI-AUTH-04.
    """

    def has_permission(self, request, view):
        token = request.auth

        # Service-to-service: trusted backend — full access.
        if token == "service-token":
            return True  # hmac already validated in ServiceTokenAuthentication.authenticate()

        # Partner-auth (Firebase, SAML) and session-auth: role-based enforcement.
        if token is None or isinstance(token, TokenClaims):
            if not (request.user and request.user.is_authenticated):
                return False
            # Staff and superusers retain full access.
            if request.user.is_superuser or getattr(request.user, 'is_staff', False):
                return True
            # Regular authenticated users (patients): read + PATCH own data only.
            # POST (sync, bulk upload) and DELETE (visits, measurements, bulk) are denied.
            return request.method in _SAFE_METHODS or request.method == 'PATCH'

        # OAuth2 token: enforce SMART on FHIR scopes.
        if not hasattr(token, 'scope') or timezone.now() >= token.expires:
            return False

        token_scopes = frozenset(token.scope.split())

        if request.method in _SAFE_METHODS:
            return bool(token_scopes & _READ_SCOPES)
        return bool(token_scopes & _WRITE_SCOPES)


class LabSyncPermission(ScopedTokenPermission):
    """
    Permission for the hk-labs → ctomop lab sync endpoint.

    Identical to ScopedTokenPermission except that an authenticated end
    user (Firebase/partner or session auth) is allowed to write, not just
    read/PATCH. Committing labs is a legitimate patient self-service write:
    SyncView resolves the target person from the authenticated identity and
    enforces can_access_patient() for on-behalf-of writes, and binds the
    actor to request.user for non-service callers — so a user can only write
    records they actually control.

    Service tokens and OAuth2 SMART scopes are handled exactly as in the
    base class.
    """

    def has_permission(self, request, view):
        token = request.auth
        # End-user auth (partner/session): allow authenticated users to write;
        # SyncView enforces per-person authorization. Service-token and OAuth2
        # clients fall through to the base role model.
        if token is None or isinstance(token, TokenClaims):
            return bool(request.user and request.user.is_authenticated)
        return super().has_permission(request, view)
