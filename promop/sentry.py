"""Sentry error monitoring, disabled unless SENTRY_DSN is set."""

from __future__ import annotations

import os
import re
from typing import Any

_ID_SEGMENT = re.compile(
    r'^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$'
)

_EMAIL = re.compile(r'[^\s<>"\']+@[^\s<>"\']+\.[A-Za-z]{2,}')

_EXTRA_SECRET_KEYS: tuple[str, ...] = (
    'SECRET_KEY',
    'DATABASE_URL',
    'STAGING_DATABASE_URL',
    'SOURCE_DATABASE_URL',
    'CELERY_BROKER_URL',
    'CELERY_RESULT_BACKEND',
    'CACHE_URL',
    'AUDIT_HMAC_KEY',
    'EXPORT_SIGNING_KEY',
    'SERVICE_AUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'MAILGUN_API_KEY',
    'ADMIN_PASSWORD',
)


def _env_flag(name: str, default: str = 'false') -> bool:
    """Read a boolean environment variable."""
    return os.environ.get(name, default).lower() in ('1', 'true', 'yes')


def _env_rate(name: str, default: str) -> float:
    """Read a sample rate, clamped to [0, 1], falling back on junk."""
    try:
        rate = float(os.environ.get(name, default))
    except ValueError:
        rate = float(default)
    return min(1.0, max(0.0, rate))


def redact_path(url: str) -> str:
    """Replace the id segments of a URL path with a placeholder."""
    prefix, separator, path = url.partition('://')
    if separator:
        host, slash, path = path.partition('/')
        prefix = f'{prefix}://{host}{slash}'
    else:
        prefix, path = '', url
    segments: list[str] = [':id' if _ID_SEGMENT.match(seg) else seg for seg in path.split('/')]
    return prefix + '/'.join(segments)


def _redact_emails(value: Any) -> Any:
    """Replace email addresses anywhere in a log message or its parameters."""
    if isinstance(value, str):
        return _EMAIL.sub('[email]', value)
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_emails(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_emails(item) for key, item in value.items()}
    return value


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """Drop the parts of an event that can carry patient data."""
    request: dict[str, Any] | None = event.get('request')
    if isinstance(request, dict):
        request.pop('query_string', None)
        request.pop('data', None)
        url: str | None = request.get('url')
        if isinstance(url, str):
            request['url'] = redact_path(url.split('?', 1)[0])

    # The key based scrubber cannot see an address formatted into the message.
    logentry: dict[str, Any] | None = event.get('logentry')
    if isinstance(logentry, dict):
        event['logentry'] = _redact_emails(logentry)

    argv: list[str] | None = event.get('extra', {}).get('sys.argv')
    if isinstance(argv, list):
        # A management command runs with a connection string in argv.
        event['extra']['sys.argv'] = ['[Filtered]' if '://' in arg else arg for arg in argv]
    return event


def build_options(dsn: str, debug: bool) -> dict[str, Any]:
    """Build the options passed to sentry_sdk.init."""
    return {
        'dsn': dsn,
        'environment': os.environ.get('SENTRY_ENVIRONMENT', 'development' if debug else 'production'),
        'release': os.environ.get('SENTRY_RELEASE') or os.environ.get('RENDER_GIT_COMMIT') or None,
        'traces_sample_rate': _env_rate('SENTRY_TRACES_SAMPLE_RATE', '0.0'),
        'profile_session_sample_rate': _env_rate('SENTRY_PROFILE_SESSION_SAMPLE_RATE', '0.0'),
        'send_default_pii': _env_flag('SENTRY_SEND_DEFAULT_PII'),
        'max_request_body_size': 'never',
        # A frame that parses a bundle holds the whole bundle in its locals.
        'include_local_variables': False,
        'before_send': _scrub_event,
        # Transactions skip before_send entirely.
        'before_send_transaction': _scrub_event,
    }


def init_sentry(debug: bool) -> bool:
    """Initialise the SDK and report whether it was enabled."""
    dsn: str = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    denylist: list[str] = DEFAULT_DENYLIST + [key.lower() for key in _EXTRA_SECRET_KEYS]
    sentry_sdk.init(
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            LoggingIntegration(level=None, event_level='ERROR'),
        ],
        event_scrubber=EventScrubber(denylist=denylist, recursive=True),
        **build_options(dsn, debug),
    )
    return True
