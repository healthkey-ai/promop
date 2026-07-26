"""
Audit-trail review API — HL7 PHR-S FM TI.2.3.

Read-only access to the AuditEvent rows written by AuditLogMiddleware.
Staff/superusers and trusted service tokens see every event; any other
authenticated user sees only their own. Filterable by event_type, method,
user_id (privileged callers only), and a timestamp window (after/before).
"""
import datetime as _datetime

from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from patient_portal.models import AuditEvent
from .permissions import is_service_token
from .serializers import AuditEventSerializer


# event_type → FHIR R4 AuditEvent.action code (C|R|U|D|E).
_FHIR_ACTION = {
    AuditEvent.EVENT_CREATE: 'C',
    AuditEvent.EVENT_VIEW: 'R',
    AuditEvent.EVENT_AUDIT_REVIEW: 'R',
    AuditEvent.EVENT_UPDATE: 'U',
    AuditEvent.EVENT_DELETE: 'D',
}


def _fhir_outcome(status_code):
    """FHIR R4 AuditEvent.outcome: 0 success, 4 minor failure, 8 serious failure."""
    if status_code is None:
        return '0'
    if status_code >= 500:
        return '8'
    if status_code >= 400:
        return '4'
    return '0'


def to_fhir_audit_event(row):
    """Project an AuditEvent row to a FHIR R4 `AuditEvent` resource (TI.2.2#01)."""
    resource = {
        'resourceType': 'AuditEvent',
        'id': str(row.id),
        'action': _FHIR_ACTION.get(row.event_type, 'E'),
        'recorded': row.timestamp.isoformat(),
        'outcome': _fhir_outcome(row.status_code),
        'type': {
            'system': 'http://terminology.hl7.org/CodeSystem/audit-event-type',
            'code': 'rest',
            'display': 'RESTful Operation',
        },
        'subtype': [{'system': 'http://hl7.org/fhir/restful-interaction', 'code': row.method}],
        'source': {'observer': {'display': 'promop'}},
        'agent': [{
            'requestor': True,
            'who': {'identifier': {'value': row.user_id or 'anonymous'}},
            'network': {'address': row.ip_address or '', 'type': '2'},  # 2 = IP address
        }],
        'entity': [{'what': {'display': row.path}}],
    }
    if row.user_email:
        resource['agent'][0]['who']['display'] = row.user_email
    return resource


class AuditEventPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000


def _parse_dt(value):
    """Parse an ISO 8601 datetime, or a bare date (as midnight). Returns aware dt or None."""
    dt = parse_datetime(value)
    if dt is None:
        d = parse_date(value)
        if d is None:
            return None
        dt = _datetime.datetime(d.year, d.month, d.day)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return dt


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audit-trail review (TI.2.3)."""
    serializer_class = AuditEventSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = AuditEventPagination

    def _is_privileged(self):
        request = self.request
        user = request.user
        return bool(
            is_service_token(request)
            or getattr(user, 'is_superuser', False)
            or getattr(user, 'is_staff', False)
        )

    def get_queryset(self):
        request = self.request
        qs = AuditEvent.objects.all()

        # Non-privileged callers are hard-scoped to their own events.
        if not self._is_privileged():
            qs = qs.filter(user_id=str(request.user.pk))

        params = request.query_params
        event_type = params.get('event_type')
        if event_type:
            qs = qs.filter(event_type=event_type)

        method = params.get('method')
        if method:
            qs = qs.filter(method=method.upper())

        # user_id filter is only honoured for privileged callers; a scoped user
        # is already restricted to their own rows above.
        user_id = params.get('user_id')
        if user_id and self._is_privileged():
            qs = qs.filter(user_id=user_id)

        after = params.get('after')
        if after:
            dt = _parse_dt(after)
            if dt is not None:
                qs = qs.filter(timestamp__gte=dt)

        before = params.get('before')
        if before:
            dt = _parse_dt(before)
            if dt is not None:
                qs = qs.filter(timestamp__lte=dt)

        return qs

    @action(detail=False, methods=['get'], url_path='fhir')
    def fhir(self, request):
        """Render the (scoped, filtered) audit trail as a FHIR R4 `AuditEvent`
        searchset Bundle — standards-based audit format (TI.2.2#01). Honors the
        same scoping/filters/pagination as the JSON list."""
        qs = self.get_queryset()
        total = qs.count()
        page = self.paginate_queryset(qs)
        rows = page if page is not None else list(qs)
        return Response({
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': total,
            'entry': [{'resource': to_fhir_audit_event(r)} for r in rows],
        })
