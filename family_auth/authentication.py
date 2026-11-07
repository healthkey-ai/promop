"""PROMOP accepting the family tokens it issued.

The token names a PROMOP identity directly (user_id + uid), so there is no
partner mapping and no email linking: a phr user signed in against PROMOP is
the same Identity as on PROMOP's own pages. Tokens from any other issuer are
left to the next authentication class.

``request.auth`` is a TokenClaims, the type PROMOP's permissions and audit
log already treat as a signed-in user's token (role-based access: a patient
reads and patches their own data, staff and org admins more), not an OAuth
token, which would need SMART scopes.
"""
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from patient_portal.api.providers.base import TokenClaims
from patient_portal.models import Identity

from . import tokens


class FamilyTokenAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if len(parts) != 2 or parts[0].lower() != self.keyword:
            return None
        token = parts[1].decode("latin-1")
        if not tokens.is_family_token(token):
            return None
        try:
            claims = tokens.decode_access(token)
        except tokens.FamilyAuthError as exc:
            raise AuthenticationFailed(str(exc)) from exc
        identity = Identity.objects.filter(pk=claims.get("user_id"), uid=claims["sub"], is_active=True).first()
        if identity is None:
            raise AuthenticationFailed("Invalid token.")
        return identity, TokenClaims(
            issuer=claims["iss"], sub=claims["sub"], email=claims.get("email", ""),
            name=identity.name or None, raw=claims, email_verified=bool(claims.get("email_verified")),
        )

    def authenticate_header(self, request):
        return 'Bearer realm="api"'
