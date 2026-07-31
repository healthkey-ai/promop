"""
Patient invitation views — staff/providers invite a patient to claim their own
record; the patient sets a password and becomes a PHR Account Holder (PH.1).

Endpoints (registered in v1_urls.py):
  POST /api/v1/patients/{person_id}/invite/        - staff/provider: create + email invite
  GET  /api/v1/patients/{person_id}/invitations/   - staff/provider: list invites for a patient
  GET  /api/v1/patient-invitations/lookup/?token=  - public: validate a token, return invitee email
  POST /api/v1/patient-invitations/accept/         - public: set password, create account + link
"""
import logging
import secrets

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.models import Person, PatientRecord, GroupAccess
from patient_portal.models import Identity, PatientInvitation, PatientUser
from .permissions import get_request_org, is_service_token
from .serializers import PatientInvitationSerializer

logger = logging.getLogger(__name__)

INVITE_TTL_DAYS = 7


class PatientInvitationEmailError(Exception):
    """Raised when an invitation email cannot be handed to the email backend."""


def _patient_display_name(person) -> str:
    name = " ".join(p for p in [person.given_name, person.family_name] if p).strip()
    return name or f"Patient {person.person_id}"


def _send_patient_invitation_email(invitation) -> None:
    accept_url = f"{settings.APP_BASE_URL}/accept-patient-invite?token={invitation.token}"
    subject = "You've been invited to access your health record on PROMOP"
    body = (
        f"Hi,\n\n"
        f"Your care team has invited you to access your health record on PROMOP.\n\n"
        f"Click the link below to create your account and set a password:\n\n"
        f"  {accept_url}\n\n"
        f"This link expires in {INVITE_TTL_DAYS} days.\n\n"
        f"If you weren't expecting this invitation, you can ignore this email.\n\n"
        f"— The HealthKey team"
    )
    if settings.DEBUG:
        logger.info(
            "Patient invitation email preview\nTo: %s\nFrom: %s\nSubject: %s\n\n%s",
            invitation.email, settings.DEFAULT_FROM_EMAIL, subject, body,
        )
    try:
        sent_count = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [invitation.email])
    except Exception as exc:
        logger.exception("Failed to send patient invitation email to %s: %s", invitation.email, exc)
        raise PatientInvitationEmailError from exc
    if sent_count != 1:
        logger.error("Email backend reported %s emails sent to %s", sent_count, invitation.email)
        raise PatientInvitationEmailError


def _can_manage_patient(request, patient_record) -> bool:
    """Mirror PatientRecordViewSet.partial_update's authorization for write access."""
    user = request.user
    if is_service_token(request):
        return True
    if getattr(user, 'is_staff', False):
        return True
    org = get_request_org(request)
    if org is not None:
        return patient_record is not None and patient_record.organization_id == org.id
    from omop_core.authorization import can_write_patient
    return can_write_patient(user, patient_record.person_id) if patient_record else False


