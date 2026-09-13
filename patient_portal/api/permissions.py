import logging

from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import BasePermission

from .providers.base import TokenClaims
from patient_portal.service_tokens import ServiceCredential
from omop_core.services.access import has_org_admin_access

logger = logging.getLogger(__name__)

# Compatibility sentinel for existing integrations/tests. New authentication
# returns ServiceCredential; callers must use is_service_token().
SERVICE_TOKEN = "service-token"


def is_service_token(request) -> bool:
    """Return True when the request was authenticated as a trusted service token."""
    token = getattr(request, "auth", None)
    return isinstance(token, ServiceCredential) or token == SERVICE_TOKEN


def service_token_scopes(request):
    if isinstance(request.auth, ServiceCredential):
        return request.auth.scope
    return settings.SERVICE_AUTH_SCOPES


def is_machine_request(request):
    if is_service_token(request):
        return True
    from oauth2_provider.models import Application
    application = getattr(request.auth, "application", None)
    return (
        getattr(application, "authorization_grant_type", None)
        == Application.GRANT_CLIENT_CREDENTIALS
        or getattr(request.user, "issuer", None) == "urn:service"
    )


def reject_machine_actor_claims(request, actor_iss, actor_sub):
    """A service credential proves the service, never a user named in JSON."""
    if is_machine_request(request) and (actor_iss or actor_sub):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied(
            "User attribution requires end-user authentication. "
            "For service imports, omit actor_iss/actor_sub and supply person_id."
        )


def get_request_org(request):
    """
    Return the Organization associated with the current OAuth2 token, or None.

    Returns None (no scoping) for:
      - staff users (can see all orgs)
      - session-authenticated requests (backward compat)
      - partner-auth requests (Firebase, SAML — no org scoping)
      - service clients not linked to any organization
      - tokens from any application that is not a client_credentials app
        (see below)
    """
    if request.user and getattr(request.user, 'is_staff', False):
        return None
    token = getattr(request, 'auth', None)
    if token is None or isinstance(token, TokenClaims):
        return None
    # Org scoping is a machine-to-machine trust grant: several call sites treat
    # `get_request_org(...) is not None` as org-wide write authority, without
    # consulting the caller's GroupAccess role. ApplicationOrganization rows are
    # only ever created by patient_portal/management/commands/create_service_client.py,
    # which only builds GRANT_CLIENT_CREDENTIALS apps — so requiring that grant
    # type here costs the legitimate service clients nothing, and stops an
    # authorization_code (SMART, human-facing) app from ever handing a human
    # org-wide write trust if one were org-linked by hand or by a future command.
    # This fails CLOSED: a non-client-credentials token simply loses org scoping
    # and falls back to the stricter per-patient can_access_patient() /
    # can_write_patient() checks.
    #
    # Imported lazily, as every other non-test oauth2_provider.models import in
    # this codebase is: permissions.py is imported while the app registry is
    # still loading, so a module-level model import raises AppRegistryNotReady.
    from oauth2_provider.models import Application
    try:
        application = token.application
        if application.authorization_grant_type != Application.GRANT_CLIENT_CREDENTIALS:
            return None
        return application.org_profile.organization
    except AttributeError:
        return None

_SAFE_METHODS = frozenset(('GET', 'HEAD', 'OPTIONS'))
_READ_SCOPES = frozenset(('patient/*.read', 'user/*.read'))
_WRITE_SCOPES = frozenset(('patient/*.write', 'user/*.write'))
_ETL_WRITE_SCOPE = 'system/etl.write'
_ETL_WRITE_METHODS = frozenset(('POST', 'PUT', 'PATCH'))
# Vocabulary/concept data is reference (system) data, not patient data, so a
# service consumer may read it with a system/reference scope in addition to the
# patient/user read scopes. See healthkey-ai/promop#344.
_VOCAB_READ_SCOPES = _READ_SCOPES | frozenset(('system/*.read',))


