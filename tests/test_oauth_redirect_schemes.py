"""django-oauth-toolkit must enforce the redirect schemes we configure."""

import pytest
from django.core.exceptions import ValidationError
from oauth2_provider.models import get_application_model

Application = get_application_model()


@pytest.mark.parametrize('schemes,uri,accepted', [
    (['https'], 'https://client.example.invalid/callback', True),
    (['https'], 'http://client.example.invalid/callback', False),
    (['https'], 'http://localhost:3000/callback', False),
    (['https', 'http'], 'http://localhost:3000/callback', True),
    (['https', 'http'], 'ftp://client.example.invalid/callback', False),
])
def test_redirect_uri_follows_allowed_schemes(settings, schemes, uri, accepted):
    settings.OAUTH2_PROVIDER = {
        **settings.OAUTH2_PROVIDER, 'ALLOWED_REDIRECT_URI_SCHEMES': schemes,
    }
    application = Application(
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=uri,
    )
    if accepted:
        application.clean()
    else:
        with pytest.raises(ValidationError):
            application.clean()