class PatientInviteView(APIView):
    """Staff/providers create (or re-send) an invitation for a patient's own record."""
    permission_classes = [IsAuthenticated]

    def get(self, request, person_id):
        person = get_object_or_404(Person, person_id=person_id)
        patient_record = PatientRecord.objects.filter(person=person).first()
        if not _can_manage_patient(request, patient_record):
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        invitations = PatientInvitation.objects.filter(person=person).order_by('-created_at')
        return Response(PatientInvitationSerializer(invitations, many=True).data)

    def post(self, request, person_id):
        person = get_object_or_404(Person, person_id=person_id)
        patient_record = PatientRecord.objects.filter(person=person).first()
        if not _can_manage_patient(request, patient_record):
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)

        # Email may be supplied to set/update the record, else use the stored one.
        email = (request.data.get('email') or '').strip().lower()
        if not email and patient_record and patient_record.email:
            email = patient_record.email.strip().lower()
        if not email:
            return Response(
                {'error': 'An email address is required. Add one to the patient record first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # A patient who already has an active account cannot be re-invited.
        if PatientUser.objects.filter(person=person, is_active=True).exists():
            return Response(
                {'error': 'This patient already has an active account.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Reject if the email already belongs to an existing account — the
        # invite would be sent but acceptance would fail with "account already
        # exists", wasting time and confusing both parties.
        existing_identity = Identity.objects.filter(
            email__iexact=email,
        ).first()
        if existing_identity and existing_identity.has_usable_password():
            return Response(
                {'error': 'This email is already associated with an existing account. '
                          'Use a different email address.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if patient_record is None:
                patient_record = PatientRecord.objects.create(person=person, email=email)
            elif patient_record.email != email:
                patient_record.email = email
                patient_record.save(update_fields=['email'])

            token = secrets.token_hex(32)  # 64 hex chars
            expires_at = timezone.now() + timezone.timedelta(days=INVITE_TTL_DAYS)
            invitation = PatientInvitation.objects.select_for_update().filter(
                person=person, accepted_at__isnull=True, cancelled_at__isnull=True,
            ).first()
            if invitation:
                invitation.email = email
                invitation.token = token
                invitation.invited_by = request.user
                invitation.expires_at = expires_at
                invitation.save(update_fields=['email', 'token', 'invited_by', 'expires_at'])
            else:
                invitation = PatientInvitation.objects.create(
                    person=person, email=email, token=token,
                    invited_by=request.user, expires_at=expires_at,
                )

        email_warning = None
        try:
            _send_patient_invitation_email(invitation)
        except PatientInvitationEmailError:
            email_warning = 'Invitation was created, but the email could not be sent.'

        data = PatientInvitationSerializer(invitation).data
        if email_warning:
            data['email_warning'] = email_warning
        return Response(data, status=status.HTTP_201_CREATED)


def _find_local_or_any_identity(email):
    """Find the best existing Identity for an email, across all issuers.

    Preference order: local with usable password → local placeholder → any issuer.
    Returns None if no identity exists for this email.
    """
    identities = list(Identity.objects.filter(email__iexact=email).order_by('id'))
    return (
        next((i for i in identities if i.issuer == 'urn:local' and i.has_usable_password()), None)
        or next((i for i in identities if i.issuer == 'urn:local'), None)
        or next((i for i in identities if i.issuer != 'urn:local'), None)
        or None
    )


def _resolve_invitation(token):
    """Return (invitation, error_response). One will be None."""
    if not token:
        return None, Response({'error': 'token is required'}, status=status.HTTP_400_BAD_REQUEST)
    # Tokens are 64 lowercase hex chars; reject malformed input before hitting the DB.
    if len(token) != 64 or not token.isalnum():
        return None, Response({'error': 'Invalid token format.'}, status=status.HTTP_400_BAD_REQUEST)
    invitation = PatientInvitation.objects.filter(token=token).select_related('person').first()
    if invitation is None:
        return None, Response({'error': 'Invitation not found.'}, status=status.HTTP_404_NOT_FOUND)
    if invitation.status == PatientInvitation.STATUS_ACCEPTED:
        return None, Response({'error': 'Invitation already accepted. Please sign in.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == PatientInvitation.STATUS_CANCELLED:
        return None, Response({'error': 'Invitation has been cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == PatientInvitation.STATUS_EXPIRED:
        return None, Response({'error': 'Invitation has expired.'}, status=status.HTTP_400_BAD_REQUEST)
    return invitation, None


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_invitation_lookup(request):
    """Public: validate a token and return the invitee email + patient name for the sign-up form."""
    token = (request.query_params.get('token') or '').strip()
    invitation, err = _resolve_invitation(token)
    if err is not None:
        return err
    data = {
        'email': invitation.email,
        'patient_name': _patient_display_name(invitation.person),
    }
    # Include org slug so the frontend can build org-scoped URLs (login redirect on error).
    pr = PatientRecord.objects.filter(person=invitation.person).select_related('organization').first()
    if pr and pr.organization:
        data['org_slug'] = pr.organization.slug
    return Response(data)


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def accept_patient_invitation(request):
    """Public: the patient sets a password; create/reuse a local Identity and link the Person."""
    token = (request.data.get('token') or '').strip()
    password = request.data.get('password') or ''

    invitation, err = _resolve_invitation(token)
    if err is not None:
        return err

    from patient_portal.services import password_validation_errors
    pw_errors = password_validation_errors(password, email=invitation.email)
    if pw_errors:
        return Response(
            {'error': ' '.join(pw_errors)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    email = invitation.email
    pr = None
    try:
        with transaction.atomic():
            # Search all issuers — not just urn:local — so we don't create a
            # duplicate Identity when the email already has an SSO/OAuth account.
            identity = _find_local_or_any_identity(email)
            from patient_portal.services import record_password
            if identity is None:
                identity = Identity.objects.create_user(email=email, password=password)
                record_password(identity)
            elif not identity.has_usable_password():
                # Claim a placeholder local account (e.g. one created by an earlier
                # invite with no password yet). Safe to set the chosen password.
                identity.set_password(password)
                identity.is_active = True
                identity.save(update_fields=['password', 'is_active'])
                record_password(identity)
            else:
                # A real account already exists for this email — never overwrite its
                # credentials from a token-gated public endpoint (that email could
                # belong to a provider/admin). Send them to sign in instead.
                return Response(
                    {'error': 'An account already exists for this email. Please sign in instead.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                PatientUser.objects.update_or_create(
                    person=invitation.person,
                    defaults={'identity': identity, 'is_active': True},
                )
            except IntegrityError:
                # The identity is already linked to a different Person.
                return Response(
                    {'error': 'This account is already linked to another patient record.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Keep the record email aligned with the claimed account.
            pr = PatientRecord.objects.filter(person=invitation.person).first()
            if pr and pr.email != email:
                pr.email = email
                pr.save(update_fields=['email'])

            # Grant patient-level access to the record's org so the patient
            # can use org-scoped views after signing in.
            if pr and pr.organization:
                GroupAccess.objects.get_or_create(
                    identity=identity,
                    org=pr.organization,
                    defaults={'role': 'patient'},
                )

            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=['accepted_at'])
    except IntegrityError:
        logger.exception('IntegrityError during patient invitation acceptance for token=%s…%s',
                         token[:8], token[-4:])
        return Response(
            {'error': 'Could not create the account. Please contact your care team.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resp = {'detail': 'Account created. You can now sign in with your email and password.'}
    if pr and pr.organization:
        resp['redirect_url'] = f'/org/{pr.organization.slug}/'
    return Response(resp)
