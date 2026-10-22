"""Authorization and input validation shared by administrative file uploads."""
from rest_framework.authentication import SessionAuthentication
from rest_framework.settings import api_settings
from rest_framework.exceptions import PermissionDenied, ValidationError

from omop_core.services.access import get_admin_orgs, has_org_admin_access
from patient_portal.api.permissions import (
    ScopedTokenPermission, get_request_org, is_machine_request, is_service_token,
)
from patient_portal.api.providers.base import TokenClaims


BULK_UPLOAD_AUTHENTICATION = [
    SessionAuthentication if issubclass(backend, SessionAuthentication) else backend
    for backend in api_settings.DEFAULT_AUTHENTICATION_CLASSES
]


class BulkUploadPermission(ScopedTokenPermission):
    """Human uploads require admin authority; machine uploads retain SMART scopes."""

    def has_permission(self, request, view):
        if is_machine_request(request):
            return super().has_permission(request, view)
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if not (user.is_staff or has_org_admin_access(user)):
            return False
        if request.auth is None or isinstance(request.auth, TokenClaims):
            return True
        return super().has_permission(request, view)


def upload_organization(request):
    """Resolve the selected tenant without granting new authority to the caller."""
    machine_org = get_request_org(request)
    data = getattr(request, 'data', None)
    if data is None:
        data = getattr(request, 'POST', {})
    slug = data.get('organization')
    if is_machine_request(request):
        if slug and (machine_org is None or machine_org.slug != slug):
            raise PermissionDenied('The organization must match the service credential.')
        return machine_org
    orgs = get_admin_orgs(request.user).filter(is_active=True)
    if slug:
        org = orgs.filter(slug=slug).first()
        if org is None:
            raise PermissionDenied('You cannot upload to this organization.')
        return org
    if request.user.is_staff:
        return None
    choices = list(orgs[:2])
    if len(choices) != 1:
        raise ValidationError({'error': 'Select an organization for this upload.'})
    return choices[0]


def upload_actor_id(request):
    """Attribute machine imports to the credential, human imports to the user."""
    if is_service_token(request):
        return f"{request.user.issuer}|{request.user.sub}"
    if is_machine_request(request):
        application = getattr(getattr(request, 'auth', None), 'application', None)
        if application is not None:
            return f"urn:oauth-client|{application.client_id}"
        return f"{request.user.issuer}|{request.user.sub}"
    return str(getattr(request.user, 'pk', '') or '')


def ordered_bundle_entries(bundle):
    """Index patients first and reject ambiguous or orphaned clinical resources."""
    if not isinstance(bundle, dict) or bundle.get('resourceType') != 'Bundle':
        raise ValidationError('FHIR file must be a Bundle')
    entries = bundle.get('entry')
    if not isinstance(entries, list) or not entries:
        raise ValidationError('The bundle must contain Patient resources.')
    aliases = set()
    patient_ids = set()
    patients = []
    other = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('resource'), dict):
            raise ValidationError('Each bundle entry must contain a resource object.')
        resource = entry['resource']
        if resource.get('resourceType') != 'Patient':
            other.append(entry)
            continue
        pid = resource.get('id')
        if not isinstance(pid, str) or not pid.strip() or pid in patient_ids:
            raise ValidationError('Each Patient must have a unique, nonempty id.')
        refs = {pid, f'Patient/{pid}'}
        full_url = entry.get('fullUrl')
        if full_url:
            if not isinstance(full_url, str):
                raise ValidationError('Patient fullUrl must be a string.')
            refs.add(full_url)
            if full_url.startswith('urn:uuid:'):
                refs.add(full_url[len('urn:uuid:'):])
        if aliases & refs:
            raise ValidationError('Patient references must be unique within the bundle.')
        patient_ids.add(pid)
        aliases.update(refs)
        patients.append(entry)
    if not patients:
        raise ValidationError('The bundle must contain Patient resources.')
    subject_types = {'Condition', 'Observation', 'MedicationStatement', 'MedicationRequest',
                     'Procedure', 'DiagnosticReport', 'Encounter'}
    for entry in other:
        resource = entry['resource']
        kind = resource.get('resourceType')
        field = 'subject' if kind in subject_types else 'patient' if kind in {'Immunization', 'AllergyIntolerance'} else None
        if field is None:
            continue
        subject = resource.get(field)
        ref = subject.get('reference') if isinstance(subject, dict) else None
        if not isinstance(ref, str) or ref not in aliases:
            raise ValidationError(f'{kind}.{field} must reference a Patient in this bundle.')
    return patients + other
