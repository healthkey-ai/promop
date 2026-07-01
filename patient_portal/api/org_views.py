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
import logging
import secrets
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class InvitationEmailError(Exception):
    """Raised when an invitation email cannot be handed to the email backend."""


def _send_invitation_email(invitation) -> None:
    accept_url = f"{settings.APP_BASE_URL}/accept-invite?token={invitation.token}"
    subject = f"You've been invited to join {invitation.org.name} on PROMOP"
    body = (
        f"Hi,\n\n"
        f"You've been invited to join {invitation.org.name} as a {invitation.get_role_display()}.\n\n"
        f"Click the link below to accept your invitation:\n\n"
        f"  {accept_url}\n\n"
        f"This link expires in 7 days. If you don't have a PROMOP account yet, "
        f"please contact your administrator — account creation requires admin approval.\n\n"
        f"If you weren't expecting this invitation, you can ignore this email.\n\n"
        f"— The PROMOP team"
    )
    if settings.DEBUG:
        logger.info(
            "Invitation email preview\nTo: %s\nFrom: %s\nSubject: %s\n\n%s",
            invitation.email,
            settings.DEFAULT_FROM_EMAIL,
            subject,
            body,
        )
    try:
        sent_count = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [invitation.email])
    except Exception as exc:
        logger.exception(
            "Failed to send invitation email to %s via %s host=%s port=%s tls=%s from=%s: %s",
            invitation.email,
            settings.EMAIL_BACKEND,
            getattr(settings, 'EMAIL_HOST', ''),
            getattr(settings, 'EMAIL_PORT', ''),
            getattr(settings, 'EMAIL_USE_TLS', ''),
            settings.DEFAULT_FROM_EMAIL,
            exc,
        )
        raise InvitationEmailError from exc
    if sent_count != 1:
        logger.error(
            "Email backend reported %s invitation emails sent to %s via %s host=%s port=%s tls=%s from=%s",
            sent_count,
            invitation.email,
            settings.EMAIL_BACKEND,
            getattr(settings, 'EMAIL_HOST', ''),
            getattr(settings, 'EMAIL_PORT', ''),
            getattr(settings, 'EMAIL_USE_TLS', ''),
            settings.DEFAULT_FROM_EMAIL,
        )
        raise InvitationEmailError

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
    """Return orgs the user may ADMINISTER (staff: all; org_admin: own only).

    Intentionally narrower than get_visible_orgs() in services/access.py —
    trust-based orgs (domain/org-to-org) appear in patient-data visibility but
    NOT here, because the user holds no admin rights on those orgs.
    """
    if getattr(user, 'is_staff', False):
        return Organization.objects.all()
    now = timezone.now()
    admin_org_ids = GroupAccess.objects.filter(
        identity=user, role='org_admin',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    ).values_list('org_id', flat=True)
    return Organization.objects.filter(id__in=admin_org_ids)


def _find_identity_by_email(email):
    return (
        Identity.objects.filter(email__iexact=email, issuer='urn:local').first()
        or Identity.objects.filter(email__iexact=email).first()
    )


ROLE_RANK = {'org_admin': 3, 'doctor': 2, 'navigator': 1}


def _grant_org_access(identity, org, role, granted_by):
    existing = GroupAccess.objects.filter(identity=identity, org=org).first()
    if existing:
        if ROLE_RANK.get(existing.role, 0) < ROLE_RANK.get(role, 0):
            existing.role = role
            existing.granted_by = granted_by
            existing.save(update_fields=['role', 'granted_by'])
        return existing
    return GroupAccess.objects.create(
        identity=identity,
        org=org,
        role=role,
        granted_by=granted_by,
    )


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

        with transaction.atomic():
            invitation = OrgInvitation.objects.select_for_update().filter(
                org=org, email=email,
                confirmed_at__isnull=True, cancelled_at__isnull=True,
            ).first()

            token = secrets.token_hex(32)  # 64 hex chars
            expires_at = timezone.now() + timezone.timedelta(days=7)
            if invitation:
                invitation.role = role
                invitation.token = token
                invitation.invited_by = request.user
                invitation.expires_at = expires_at
                invitation.save(update_fields=['role', 'token', 'invited_by', 'expires_at'])
            else:
                invitation = OrgInvitation.objects.create(
                    org=org,
                    email=email,
                    role=role,
                    token=token,
                    invited_by=request.user,
                    expires_at=expires_at,
                )

            identity = _find_identity_by_email(email)
            if identity:
                _grant_org_access(identity, org, role, request.user)

        email_warning = None
        try:
            _send_invitation_email(invitation)
        except InvitationEmailError:
            email_warning = 'Invitation was created, but the email could not be sent.'

        # Defensive cleanup for stale duplicate pending rows from before the
        # partial unique constraint existed; normal paths keep one pending invite.
        OrgInvitation.objects.filter(
            org=org, email=email,
            confirmed_at__isnull=True, cancelled_at__isnull=True,
        ).exclude(id=invitation.id).update(cancelled_at=timezone.now())
        data = OrgInvitationSerializer(invitation).data
        data['access_granted'] = bool(identity)
        if email_warning:
            data['email_warning'] = email_warning
        return Response(data, status=status.HTTP_201_CREATED)


class OrgInvitationListView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def get(self, request, slug):
        org = _get_org(slug)
        invitations = org.invitations.select_related('org').order_by('-created_at')
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
    # Reject malformed tokens before touching the DB (tokens are 64 lowercase hex chars)
    if len(token) != 64 or not token.isalnum():
        return Response({'error': 'Invalid token format.'}, status=status.HTTP_400_BAD_REQUEST)

    invitation = get_object_or_404(OrgInvitation, token=token)

    if invitation.status == OrgInvitation.STATUS_CONFIRMED:
        return Response({'error': 'Invitation already confirmed.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == OrgInvitation.STATUS_CANCELLED:
        return Response({'error': 'Invitation has been cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == OrgInvitation.STATUS_EXPIRED:
        return Response({'error': 'Invitation has expired.'}, status=status.HTTP_400_BAD_REQUEST)

    # Prefer a local-auth identity; fall back to any identity with this email
    # so that OIDC/SSO users can also confirm invitations.
    identity = _find_identity_by_email(invitation.email)
    if not identity:
        return Response(
            {'error': 'No account found for this email. Please log in first, or contact your administrator.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Grant access — use get_or_create rather than update_or_create to avoid
    # silently downgrading an existing higher-privilege role (e.g. org_admin → doctor).
    # If the grant already exists, leave it unchanged; the invitation is still
    # marked confirmed so it can't be replayed.
    _grant_org_access(identity, invitation.org, invitation.role, invitation.invited_by)

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
