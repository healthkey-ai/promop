import json
import logging
import time

logger = logging.getLogger('audit')

_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})


def _get_client_id(request):
    """Extract OAuth2 client_id from the token on the request, if present."""
    token = getattr(request, 'auth', None)
    if token is None:
        return None
    # django-oauth-toolkit AccessToken has an `application` FK
    app = getattr(token, 'application', None)
    if app:
        return app.client_id
    return str(token)


def _get_resource_id(request):
    """Best-effort extraction of the primary resource ID from the URL kwargs."""
    resolver_match = getattr(request, 'resolver_match', None)
    if resolver_match:
        kwargs = resolver_match.kwargs
        for key in ('pk', 'id', 'person_id', 'slug'):
            if key in kwargs:
                return str(kwargs[key])
    return None


class AuditLogMiddleware:
    """
    Emits a structured JSON audit log line for every mutating API request
    (POST, PUT, PATCH, DELETE). Reads are not logged.

    Never raises — failures are swallowed so the API response is never blocked.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)

        if request.method in _SAFE_METHODS:
            return response

        try:
            user = request.user
            entry = {
                'event': 'api_write',
                'method': request.method,
                'path': request.path,
                'status_code': response.status_code,
                'client_id': _get_client_id(request),
                'user_id': str(user.pk) if user and user.is_authenticated else None,
                'resource_id': _get_resource_id(request),
                'ip_address': (
                    request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                    or request.META.get('REMOTE_ADDR')
                ),
                'duration_ms': round((time.monotonic() - start) * 1000),
            }
            logger.info(json.dumps(entry))
        except Exception:
            logger.warning("AuditLogMiddleware internal error", exc_info=True)

        return response
