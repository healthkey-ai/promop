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
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from patient_portal.models import AuditEvent
from .permissions import is_service_token
from .serializers import AuditEventSerializer


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