class ScopedTokenPermission(BasePermission):
    """
    Enforces SMART on FHIR read/write scopes based on HTTP method.

    Safe methods   (GET, HEAD, OPTIONS) → patient/*.read  or user/*.read
    Unsafe methods (POST, PUT, PATCH, DELETE) → patient/*.write or user/*.write

    Role model for non-OAuth2 auth paths:

      service-token         → credential scopes (read-only by default)
      is_staff              → full access
      other authenticated   → safe methods + PATCH only
                              (read + self-edit; POST/DELETE denied)

    IMPORTANT — object-level ownership:
    This class grants or denies access at the view level only. It does NOT
    enforce per-patient ownership (e.g. preventing a patient from PATCHing
    another patient's record). Any view using this permission class for
    mutating endpoints MUST also enforce object ownership via one of:
      - _ProvenanceMixin.perform_update / perform_destroy (lab results views)
      - PatientRecordViewSet.partial_update (patient info views)
      - an explicit can_access_patient() check in the action method

    A new view that uses ScopedTokenPermission without one of these safeguards
    will allow any authenticated patient to mutate any other patient's data.
    """

    read_scopes = _READ_SCOPES

    def has_permission(self, request, view):
        token = request.auth

        if is_service_token(request):
            return self.has_scopes(request.method, service_token_scopes(request))

        # Partner-auth (Firebase, SAML) and session-auth: role-based enforcement.
        if token is None or isinstance(token, TokenClaims):
            if not (request.user and request.user.is_authenticated):
                return False
            # Staff users retain full access.
            if getattr(request.user, 'is_staff', False):
                return True
            # Regular authenticated users (patients): read + PATCH own data only.
            # POST (sync, bulk upload) and DELETE (visits, measurements, bulk) are denied.
            return request.method in _SAFE_METHODS or request.method == 'PATCH'

        # OAuth2 token: enforce SMART on FHIR scopes.
        if not hasattr(token, 'scope') or timezone.now() >= token.expires:
            return False

        return self.has_scopes(request.method, token.scope)

    def has_scopes(self, method, scope):
        token_scopes = frozenset(scope.split())
        if method in _SAFE_METHODS:
            return bool(token_scopes & self.read_scopes)
        return bool(token_scopes & _WRITE_SCOPES)


class VocabReadPermission(ScopedTokenPermission):
    """Read permission for the vocabulary release + snapshot endpoints.

    Vocabulary/concept data is reference (system) data, not patient data, so an
    OAuth2 or service consumer may read it with a ``system/*.read`` scope in
    addition to the patient/user read scopes the base class accepts (#344).
    All views using this
    class are GET-only; staff and partner/session auth are handled by the base
    class. Only the safe-method read-scope set is broadened here.
    """

    read_scopes = _VOCAB_READ_SCOPES


def _has_legacy_etl_write_grant(request) -> bool:
    """Accept the ETL capability only for the three non-delete write verbs.

    SMART ``patient/*.write`` is resource-wide and also authorizes destructive
    endpoints. The ETL capability is accepted only where an ETL-specific
    permission class has deliberately been installed.
    """
    return (
        is_service_token(request)
        and request.method.upper() in _ETL_WRITE_METHODS
        and _ETL_WRITE_SCOPE in service_token_scopes(request).split()
    )


class EtlWritePermission(ScopedTokenPermission):
    """Allow the legacy ETL capability on an explicitly approved endpoint."""

    def has_permission(self, request, view):
        return _has_legacy_etl_write_grant(request) or super().has_permission(
            request, view
        )


class LabSyncPermission(ScopedTokenPermission):
    """
    Permission for the lab result sync endpoint.

    Identical to ScopedTokenPermission except that an authenticated end
    user (Firebase/partner or session auth) is allowed to write, not just
    read/PATCH. Committing labs is a legitimate patient self-service write:
    SyncView resolves the target person from the authenticated identity and
    enforces can_access_patient() for on-behalf-of writes, and binds the
    actor to request.user for non-service callers — so a user can only write
    records they actually control.

    Service tokens and OAuth2 SMART scopes are handled exactly as in the
    base class.
    """

    def has_permission(self, request, view):
        token = request.auth
        # End-user auth (partner/session): allow authenticated users to write;
        # SyncView enforces per-person authorization. Service-token and OAuth2
        # clients fall through to the base role model.
        if token is None or isinstance(token, TokenClaims):
            return bool(request.user and request.user.is_authenticated)
        return super().has_permission(request, view)


_PATIENT_CRUD_METHODS = frozenset(('GET', 'HEAD', 'OPTIONS', 'POST', 'PATCH'))


class PatientCrudPermission(ScopedTokenPermission):
    """ScopedTokenPermission that allows GET/POST/PATCH for authenticated patients.

    Used by patient-facing viewsets and clinical row viewsets where patients
    need to create and update their own records. The viewset must enforce
    per-person authorization via _OmopFilterMixin/PatientSelfScopePermission or
    a create-time can_write_patient() check. Staff users retain full access.

    Service tokens and OAuth2 SMART scopes are handled exactly as in the
    base class.
    """

    def has_permission(self, request, view):
        token = request.auth
        if token is None or isinstance(token, TokenClaims):
            if not (request.user and request.user.is_authenticated):
                return False
            if getattr(request.user, 'is_staff', False):
                return True
            return request.method in _PATIENT_CRUD_METHODS
        return super().has_permission(request, view)


