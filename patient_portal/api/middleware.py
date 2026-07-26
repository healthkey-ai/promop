import datetime
import json
import logging
import time
from email.utils import parsedate_to_datetime

logger = logging.getLogger('audit')

_SUNSET_DATE = 'Tue, 01 Sep 2026 00:00:00 GMT'
_SUNSET_DT = parsedate_to_datetime(_SUNSET_DATE)
_SUCCESSOR = '</api/v1/>; rel="successor-version"'


class DeprecationWarningMiddleware:
    """
    Adds HTTP deprecation headers to responses on legacy /api/ paths (non-versioned).
    Versioned /api/v1/ paths are unaffected.

    Headers added to legacy responses:
      Deprecation: true
      Sunset: <date>
      Link: </api/v1/>; rel="successor-version"
    """

    def __init__(self, get_response):
        self.get_response = get_response
        if datetime.datetime.now(datetime.timezone.utc) > _SUNSET_DT:
            logger.warning(
                "DeprecationWarningMiddleware: Sunset date %s has passed — "
                "remove legacy /api/ URL aliases from ctomop/urls.py (the Django project package).",
                _SUNSET_DATE,
            )

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if path.startswith('/api/') and not path.startswith('/api/v1/'):
            response['Deprecation'] = 'true'
            response['Sunset'] = _SUNSET_DATE
            response['Link'] = _SUCCESSOR
        return response

# Only these methods are audited. HEAD/OPTIONS/TRACE (CORS preflight, health probes)
# are intentionally excluded as noise; GET is audited as a record_view (TI.2).
_AUDITED_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})

# Paths whose prefixes carry auditable activity: the API, OAuth, and the Django
# admin (privileged/security-config actions — TI.2.1). Static assets, the SPA
# shell, and health checks are skipped.
_AUDITED_PREFIXES = ('/api/', '/o/', '/admin/')


def _classify_event_type(request):
    """Map a request to a TI.2 audit event_type.

    Auth and consent are recognised by path first (a login is a POST but an
    auth event, not a create); everything else falls back to CRUD-by-method,
    with reads recorded as record_view.
    """
    from patient_portal.models import AuditEvent

    path = request.path
    method = request.method

    # Access to the audit trail itself is auditable (TI.2.2#04).
    if '/audit-events' in path:
        return AuditEvent.EVENT_AUDIT_REVIEW
    # Django-admin activity (privileged / security-config actions — TI.2.1).
    if path.startswith('/admin/'):
        return AuditEvent.EVENT_ADMIN

    if (
        '/auth/login' in path
        or '/auth/logout' in path
        or '/patients/signup' in path
        or '/patient-invitations/accept' in path
        or path.startswith('/o/token')
        or path.startswith('/o/authorize')
        or path.startswith('/o/revoke')
        or path.startswith('/o/introspect')
    ):
        return AuditEvent.EVENT_AUTH
    if 'consent' in path:
        return AuditEvent.EVENT_CONSENT
    if method == 'GET':
        return AuditEvent.EVENT_VIEW
    if method == 'POST':
        return AuditEvent.EVENT_CREATE
    if method in ('PUT', 'PATCH'):
        return AuditEvent.EVENT_UPDATE
    if method == 'DELETE':
        return AuditEvent.EVENT_DELETE
    return AuditEvent.EVENT_OTHER


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


def _should_audit(request):
    """True when this request is on an auditable path (API / OAuth / admin) with
    an audited method. Access to the audit trail itself IS audited (TI.2.2#04)."""
    if request.method not in _AUDITED_METHODS:
        return False
    return request.path.startswith(_AUDITED_PREFIXES)


class AuditLogMiddleware:
    """
    Persists an audit-trail entry (HL7 PHR-S FM TI.2) for every audited API
    request — reads (record_view) as well as writes — dual-writing a structured
    JSON line to stdout (SIEM-friendly) and an AuditEvent row (reviewable via
    /api/v1/audit-events/).

    Never raises — stdout and DB writes are independently guarded so a logging
    or database failure can never block the API response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)

        if not _should_audit(request):
            return response

        entry = None
        try:
            user = getattr(request, 'user', None)
            authed = bool(user and user.is_authenticated)
            entry = {
                'event': _classify_event_type(request),
                'method': request.method,
                'path': request.path,
                'status_code': response.status_code,
                'client_id': _get_client_id(request),
                'user_id': str(user.pk) if authed else None,
                'user_email': (getattr(user, 'email', '') or None) if authed else None,
                'resource_id': _get_resource_id(request),
                'ip_address': (
                    request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                    or request.META.get('REMOTE_ADDR')
                ),
                'duration_ms': round((time.monotonic() - start) * 1000),
            }
        except Exception:
            logger.warning("AuditLogMiddleware failed to build audit entry", exc_info=True)
            return response

        # stdout (SIEM) and DB (review API) are written independently so one
        # failing never suppresses the other, and neither blocks the response.
        try:
            logger.info(json.dumps(entry))
        except Exception:
            logger.warning("AuditLogMiddleware stdout write failed", exc_info=True)

        try:
            self._persist(entry)
        except Exception:
            logger.warning("AuditLogMiddleware DB write failed", exc_info=True)

        return response

    @staticmethod
    def _persist(entry):
        from patient_portal.models import AuditEvent

        AuditEvent.objects.create(
            event_type=entry['event'],
            method=entry['method'],
            path=entry['path'][:512],
            status_code=entry['status_code'],
            user_id=entry['user_id'],
            user_email=(entry['user_email'] or '')[:254] or None,
            client_id=entry['client_id'],
            resource_id=entry['resource_id'],
            ip_address=(entry['ip_address'] or '')[:64] or None,
            duration_ms=entry['duration_ms'],
        )
