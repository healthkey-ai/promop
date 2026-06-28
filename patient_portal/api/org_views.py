"""
Org management views.

Endpoints are registered in urls.py as:
  /api/orgs/                          - list / create
  /api/orgs/{slug}/                   - detail / update / delete
  /api/orgs/{slug}/invite/            - send invitation
  /api/orgs/{slug}/invitations/       - list invitations
  /api/orgs/{slug}/invitations/{id}/  - cancel invitation
  /api/orgs/{slug}/trusts/            - list / add trusts
  /api/orgs/{slug}/trusts/{id}/       - remove trust
  /api/orgs/{slug}/access/            - list GroupAccess grants
  /api/orgs/{slug}/access/{id}/       - revoke access grant
  /api/orgs/confirm-invitation/       - public: confirm by token
"""
import secrets
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.models import Organization, OrgTrust, OrgInvitation, GroupAccess
from patient_portal.models import Identity
from .permissions import IsStaffPermission, IsStaffOrOrgAdmin
from .serializers import (
    OrganizationSerializer, OrgTrustSerializer,
    OrgInvitationSerializer, GroupAccessSerializer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org(slug: str) -> Organization:
    return get_object_or_404(Organization, slug=slug)


def _visible_orgs(user):
    """Return orgs the user may administer (staff: all; org_admin: own)."""
    from omop_core.services.access import get_visible_orgs
    if getattr(user, 'is_staff', False):
        return Organization.objects.all()
    from django.db.models import Q
    now = timezone.now()
    admin_org_ids = GroupAccess.objects.filter(
        identity=user, role='org_admin',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).values_list('org_id', flat=True)
    return Organization.objects.filter(id__in=admin_org_ids)


# ---------------------------------------------------------------------------
# Org list / create
# ---------------------------------------------------------------------------

class OrgListCreateView(APIView):
    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsStaffPermission()]
        return [IsStaffOrOrgAdmin()]

    def get(self, request):
        orgs = _visible_orgs(request.user)
        return Response(OrganizationSerializer(orgs, many=True).data)

    def post(self, request):
        ser = OrganizationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        org = ser.save(created_by=request.user)
        return Response(OrganizationSerializer(org).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Org detail / update / delete
# ---------------------------------------------------------------------------

class OrgDetailView(APIView):
    def get_permissions(self):
        if self.request.method == 'DELETE':
            return [IsStaffPermission()]
        return [IsStaffOrOrgAdmin()]

    def get(self, request, slug):
        org = _get_org(slug)
        return Response(OrganizationSerializer(org).data)

    def patch(self, request, slug):
        org = _get_org(slug)
        ser = OrganizationSerializer(org, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        # Non-staff org_admins cannot toggle is_active
        if not getattr(request.user, 'is_staff', False):
            ser.validated_data.pop('is_active', None)
        ser.save()
        return Response(ser.data)

    def delete(self, request, slug):
        org = _get_org(slug)
        org.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

class OrgInviteView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def post(self, request, slug):
        org = _get_org(slug)
        email = request.data.get('email', '').strip().lower()
        role = request.data.get('role', 'doctor')

        if not email:
            return Response({'error': 'email is required'}, status=status.HTTP_400_BAD_REQUEST)
        if role not in dict(OrgInvitation.ROLE):
            return Response({'error': f'Invalid role: {role}'}, status=status.HTTP_400_BAD_REQUEST)

        # Cancel any existing pending invitation for the same email+org
        OrgInvitation.objects.filter(
            org=org, email=email,
            confirmed_at__isnull=True, cancelled_at__isnull=True,
        ).update(cancelled_at=timezone.now())

        token = secrets.token_hex(32)  # 64 hex chars
        expires_at = timezone.now() + timezone.timedelta(days=7)
        invitation = OrgInvitation.objects.create(
            org=org,
            email=email,
            role=role,
            token=token,
            invited_by=request.user,
            expires_at=expires_at,
        )
        return Response(OrgInvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)


class OrgInvitationListView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def get(self, request, slug):
        org = _get_org(slug)
        invitations = org.invitations.order_by('-created_at')
        return Response(OrgInvitationSerializer(invitations, many=True).data)


class OrgInvitationDetailView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def delete(self, request, slug, invitation_id):
        org = _get_org(slug)
        invitation = get_object_or_404(OrgInvitation, id=invitation_id, org=org)
        if invitation.status != OrgInvitation.STATUS_PENDING:
            return Response(
                {'error': 'Only pending invitations can be cancelled.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        invitation.cancelled_at = timezone.now()
        invitation.save(update_fields=['cancelled_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([AllowAny])
def confirm_invitation(request):
    """Public endpoint — confirms an invitation by token and creates a GroupAccess."""
    token = request.data.get('token', '').strip()
    if not token:
        return Response({'error': 'token is required'}, status=status.HTTP_400_BAD_REQUEST)

    invitation = get_object_or_404(OrgInvitation, token=token)

    if invitation.status == OrgInvitation.STATUS_CONFIRMED:
        return Response({'error': 'Invitation already confirmed.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == OrgInvitation.STATUS_CANCELLED:
        return Response({'error': 'Invitation has been cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == OrgInvitation.STATUS_EXPIRED:
        return Response({'error': 'Invitation has expired.'}, status=status.HTTP_400_BAD_REQUEST)

    # Look up or create identity for invited email
    try:
        identity = Identity.objects.get(email=invitation.email)
    except Identity.DoesNotExist:
        return Response(
            {'error': 'No account found for this email. Please sign up first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Create GroupAccess (idempotent: update_or_create so re-confirm works)
    GroupAccess.objects.update_or_create(
        identity=identity,
        org=invitation.org,
        defaults={'role': invitation.role, 'granted_by': None},
    )

    invitation.confirmed_at = timezone.now()
    invitation.save(update_fields=['confirmed_at'])

    return Response({'detail': f'Invitation confirmed. Access granted to {invitation.org.name}.'})


# ---------------------------------------------------------------------------
# Trusts
# ---------------------------------------------------------------------------

class OrgTrustListCreateView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def get(self, request, slug):
        org = _get_org(slug)
        trusts = org.trusts_granted.select_related('trusted_org').order_by('id')
        return Response(OrgTrustSerializer(trusts, many=True).data)

    def post(self, request, slug):
        org = _get_org(slug)
        ser = OrgTrustSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        trust = ser.save(granting_org=org, granted_by=request.user)
        return Response(OrgTrustSerializer(trust).data, status=status.HTTP_201_CREATED)


class OrgTrustDetailView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def delete(self, request, slug, trust_id):
        org = _get_org(slug)
        trust = get_object_or_404(OrgTrust, id=trust_id, granting_org=org)
        trust.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Access grants
# ---------------------------------------------------------------------------

class OrgAccessListView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def get(self, request, slug):
        org = _get_org(slug)
        grants = GroupAccess.objects.filter(org=org).select_related('identity').order_by('id')
        return Response(GroupAccessSerializer(grants, many=True).data)


class OrgAccessDetailView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def delete(self, request, slug, access_id):
        org = _get_org(slug)
        grant = get_object_or_404(GroupAccess, id=access_id, org=org)
        grant.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
