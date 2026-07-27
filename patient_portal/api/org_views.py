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
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)
DEFAULT_ANALYST_REDIRECT_URL = 'https://analytics.healthkey.ai'


class InvitationEmailError(Exception):
    """Raised when an invitation email cannot be handed to the email backend."""


def _send_invitation_email(invitation) -> None:
    slug = invitation.org.slug
    accept_url = f"{settings.APP_BASE_URL}/org/{slug}/accept-invite?token={invitation.token}"
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
            "Failed to send invitation email to %s via %s domain=%s from=%s: %s",
            invitation.email,
            settings.EMAIL_BACKEND,
            settings.ANYMAIL.get('MAILGUN_SENDER_DOMAIN', ''),
            settings.DEFAULT_FROM_EMAIL,
            exc,
        )
        raise InvitationEmailError from exc
    if sent_count != 1:
        logger.error(
            "Email backend reported %s invitation emails sent to %s via %s domain=%s from=%s",
            sent_count,
            invitation.email,
            settings.EMAIL_BACKEND,
            settings.ANYMAIL.get('MAILGUN_SENDER_DOMAIN', ''),
            settings.DEFAULT_FROM_EMAIL,
        )
        raise InvitationEmailError

from omop_core.models import Organization, OrgTrust, OrgInvitation, GroupAccess
from omop_core.services.organization_cleanup import delete_organization_with_patient_cascade
from omop_core.services.access import get_admin_orgs
from omop_core.services.pk import next_pk
from patient_portal.models import Identity, PatientUser
from omop_core.models import Person, PatientRecord
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
    """Return orgs the user may administer, including trust-derived admin access."""
    return get_admin_orgs(user)


def _find_identity_by_email(email):
    identities = list(Identity.objects.filter(email__iexact=email).order_by('id'))
    return (
        next((identity for identity in identities if identity.issuer != 'urn:local'), None)
        or next((identity for identity in identities if identity.issuer == 'urn:local' and identity.has_usable_password()), None)
        or next((identity for identity in identities if identity.issuer == 'urn:local'), None)
        or None
    )


def _get_or_create_invitee_identity(email):
    identity = _find_identity_by_email(email)
    if identity:
        return identity
    return Identity.objects.create_user(email=email, password=None)


ROLE_RANK = {'org_admin': 3, 'doctor': 2, 'analyst': 1, 'patient': 0}


def _normalize_redirect_url(role, redirect_url):
    if role != 'analyst':
        return ''

    candidate = (redirect_url or '').strip() or DEFAULT_ANALYST_REDIRECT_URL
    validator = URLValidator(schemes=['http', 'https'])
    validator(candidate)
    return candidate


