"""DRF exception handler that reports server errors to Sentry."""

from __future__ import annotations

from typing import Any

import sentry_sdk
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def sentry_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Answer through DRF, reporting anything DRF turns into a 5xx."""
    response: Response | None = drf_exception_handler(exc, context)

    # None means DRF did not recognise it, so Django reports it instead.
    if response is not None and response.status_code >= 500:
        sentry_sdk.capture_exception(exc)

    return response