class GenomicsCrudPermission(ScopedTokenPermission):
    """Variant CRUD; actions must enforce can_access/write_patient per person."""

    def has_permission(self, request, view):
        if not is_service_token(request) and (request.auth is None or isinstance(request.auth, TokenClaims)):
            return bool(request.user and request.user.is_authenticated)
        return super().has_permission(request, view)


class EtlPatientCrudPermission(PatientCrudPermission):
    """Patient CRUD rules plus the narrowly placed legacy ETL capability."""

    def has_permission(self, request, view):
        return _has_legacy_etl_write_grant(request) or super().has_permission(
            request, view
        )


class IsStaffPermission(BasePermission):
    """Allow access only to staff users (is_staff=True)."""

    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            getattr(request.user, 'is_staff', False)
        )


def _resolve_person_id(obj):
    """Extract person_id from any OMOP model instance.

    Returns the person_id (int) or None if it cannot be determined.

    Handles three patterns:
    - Direct FK: obj.person_id (covers Person, PatientRecord, ConditionOccurrence,
      DrugExposure, Measurement, Observation, ProcedureOccurrence, Episode,
      PatientDocument, PatientTrialEnrollment)
    - PatientConsent/PatientMessage: has patient_user_id FK. Resolves via
      PatientUser.objects.values_list('person_id', ...).
    - EpisodeEvent: has a bare episode_id (BigIntegerField, not a FK). Resolves
      via Episode.objects.values_list('person_id', ...).
    """
    # Pattern 1: direct person_id attribute (covers most OMOP models)
    pid = getattr(obj, 'person_id', None)
    if pid is not None:
        return pid

    # Pattern 2: PatientConsent — resolve via PatientUser.person_id
    patient_user_id = getattr(obj, 'patient_user_id', None)
    if patient_user_id is not None:
        from patient_portal.models import PatientUser
        try:
            return PatientUser.objects.values_list('person_id', flat=True).get(pk=patient_user_id)
        except PatientUser.DoesNotExist:
            return None

    # Pattern 3: EpisodeEvent — resolve via Episode table
    episode_id = getattr(obj, 'episode_id', None)
    if episode_id is not None:
        from omop_oncology.models import Episode
        try:
            return Episode.objects.values_list('person_id', flat=True).get(
                episode_id=episode_id
            )
        except Episode.DoesNotExist:
            return None

    return None


class PatientSelfScopePermission(BasePermission):
    """Object-level permission: patients may only access their own data.

    Belt-and-suspenders safety net on top of queryset filtering. DRF calls
    ``has_object_permission`` on retrieve/update/destroy — not on list (which
    relies on queryset scoping in ``_OmopFilterMixin``/viewset ``get_queryset``).

    Bypass rules (allow access regardless of object ownership):
    - Service tokens (trusted backend)
    - Staff users
    - Non-patient identities (no PatientUser link, or has provider GroupAccess)
    """

    def has_permission(self, request, view):
        return True  # View-level gating is ScopedTokenPermission's job

    def has_object_permission(self, request, view, obj):
        if is_service_token(request):
            return True

        from patient_portal.services import patient_person_for
        patient_person = patient_person_for(request.user)
        if patient_person is None:
            # Not a patient (staff, provider, or unauthenticated).
            return True

        obj_person_id = _resolve_person_id(obj)
        if obj_person_id is None:
            # Cannot determine ownership — fail closed.
            logger.warning(
                'PatientSelfScopePermission: cannot resolve person_id for %s pk=%s',
                type(obj).__name__, getattr(obj, 'pk', '?'),
            )
            return False

        return obj_person_id == patient_person.person_id


class PatientDeletePermission(ScopedTokenPermission):
    """ScopedTokenPermission that also allows DELETE for patient account deletion.

    Used on the ``me`` action where patients need to delete their own account.
    The exception applies to session/partner auth; service and OAuth2 tokens
    must still carry a write scope. Other methods defer to the base rules.
    """

    def has_permission(self, request, view):
        if request.method == 'DELETE' and (
                request.auth is None or isinstance(request.auth, TokenClaims)):
            return bool(request.user and request.user.is_authenticated)
        return super().has_permission(request, view)


class IsStaffOrOrgAdmin(BasePermission):
    """Allow staff users, or org_admin users for the org identified by view.kwargs['slug']."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        if getattr(request.user, 'is_staff', False):
            return True

        slug = view.kwargs.get('slug')
        return has_org_admin_access(request.user, slug)
