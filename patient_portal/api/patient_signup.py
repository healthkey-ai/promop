"""
Patient signup — a trusted app built on PROMOP creates a patient account and
assigns them to an org, server-to-server (approach "A"). PHR-S FM PH.1.

Endpoint (registered in v1_urls.py):
  POST /api/v1/patients/signup/

Auth: a trusted caller only — an org-scoped OAuth client, a service token, or
staff. The org is taken from the caller's org-scoped token when present;
otherwise a staff/service caller must pass an `org` slug explicitly. A plain
patient identity cannot call this (ScopedTokenPermission denies non-staff POST).

Body (one identity anchor required):
  actor_iss + actor_sub   -> OIDC-linked account; the patient's later JWT login
                             (PartnerAuthentication) matches this Identity.
  email + password        -> local (email/password) account.
Optional: email, given_name, family_name.

Response 200/201: { "person_id": <int>, "created": <bool> }
"""
import logging

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.models import Organization, Person, PatientRecord
from omop_core.services.pk import next_pk
from patient_portal.models import Identity, PatientUser
from .permissions import ScopedTokenPermission, get_request_org, is_service_token

logger = logging.getLogger(__name__)


class PatientSignupView(APIView):
    permission_classes = [ScopedTokenPermission]

    def _resolve_org(self, request):
        """Return (org, error_response). The token's org wins; else staff/service may name one."""
        org = get_request_org(request)
        if org is not None:
            return org, None
        user = request.user
        is_privileged = (
            is_service_token(request)
            or getattr(user, 'is_superuser', False)
            or getattr(user, 'is_staff', False)
        )
        if not is_privileged:
            return None, Response(
                {'error': 'Caller is not permitted to assign an organization.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        slug = (request.data.get('org') or '').strip()
        if not slug:
            return None, Response(
                {'error': 'org is required (no organization is bound to this token).'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return get_object_or_404(Organization, slug=slug), None

    def _resolve_identity(self, request):
        """Build/find the Identity for the new account. Returns (identity, error_response)."""
        actor_iss = (request.data.get('actor_iss') or '').strip()
        actor_sub = (request.data.get('actor_sub') or '').strip()
        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password') or ''
        name = (request.data.get('name') or '').strip()

        if actor_iss and actor_sub:
            identity, created = Identity.objects.get_or_create(
                issuer=actor_iss, sub=actor_sub,
                defaults={'uid': f'{actor_iss}:{actor_sub}', 'email': email, 'name': name},
            )
            if created:
                identity.set_unusable_password()  # OIDC-authenticated; no local password
                identity.save(update_fields=['password'])
            return identity, None

        if email and password:
            from patient_portal.services import password_validation_errors
            pw_errors = password_validation_errors(password, email=email)
            if pw_errors:
                return None, Response(
                    {'error': ' '.join(pw_errors)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            identity = (
                Identity.objects.filter(issuer='urn:local', email__iexact=email).order_by('id').first()
            )
            if identity is None:
                identity = Identity.objects.create_user(email=email, password=password)
                from patient_portal.services import record_password
                record_password(identity)
            # If a local account already exists for this email we reuse it as-is
            # (idempotent signup) and intentionally do NOT reset its password here —
            # credential changes go through the account's own reset flow.
            return identity, None

        return None, Response(
            {'error': 'Provide actor_iss + actor_sub (OIDC) or email + password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def post(self, request):
        org, err = self._resolve_org(request)
        if err is not None:
            return err

        email = (request.data.get('email') or '').strip().lower()
        given_name = (request.data.get('given_name') or '').strip()
        family_name = (request.data.get('family_name') or '').strip()
        actor_iss = (request.data.get('actor_iss') or '').strip()
        actor_sub = (request.data.get('actor_sub') or '').strip()

        try:
            with transaction.atomic():
                identity, err = self._resolve_identity(request)
                if err is not None:
                    return err

                pu = (
                    PatientUser.objects.filter(identity=identity)
                    .select_related('person').first()
                )
                if pu is not None:
                    person = pu.person
                    created = False
                else:
                    person = Person.objects.create(
                        person_id=next_pk(Person, 'person_id'),
                        year_of_birth=1900,
                        gender_source_value='unknown',
                        race_source_value='unknown',
                        ethnicity_source_value='unknown',
                        given_name=given_name or None,
                        family_name=family_name or None,
                        actor_iss=actor_iss or None,
                        actor_sub=actor_sub or None,
                    )
                    PatientUser.objects.create(identity=identity, person=person)
                    created = True

                record, _ = PatientRecord.objects.get_or_create(person=person)
                update_fields = []
                if record.organization_id is None:
                    record.organization = org
                    update_fields.append('organization')
                if email and not record.email:
                    record.email = email
                    update_fields.append('email')
                if update_fields:
                    record.save(update_fields=update_fields)
        except IntegrityError:
            return Response(
                {'error': 'Could not create the account. The identity may already be linked.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {'person_id': person.person_id, 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
