from django.conf import settings
from django.test import override_settings


def test_provenance_headers_are_allowed_for_cors_preflight():
    headers = {header.lower() for header in settings.CORS_ALLOW_HEADERS}

    assert 'x-provenance-source' in headers
    assert 'x-provenance-user-id' in headers
    assert 'authorization' in headers
    assert 'content-type' in headers


def test_conditional_write_headers_are_allowed_for_cors_preflight():
    """Every browser client of this API is cross-origin, and EXACT makes
    every preference write conditional (#1312).

    `default_headers` from django-cors-headers carries neither of these, so
    the preflight was refused with "Request header field if-match is not
    allowed by Access-Control-Allow-Headers" and the PATCH never left the
    browser. Silently: the client sees a network error, not a 4xx, so nothing
    in this service's logs recorded it.
    """
    headers = {header.lower() for header in settings.CORS_ALLOW_HEADERS}

    assert 'if-match' in headers
    assert 'if-none-match' in headers


def test_the_conditional_write_tag_is_readable_cross_origin():
    """A response header a cross-origin caller cannot read may as well not be
    sent. EXACT prefers the tag the body carries and falls back to this one,
    so its absence degraded rather than broke — but the fallback was never
    reachable from a browser."""
    exposed = {header.lower() for header in settings.CORS_EXPOSE_HEADERS}

    assert 'etag' in exposed


CONDITIONAL_WRITE_URL = '/api/v1/trial-search-preferences/upsert/'


@override_settings(CORS_ALLOWED_ORIGINS=['http://localhost:5173'])
def test_a_conditional_preflight_is_answered_by_the_middleware(client):
    """The settings assertions above would both pass with the middleware
    removed, or ordered behind something that short-circuits. This asks the
    stack.

    What it does NOT do is exercise the endpoint. `CorsMiddleware` answers a
    preflight with a bare response BEFORE url resolution and without reading
    `Access-Control-Request-Headers` at all, so the path and that header are
    decorative here — the same assertions pass against a route that does not
    exist. The test below is the one that keeps the route honest.
    """
    response = client.options(
        CONDITIONAL_WRITE_URL,
        HTTP_ORIGIN='http://localhost:5173',
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='PATCH',
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type,if-match',
    )

    allowed = {
        header.strip().lower()
        for header in response.headers.get('access-control-allow-headers', '').split(',')
    }
    assert response.headers.get('access-control-allow-origin') == 'http://localhost:5173'
    assert 'if-match' in allowed


@override_settings(CORS_ALLOWED_ORIGINS=['http://localhost:5173'])
def test_the_route_the_preflight_is_about_exists(client):
    """No `Access-Control-Request-Method`, so the preflight short-circuit does
    not fire and the request reaches the router.

    Without this, renaming or deleting `upsert` leaves the test above green
    while it claims to be about that endpoint.
    """
    response = client.options(CONDITIONAL_WRITE_URL, HTTP_ORIGIN='http://localhost:5173')

    assert response.status_code != 404
