"""Request-local calendar timezone supplied by the browser UI.

The timezone is a display/input hint, not an authorization boundary. Invalid or
missing hints retain the deployment's configured timezone.
"""
from __future__ import annotations

from contextlib import contextmanager
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


def request_timezone(request):
    """Return a valid IANA timezone from the request, or the configured default."""
    name = request.headers.get('X-Client-Timezone', '').strip()
    if not name:
        return timezone.get_default_timezone()
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return timezone.get_default_timezone()


@contextmanager
def use_request_timezone(request):
    """Use the browser's calendar day while validating this interactive write."""
    with timezone.override(request_timezone(request)):
        yield
