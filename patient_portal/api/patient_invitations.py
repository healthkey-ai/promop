"""
Patient invitation views — staff/providers invite a patient to claim their own
record; the patient sets a password and becomes a PHR Account Holder (PH.1).

Endpoints (registered in v1_urls.py):
  POST /api/v1/patients/                           - org admin: create a record, optionally invite or claim it
  POST /api/v1/patients/{person_id}/invite/        - staff/provider: create + email invite
  GET  /api/v1/patients/{person_id}/invitations/   - staff/provider: list invites for a patient
  GET  /api/v1/patient-invitations/lookup/?token=  - public: validate a token, return invitee email
  POST /api/v1/patient-invitations/accept/         - public: set password, create account + link
"""
import logging
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
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
from omop_core.services.access import get_admin_orgs
from .permissions import get_request_org, is_service_token, is_machine_request
from .serializers import PatientInvitationSerializer

logger = logging.getLogger(__name__)

class PatientInvitationEmailError(Exception):
    """Raised when an invitation email cannot be handed to the email backend."""


def _patient_display_name(person) -> str:
    name = " ".join(p for p in [person.given_name, person.family_name] if p).strip()
    return name or f"Patient {person.person_id}"


def _days(n: int) -> str:
    return '1 day' if n == 1 else f'{n} days'


