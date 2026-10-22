"""Keep retired SPA OAuth credentials out of the session-only browser app."""

from django.conf import settings
from oauth2_provider.oauth2_validators import OAuth2Validator


class SessionOnlyBrowserValidator(OAuth2Validator):
    @staticmethod
    def _is_retired(client):
        return bool(client and client.client_id in settings.OAUTH2_RETIRED_BROWSER_CLIENT_IDS)

    def validate_grant_type(self, client_id, grant_type, client, request, *args, **kwargs):
        if self._is_retired(client):
            return False
        return super().validate_grant_type(client_id, grant_type, client, request, *args, **kwargs)

    def validate_response_type(self, client_id, response_type, client, request, *args, **kwargs):
        if self._is_retired(client):
            return False
        return super().validate_response_type(client_id, response_type, client, request, *args, **kwargs)

    def validate_bearer_token(self, token, scopes, request):
        if not super().validate_bearer_token(token, scopes, request):
            return False
        if self._is_retired(request.client):
            request.user = None
            request.access_token = None
            request.oauth2_error = {"error": "invalid_token"}
            return False
        return True
