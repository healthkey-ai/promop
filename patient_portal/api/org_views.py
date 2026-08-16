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
from django.db.models import Count, F, Q
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
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
    # Bare /accept-invite path — matches the SPA route (App.tsx) and the emailed-link
    # convention used by /reset-password and /accept-patient-invite. An org-scoped
    # /org/<slug>/accept-invite path has no route and gets swallowed by the catch-all,
    # so the accept page (and its post-accept redirect) never runs. The token alone
    # identifies the invitation and its org; the slug is not needed here.
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
        f"— The HealthKey team"
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

from omop_core.models import (
    ConditionOccurrence, DrugExposure, Measurement, Observation,
    Organization, OrgTrust, OrgInvitation, GroupAccess, ProcedureOccurrence,
)
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

NONCONFORMING_VOCABULARIES = frozenset({'LOCAL', 'FHIR', 'sct'})

VOCABULARY_USAGE_SOURCES = (
    (ConditionOccurrence, 'condition_occurrence', (
        ('condition_concept', 'concept'),
        ('condition_source_concept', 'source_concept'),
    )),
    (ProcedureOccurrence, 'procedure_occurrence', (
        ('procedure_concept', 'concept'),
        ('procedure_source_concept', 'source_concept'),
    )),
    (DrugExposure, 'drug_exposure', (
        ('drug_concept', 'concept'),
        ('drug_source_concept', 'source_concept'),
    )),
    (Measurement, 'measurement', (
        ('measurement_concept', 'concept'),
        ('measurement_source_concept', 'source_concept'),
    )),
    (Observation, 'observation', (
        ('observation_concept', 'concept'),
        ('observation_source_concept', 'source_concept'),
    )),
)


def _vocabulary_group(vocabulary_id, source):
    if vocabulary_id in NONCONFORMING_VOCABULARIES:
        return 'nonconforming', 2, 'Non-conforming'
    if (vocabulary_id or '').startswith('HK-') or source == 'HealthKey':
        return 'healthkey', 1, f'HealthKey: {vocabulary_id}'
    return 'athena', 0, 'Athena / external'


