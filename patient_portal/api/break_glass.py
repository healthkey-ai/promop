"""
Break-glass emergency access — PHR-S FM TI.2.3#04.

A clinical/professional user (staff, superuser, or a holder of a professional
GroupAccess grant — not a plain patient) may assert an emergency need to review a
specific patient's audit-log entries, providing a reason. This creates a
time-boxed BreakGlassGrant (the reason + patient are captured) and, during the
window, the AuditEventViewSet reveals that patient's audit entries to them. The
break-glass request is itself audited by AuditLogMiddleware (event `break_glass`).
"""
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import GroupAccess, PatientRecord
from patient_portal.models import BreakGlassGrant
from .permissions import is_service_token


def _may_break_glass(request, person_id: int) -> bool:
    """Require a patient/org nexus unless an explicit emergency policy applies."""
    user = request.user
    if is_service_token(request):
        return bool(getattr(settings, 'BREAK_GLASS_ALLOW_SERVICE', False))
    if getattr(user, 'is_superuser', False):
        return True
    from django.db.models import Q
    now = timezone.now()
    org_ids = GroupAccess.objects.filter(identity=user).exclude(
        role='patient',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).exclude(org_id__isnull=True).values_list('org_id', flat=True)
    return PatientRecord.objects.filter(
        person_id=person_id, organization_id__in=org_ids,
    ).exists()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def break_glass(request):
    """POST /api/v1/break-glass/ {person_id, reason} — authorize emergency audit access."""
    reason = (request.data.get('reason') or '').strip()
    if not reason:
        return Response({'error': 'A reason is required for break-glass access.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        person_id = int(request.data.get('person_id'))
    except (TypeError, ValueError):
        return Response({'error': 'A valid person_id is required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    if not _may_break_glass(request, person_id):
        return Response(
            {'error': 'You are not permitted to invoke break-glass access.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    expires_at = timezone.now() + timezone.timedelta(seconds=settings.BREAK_GLASS_TTL_SECONDS)
    grant = BreakGlassGrant.objects.create(
        identity=request.user, person_id=person_id, reason=reason, expires_at=expires_at,
    )
    return Response(
        {
            'id': grant.id,
            'person_id': grant.person_id,
            'reason': grant.reason,
            'expires_at': grant.expires_at.isoformat(),
        },
        status=status.HTTP_201_CREATED,
    )