def _send_patient_invitation_email(invitation) -> None:
    accept_url = f"{settings.APP_BASE_URL}/accept-patient-invite?token={invitation.token}"
    subject = "You've been invited to access your health record on PROMOP"
    body = (
        f"Hi,\n\n"
        f"Your care team has invited you to access your health record on PROMOP.\n\n"
        f"Click the link below to create your account and set a password:\n\n"
        f"  {accept_url}\n\n"
        f"This link expires in {_days(settings.INVITE_TTL_DAYS)}.\n\n"
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

        # Email may be supplied to set/update Person, else use the stored one.
        email = (request.data.get('email') or '').strip().lower()
        stored_email = person.email or (patient_record.email if patient_record else None)
        if not email and stored_email:
            email = stored_email.strip().lower()
        if not email:
            return Response(
                {'error': 'An email address is required. Add one to the patient record first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        refusal = _invitation_refusal(person, email)
        if refusal is not None:
            return refusal
        return _issue_invitation(request, person, patient_record, email)


def _invitation_refusal(person, email):
    """A 400 Response when ``email`` cannot be invited for ``person``, else None."""
    # A patient who already has an active account cannot be re-invited.
    if person is not None and PatientUser.objects.filter(person=person, is_active=True).exists():
        return Response(
            {'error': 'This patient already has an active account.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Reject if the email already belongs to an existing account — the
    # invite would be sent but acceptance would fail with "account already
    # exists", wasting time and confusing both parties.
    existing_identity = Identity.objects.filter(email__iexact=email).first()
    if existing_identity and existing_identity.has_usable_password():
        return Response(
            {'error': 'This email is already associated with an existing account. '
                      'Use a different email address.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


def _issue_invitation(request, person, patient_record, email, *, address_for_invitation=False):
    """Store the email on the Person, create or refresh the invitation, send it.

    ``address_for_invitation``: the person was just created with this address
    in order to invite them, so it is the invitation's, not the record's."""
    with transaction.atomic():
        invitation = PatientInvitation.objects.select_for_update().filter(
            person=person, accepted_at__isnull=True, cancelled_at__isnull=True,
        ).first()
        # Whether an invitation put this address on the person, and what it
        # replaced: an address the record already held is the hospital's.
        earlier = invitation or PatientInvitation.objects.filter(
            person=person, accepted_at__isnull=True, email__iexact=email, set_person_email=True,
        ).order_by('-sent_at').first()
        if (person.email or '').lower() != email.lower():
            set_person_email, replaced_email = True, person.email
            if earlier is not None and earlier.set_person_email:
                replaced_email = earlier.replaced_email  # the address before any invitation
        elif earlier is not None and earlier.set_person_email:
            set_person_email, replaced_email = True, earlier.replaced_email
        elif address_for_invitation:
            set_person_email, replaced_email = True, None
        else:
            set_person_email, replaced_email = False, None
        if person.email != email:
            person.email = email
            person.save(update_fields=['email'])

        if patient_record is None:
            patient_record = PatientRecord.objects.create(person=person)
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)

        token = secrets.token_hex(32)  # 64 hex chars
        now = timezone.now()
        expires_at = now + timezone.timedelta(days=settings.INVITE_TTL_DAYS)
        if invitation:
            invitation.email = email
            invitation.token = token
            invitation.invited_by = request.user
            invitation.sent_at = now
            invitation.expires_at = expires_at
            invitation.set_person_email, invitation.replaced_email = set_person_email, replaced_email
            invitation.save(update_fields=['email', 'token', 'invited_by', 'sent_at', 'expires_at',
                                           'set_person_email', 'replaced_email'])
        else:
            invitation = PatientInvitation.objects.create(
                person=person, email=email, token=token, invited_by=request.user, sent_at=now,
                expires_at=expires_at, set_person_email=set_person_email, replaced_email=replaced_email,
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


class PatientCreateView(APIView):
    """An org admin creates a patient record, and invites the patient or claims it.

    Body: ``organization`` (slug; may be omitted when the caller administers
    exactly one), ``given_name``, ``family_name``, ``email``, and either
    ``invite: true`` (email the patient a link to set their own password) or
    ``for_self: true`` (the admin's own record: linked to their account, with
    the ``patient`` role granted at the organization -- roles are additive, so
    the admin role is kept). Neither flag creates a bare record.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from omop_core.models import Organization
        user = request.user
        if is_machine_request(request):
            return Response({'error': 'Not permitted.'}, status=status.HTTP_403_FORBIDDEN)
        data = request.data if isinstance(request.data, dict) else {}
        orgs = get_admin_orgs(user).filter(is_active=True)
        if not orgs.exists():
            return Response({'error': 'Organization admin access is required.'},
                            status=status.HTTP_403_FORBIDDEN)
        slug = (data.get('organization') or '').strip()
        if slug:
            org = orgs.filter(slug=slug).first()
            if org is None:
                return Response({'error': 'You cannot add patients to this organization.'},
                                status=status.HTTP_403_FORBIDDEN)
        else:
            choices = list(orgs[:2])
            if len(choices) != 1:
                return Response({'error': 'Select an organization.'},
                                status=status.HTTP_400_BAD_REQUEST)
            org = choices[0]

        for_self = data.get('for_self') is True
        invite = data.get('invite') is True
        if for_self and invite:
            return Response({'error': 'A record for yourself is not invited by email.'},
                            status=status.HTTP_400_BAD_REQUEST)

        given_name = str(data.get('given_name') or '').strip()[:100]
        family_name = str(data.get('family_name') or '').strip()[:100]
        email = str(data.get('email') or '').strip().lower()

        if for_self:
            if PatientUser.objects.filter(identity=user).exists():
                return Response({'error': 'You already have a patient record.'},
                                status=status.HTTP_409_CONFLICT)
            given_name = given_name or (getattr(user, 'name', '') or '').split(' ')[0]
            email = email or (user.email or '').strip().lower()
            # Only a verified address may sit on a Person that email matching reads.
            if email != (user.email or '').strip().lower() or not getattr(user, 'has_verified_email', False):
                email = ''
        elif invite:
            if not email:
                return Response({'error': 'An email address is required to invite the patient.'},
                                status=status.HTTP_400_BAD_REQUEST)
            try:
                validate_email(email)
            except ValidationError:
                return Response({'error': 'Enter a valid email address.'},
                                status=status.HTTP_400_BAD_REQUEST)
        if not for_self and not (given_name or family_name or email):
            return Response({'error': 'Enter a name or an email address.'},
                            status=status.HTTP_400_BAD_REQUEST)

        if invite:
            # Refuse before anything is written: an unusable address leaves no stray record.
            refusal = _invitation_refusal(None, email)
            if refusal is not None:
                return refusal

        from omop_core.services.patient_record_service import refresh_patient_record
        from omop_core.services.pk import next_pk
        with transaction.atomic():
            person = Person.objects.create(
                person_id=next_pk(Person, 'person_id'),
                year_of_birth=1900,
                gender_source_value='unknown',
                race_source_value='unknown',
                ethnicity_source_value='unknown',
                given_name=given_name or None,
                family_name=family_name or None,
                email=email or None,
            )
            record = PatientRecord.objects.create(person=person, organization=org)
            if for_self:
                PatientUser.objects.create(identity=user, person=person)
                GroupAccess.objects.get_or_create(identity=user, org=org, role='patient')
            refresh_patient_record(person)

        result = {'person_id': person.person_id, 'organization': org.slug, 'invited': False}
        if invite:
            invited = _issue_invitation(request, person, record, email, address_for_invitation=True)
            result['invited'] = True
            result['invitation'] = invited.data
            if invited.data.get('email_warning'):
                result['email_warning'] = invited.data['email_warning']
        return Response(result, status=status.HTTP_201_CREATED)


def _find_local_or_any_identity(email):
    """Find the best existing Identity for an email, across all issuers.

    Preference order: local with usable password → local placeholder → any issuer.
    Returns None if no identity exists for this email.
    """
    from django.conf import settings

    # A provider-made staff account is never claimed: it has no usable
    # password and must not gain one or a patient record, so its holder gets a
    # member account of their own. A local staff account still counts here, so
    # the "an account already exists" refusal applies to it as before: a second
    # local account on the same address would break password sign-in for both.
    from django.db.models import Q

    staff = Q(is_staff=True) | Q(is_superuser=True) | Q(groups__name__in=settings.STAFF_ROLES)
    identities = list(
        Identity.objects.filter(email__iexact=email).exclude(staff & ~Q(issuer='urn:local'))
        .distinct().order_by('id')
    )
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


class _Refused(Exception):
    """Raised inside the acceptance transaction to roll it back and answer ``response``."""

    def __init__(self, response):
        super().__init__()
        self.response = response


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def accept_patient_invitation(request):
    """Public: the patient sets a password; create/reuse a local Identity and link the Person.

    The sign-up form's answers travel in the same body and are written in the
    same transaction (``patient_portal.signup``): ``country`` (ISO alpha-2 from
    the sign-up policy), ``year_of_birth`` (year only), and ``consent`` with the
    ``decisions`` and ``documents`` the deployment asks for at sign-up, each
    naming the version the form showed. A deployment with sign-up declarations
    or documents loaded refuses an acceptance that omits one (400); wording
    republished since the form loaded is a 409.
    """
    from patient_portal import signup
    from patient_portal.api.phr.consent import language_of
    from patient_portal.consent import StaleVersion

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
    data = request.data if isinstance(request.data, dict) else {}
    language = language_of(request)
    record = None
    try:
        with transaction.atomic():
            # Two requests racing on one token must not both record the
            # sign-up: lock the row and re-check it before anything is written.
            invitation = PatientInvitation.objects.select_for_update().select_related('person').get(pk=invitation.pk)
            if invitation.accepted_at or invitation.cancelled_at:
                raise _Refused(Response(
                    {'error': 'Invitation already accepted. Please sign in.'},
                    status=status.HTTP_400_BAD_REQUEST,
                ))
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
                raise _Refused(Response(
                    {'error': 'An account already exists for this email. Please sign in instead.'},
                    status=status.HTTP_400_BAD_REQUEST,
                ))

            # The token reached the invited mailbox; holding it proves the address.
            if (identity.email or '').lower() == email.lower():
                identity.mark_email_verified()

            try:
                PatientUser.objects.update_or_create(
                    person=invitation.person,
                    defaults={'identity': identity, 'is_active': True},
                )
            except IntegrityError:
                # The identity is already linked to a different Person.
                raise _Refused(Response(
                    {'error': 'This account is already linked to another patient record.'},
                    status=status.HTTP_400_BAD_REQUEST,
                ))

            # Keep Person and its PatientRecord projection aligned with the claimed account.
            person = invitation.person
            if person.email != email:
                person.email = email
                person.save(update_fields=['email'])
            record = PatientRecord.objects.filter(person=person).select_related('organization').first()

            # The form's country, year of birth and declarations; derives the
            # record once the country and year are written, then records the
            # consent against the derived country.
            from omop_core.services.patient_record_service import refresh_patient_record
            try:
                signup.apply_signup(
                    person, record, email=email, data=data, language=language,
                    recorded_by=identity, refresh=refresh_patient_record,
                )
            except StaleVersion as e:
                raise _Refused(Response({'error': e.messages[0]}, status=status.HTTP_409_CONFLICT))
            except ValidationError as e:
                raise _Refused(Response({'error': e.messages[0]}, status=status.HTTP_400_BAD_REQUEST))

            # Grant patient-level access to the record's org so the patient
            # can use org-scoped views after signing in.
            if record and record.organization:
                GroupAccess.objects.get_or_create(
                    identity=identity,
                    org=record.organization,
                    role='patient',
                )

            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=['accepted_at'])
    except _Refused as refused:
        return refused.response
    except IntegrityError:
        logger.exception('IntegrityError during patient invitation acceptance for token=%s…%s',
                         token[:8], token[-4:])
        return Response(
            {'error': 'Could not create the account. Please contact your care team.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    resp = {'detail': 'Account created. You can now sign in with your email and password.'}
    if record and record.organization:
        resp['redirect_url'] = f'/org/{record.organization.slug}/'
    return Response(resp)