def _org_vocabulary_usage(org):
    concepts = {}

    def ensure_concept(row):
        concept_id = row['concept_id']
        if concept_id not in concepts:
            group, group_order, group_label = _vocabulary_group(
                row['vocabulary_id'], row['source'],
            )
            concepts[concept_id] = {
                'concept_id': concept_id,
                'vocabulary_id': row['vocabulary_id'],
                'concept_code': row['concept_code'],
                'concept_name': row['concept_name'],
                'domain_id': row['domain_id'],
                'standard_concept': row['standard_concept'],
                'source': row['source'],
                'group': group,
                'group_label': group_label,
                'group_order': group_order,
                'patient_ids': set(),
                'instance_count': 0,
                'usage': [],
            }
        return concepts[concept_id]

    for model, table_name, fields in VOCABULARY_USAGE_SOURCES:
        base_qs = model.objects.filter(person__patient_record__organization=org)
        pk_name = model._meta.pk.name
        for field_name, role in fields:
            field_filter = {f'{field_name}__isnull': False}
            value_exprs = {
                'concept_id': F(f'{field_name}_id'),
                'vocabulary_id': F(f'{field_name}__vocabulary_id'),
                'concept_code': F(f'{field_name}__concept_code'),
                'concept_name': F(f'{field_name}__concept_name'),
                'domain_id': F(f'{field_name}__domain_id'),
                'standard_concept': F(f'{field_name}__standard_concept'),
                'source': F(f'{field_name}__source'),
            }
            usage_rows = (
                base_qs
                .filter(**field_filter)
                .values(**value_exprs)
                .annotate(instance_count=Count(pk_name))
            )
            for row in usage_rows:
                concept = ensure_concept(row)
                count = row['instance_count']
                concept['instance_count'] += count
                concept['usage'].append({
                    'table': table_name,
                    'column': f'{field_name}_id',
                    'role': role,
                    'instance_count': count,
                })

            patient_rows = (
                base_qs
                .filter(**field_filter)
                .values('person_id', concept_id=F(f'{field_name}_id'))
                .distinct()
            )
            for row in patient_rows:
                if row['concept_id'] in concepts:
                    concepts[row['concept_id']]['patient_ids'].add(row['person_id'])

    result = []
    for concept in concepts.values():
        concept['patient_count'] = len(concept.pop('patient_ids'))
        concept['usage'].sort(key=lambda item: (-item['instance_count'], item['table'], item['column']))
        result.append(concept)

    result.sort(key=lambda row: (
        row['group_order'],
        row['vocabulary_id'] or '',
        -row['patient_count'],
        -row['instance_count'],
        row['concept_name'] or '',
    ))
    return result


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
        unit_policy_changed = (
            'clinical_unit_system' in ser.validated_data
            and ser.validated_data['clinical_unit_system'] != org.clinical_unit_system
        )
        ser.save()
        if unit_policy_changed:
            # The setting changes a derived compatibility field. Mark exactly
            # this tenant's rows stale; operators can rederive them without
            # putting a potentially large refresh on the admin PATCH request.
            PatientRecord.objects.filter(organization=org).update(derivation_version=0)
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


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def confirm_invitation(request):
    """Public endpoint — confirms an invitation by token and creates a GroupAccess."""
    token = request.data.get('token', '').strip()
    password = request.data.get('password') or ''
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

    # Mirror the patient-invite flow for professional-role invitees: someone with
    # no account, or a local placeholder that has never set a password, sets one
    # here. SSO/OIDC users (non-local issuer) authenticate via their provider and
    # need no local password. The patient role has its own dedicated invite flow,
    # so it is left untouched here.
    needs_password = (
        invitation.role != 'patient'
        and (
            identity is None
            or (identity.issuer == 'urn:local' and not identity.has_usable_password())
        )
    )
    if needs_password:
        from patient_portal.services import password_validation_errors
        pw_errors = password_validation_errors(password, email=invitation.email)
        if pw_errors:
            return Response({'error': ' '.join(pw_errors)}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        if needs_password:
            from patient_portal.services import record_password
            if identity is None:
                identity = Identity.objects.create_user(email=invitation.email, password=password)
                record_password(identity)
            else:
                # Claim the placeholder account created at invite time.
                identity.set_password(password)
                identity.is_active = True
                identity.save(update_fields=['password', 'is_active'])
                record_password(identity)

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
            existing_pu = PatientUser.objects.filter(identity=identity).first()
            if existing_pu:
                person = existing_pu.person
            elif invitation.person:
                person = invitation.person
                PatientUser.objects.create(identity=identity, person=person)
            else:
                # No pre-existing person — create a new one
                new_id = next_pk(Person, 'person_id')
                person = Person.objects.create(
                    person_id=new_id,
                    year_of_birth=1900,
                    gender_source_value='unknown',
                    race_source_value='unknown',
                    ethnicity_source_value='unknown',
                )
                PatientRecord.objects.create(
                    person=person, email=invitation.email,
                    organization=invitation.org,
                )
                PatientUser.objects.create(identity=identity, person=person)

        invitation.confirmed_at = timezone.now()
        invitation.save(update_fields=['confirmed_at'])

    response_data = {'detail': f'Invitation confirmed. Access granted to {invitation.org.name}.'}
    if grant.redirect_url:
        response_data['redirect_url'] = grant.redirect_url
    if invitation.role == 'patient':
        response_data['redirect_url'] = f'/org/{invitation.org.slug}/'
    return Response(response_data)


@api_view(['GET'])
@permission_classes([AllowAny])
def org_invitation_lookup(request):
    """Public: validate an org-invitation token and return what the accept page needs.

    ``needs_password`` is True when the invitee must choose a password to accept —
    i.e. there is no account yet, or only a local placeholder that has never set
    one. It is False for existing accounts and for SSO/OIDC identities (which
    authenticate via their provider), so the accept page only prompts for a
    password when one is actually required.
    """
    token = (request.query_params.get('token') or '').strip()
    if not token:
        return Response({'error': 'token is required'}, status=status.HTTP_400_BAD_REQUEST)
    if len(token) != 64 or not token.isalnum():
        return Response({'error': 'Invalid token format.'}, status=status.HTTP_400_BAD_REQUEST)

    invitation = OrgInvitation.objects.filter(token=token).select_related('org').first()
    if invitation is None:
        return Response({'error': 'Invitation not found.'}, status=status.HTTP_404_NOT_FOUND)
    if invitation.status == OrgInvitation.STATUS_CONFIRMED:
        return Response({'error': 'Invitation already confirmed.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == OrgInvitation.STATUS_CANCELLED:
        return Response({'error': 'Invitation has been cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
    if invitation.status == OrgInvitation.STATUS_EXPIRED:
        return Response({'error': 'Invitation has expired.'}, status=status.HTTP_400_BAD_REQUEST)

    identity = _find_identity_by_email(invitation.email)
    needs_password = (
        invitation.role != 'patient'
        and (
            identity is None
            or (identity.issuer == 'urn:local' and not identity.has_usable_password())
        )
    )
    return Response({
        'email': invitation.email,
        'org_name': invitation.org.name,
        'role': invitation.get_role_display(),
        'needs_password': needs_password,
    })


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
        if grant.role == 'patient':
            return Response(
                {'error': 'Cannot modify patient grants via this endpoint.'},
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


class OrgVocabularyUsageView(APIView):
    permission_classes = [IsStaffOrOrgAdmin]

    def get(self, request, slug):
        org = _get_org(slug)
        concepts = _org_vocabulary_usage(org)
        return Response({
            'org_slug': org.slug,
            'org_name': org.name,
            'concept_count': len(concepts),
            'concepts': concepts,
        })


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

@method_decorator(csrf_exempt, name='dispatch')
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

        # Collect field-level validation errors so the frontend can display
        # them next to the relevant input rather than as a single blob.
        field_errors: dict[str, list[str]] = {}

        if not email:
            field_errors.setdefault('email', []).append('Email is required.')
        else:
            try:
                from django.core.validators import validate_email as _validate_email
                _validate_email(email)
            except ValidationError:
                field_errors.setdefault('email', []).append(
                    'Enter a valid email address.'
                )

        if not password:
            field_errors.setdefault('password', []).append('Password is required.')
        else:
            from patient_portal.services import password_validation_errors
            pw_errors = password_validation_errors(password, email=email)
            if pw_errors:
                field_errors.setdefault('password', []).extend(pw_errors)

        if field_errors:
            return Response(
                {'errors': field_errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from patient_portal.services import set_new_password

        # Check for existing account with this email
        existing = _find_identity_by_email(email)
        if existing and existing.has_usable_password():
            return Response(
                {
                    'error': 'An account with this email already exists. Please log in instead.',
                    'errors': {
                        'email': ['An account with this email already exists. Please log in instead.'],
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
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

                # Reuse existing PatientUser link if present (e.g. from a prior invitation
                # or signup at another org)
                existing_pu = PatientUser.objects.filter(identity=identity).first()
                if existing_pu:
                    person = existing_pu.person
                    # Ensure a PatientRecord exists for this person in the new org
                    PatientRecord.objects.get_or_create(
                        person=person,
                        organization=org,
                        defaults={'email': email},
                    )
                else:
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
                    PatientRecord.objects.create(person=person, email=email, organization=org)
                    PatientUser.objects.create(identity=identity, person=person)

                # Grant patient access to this org
                GroupAccess.objects.get_or_create(
                    identity=identity,
                    org=org,
                    defaults={'role': 'patient'},
                )
        except Exception:
            logger.exception('Unexpected error during patient signup for %s', email)
            return Response(
                {'error': 'Could not create your account. Please try again or contact support.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
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