def _grant_org_access(identity, org, role, granted_by, redirect_url=''):
    normalized_redirect_url = _normalize_redirect_url(role, redirect_url)
    existing = GroupAccess.objects.filter(identity=identity, org=org).first()
    if existing:
        changed_fields = []
        if ROLE_RANK.get(existing.role, 0) < ROLE_RANK.get(role, 0):
            existing.role = role
            existing.granted_by = granted_by
            changed_fields.extend(['role', 'granted_by'])

        desired_redirect_url = normalized_redirect_url if existing.role == 'analyst' else ''
        if existing.redirect_url != desired_redirect_url:
            existing.redirect_url = desired_redirect_url
            changed_fields.append('redirect_url')

        if changed_fields:
            existing.save(update_fields=changed_fields)
        return existing
    return GroupAccess.objects.create(
        identity=identity,
        org=org,
        role=role,
        redirect_url=normalized_redirect_url if role == 'analyst' else '',
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
        delete_organization_with_patient_cascade(org)
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
        redirect_url = request.data.get('redirect_url', '')
        person_id = request.data.get('person_id')

        if not email:
            return Response({'error': 'email is required'}, status=status.HTTP_400_BAD_REQUEST)
        if role not in dict(OrgInvitation.ROLE):
            return Response({'error': f'Invalid role: {role}'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            normalized_redirect_url = _normalize_redirect_url(role, redirect_url)
        except ValidationError:
            return Response(
                {'error': 'redirect_url must be a valid http(s) URL.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate optional person_id for patient invites
        linked_person = None
        if person_id is not None:
            if role != 'patient':
                return Response(
                    {'error': 'person_id can only be set for patient invites.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            linked_person = Person.objects.filter(person_id=person_id).first()
            if not linked_person:
                return Response(
                    {'error': f'Person {person_id} not found.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        with transaction.atomic():
            invitation = OrgInvitation.objects.select_for_update().filter(
                org=org, email=email,
                confirmed_at__isnull=True, cancelled_at__isnull=True,
            ).first()

            token = secrets.token_hex(32)  # 64 hex chars
            expires_at = timezone.now() + timezone.timedelta(days=7)
            if invitation:
                invitation.role = role
                invitation.redirect_url = normalized_redirect_url
                invitation.token = token
                invitation.invited_by = request.user
                invitation.expires_at = expires_at
                invitation.person = linked_person
                invitation.save(
                    update_fields=['role', 'redirect_url', 'token', 'invited_by', 'expires_at', 'person']
                )
            else:
                invitation = OrgInvitation.objects.create(
                    org=org,
                    email=email,
                    role=role,
                    redirect_url=normalized_redirect_url,
                    token=token,
                    person=linked_person,
                    invited_by=request.user,
                    expires_at=expires_at,
                )

            identity = _get_or_create_invitee_identity(email)
            _grant_org_access(identity, org, role, request.user, redirect_url=normalized_redirect_url)

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
        data['access_granted'] = True
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

    with transaction.atomic():
        # Grant access — use get_or_create rather than update_or_create to avoid
        # silently downgrading an existing higher-privilege role (e.g. org_admin → doctor).
        # If the grant already exists, leave it unchanged; the invitation is still
        # marked confirmed so it can't be replayed.
        grant = _grant_org_access(
            identity,
            invitation.org,
            invitation.role,
            invitation.invited_by,
            redirect_url=invitation.redirect_url,
        )

        # For patient invites, create the PatientUser link
        if invitation.role == 'patient':
            person = invitation.person
            if person is None:
                # No pre-existing person — create a new one
                new_id = next_pk(Person, 'person_id')
                person = Person.objects.create(
                    person_id=new_id,
                    year_of_birth=1900,
                    gender_source_value='unknown',
                    race_source_value='unknown',
                    ethnicity_source_value='unknown',
                )
                PatientRecord.objects.create(person=person, email=invitation.email)

            # Link the identity to the person (idempotent)
            PatientUser.objects.get_or_create(
                identity=identity,
                defaults={'person': person},
            )

        invitation.confirmed_at = timezone.now()
        invitation.save(update_fields=['confirmed_at'])

    response_data = {'detail': f'Invitation confirmed. Access granted to {invitation.org.name}.'}
    if grant.redirect_url:
        response_data['redirect_url'] = grant.redirect_url
    if invitation.role == 'patient':
        response_data['redirect_url'] = f'/org/{invitation.org.slug}/'
    return Response(response_data)


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

    def patch(self, request, slug, access_id):
        """Update role and/or premium status for an access grant.

        Allowed fields: role ('doctor' | 'analyst'), is_premium (bool).
        org_admin grants cannot be changed; premium is not applicable to org_admins.
        """
        org = _get_org(slug)
        grant = get_object_or_404(GroupAccess, id=access_id, org=org)

        if grant.role == 'org_admin':
            return Response(
                {'error': 'Cannot modify org_admin grants via this endpoint.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        changed_grant = []
        changed_identity = []

        new_role = request.data.get('role')
        if new_role is not None:
            if new_role not in ('doctor', 'analyst'):
                return Response(
                    {'error': "role must be 'doctor' or 'analyst'"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            grant.role = new_role
            changed_grant.append('role')
            normalized_redirect_url = _normalize_redirect_url(new_role, grant.redirect_url)
            desired_redirect_url = normalized_redirect_url if new_role == 'analyst' else ''
            if grant.redirect_url != desired_redirect_url:
                grant.redirect_url = desired_redirect_url
                changed_grant.append('redirect_url')

        if 'is_premium' in request.data:
            raw = request.data['is_premium']
            is_premium = raw if isinstance(raw, bool) else str(raw).lower() in ('true', '1', 'yes')
            grant.identity.is_premium = is_premium
            changed_identity.append('is_premium')

        if changed_grant:
            grant.save(update_fields=changed_grant)
        if changed_identity:
            grant.identity.save(update_fields=changed_identity)

        return Response(GroupAccessSerializer(grant).data)

    def delete(self, request, slug, access_id):
        org = _get_org(slug)
        grant = get_object_or_404(GroupAccess, id=access_id, org=org)
        grant.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Public org info (unauthenticated)
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([AllowAny])
def org_public_info(request, slug):
    """Return minimal public info about an org for the login/signup pages."""
    org = get_object_or_404(Organization, slug=slug, is_active=True)
    return Response({
        'name': org.name,
        'slug': org.slug,
        'allows_patient_signup': org.allows_patient_signup,
    })


# ---------------------------------------------------------------------------
# Patient self-signup (public, rate-limited)
# ---------------------------------------------------------------------------

class OrgPatientSignupView(APIView):
    """Public endpoint for patient self-registration on orgs that allow it."""
    permission_classes = [AllowAny]
    throttle_scope = 'patient_signup'

    def post(self, request, slug):
        org = get_object_or_404(Organization, slug=slug, is_active=True)
        if not org.allows_patient_signup:
            return Response(
                {'error': 'This organization does not allow direct patient signup.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password', '')
        given_name = (request.data.get('given_name') or '').strip()
        family_name = (request.data.get('family_name') or '').strip()

        if not email:
            return Response({'error': 'email is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not password:
            return Response({'error': 'password is required.'}, status=status.HTTP_400_BAD_REQUEST)

        from patient_portal.services import password_validation_errors, set_new_password
        pw_errors = password_validation_errors(password, email=email)
        if pw_errors:
            return Response({'error': pw_errors}, status=status.HTTP_400_BAD_REQUEST)

        # Check for existing account with this email
        existing = _find_identity_by_email(email)
        if existing and existing.has_usable_password():
            return Response(
                {'error': 'An account with this email already exists. Please log in instead.'},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            if existing:
                # Placeholder identity from a prior invitation — set its password
                identity = existing
                set_new_password(identity, password)
                if given_name:
                    identity.name = f"{given_name} {family_name}".strip()
                    identity.save(update_fields=['name'])
            else:
                identity = Identity.objects.create_user(
                    email=email,
                    password=password,
                    name=f"{given_name} {family_name}".strip(),
                )

            # Create Person + PatientRecord + PatientUser
            new_id = next_pk(Person, 'person_id')
            person = Person.objects.create(
                person_id=new_id,
                given_name=given_name or None,
                family_name=family_name or None,
                year_of_birth=1900,
                gender_source_value='unknown',
                race_source_value='unknown',
                ethnicity_source_value='unknown',
            )
            PatientRecord.objects.create(person=person, email=email)
            PatientUser.objects.get_or_create(
                identity=identity,
                defaults={'person': person},
            )

            # Grant patient access to this org
            GroupAccess.objects.get_or_create(
                identity=identity,
                org=org,
                defaults={'role': 'patient'},
            )

        # Auto-login via session
        from django.contrib.auth import login
        login(request, identity, backend='patient_portal.backends.EmailBackend')

        return Response(
            {
                'person_id': person.person_id,
                'redirect_url': f'/org/{slug}/',
            },
            status=status.HTTP_201_CREATED,
        )
