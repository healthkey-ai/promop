from django.conf import settings


def test_provenance_headers_are_allowed_for_cors_preflight():
    headers = {header.lower() for header in settings.CORS_ALLOW_HEADERS}

    assert 'x-provenance-source' in headers
    assert 'x-provenance-user-id' in headers
    assert 'authorization' in headers
    assert 'content-type' in headers
