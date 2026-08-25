import logging

from django.utils import timezone
from rest_framework.permissions import BasePermission

from .providers.base import TokenClaims
from omop_core.services.access import has_org_admin_access

logger = logging.getLogger(__name__)

# Sentinel value set by ServiceTokenAuthentication when the HMAC check passes.
# Use is_service_token() rather than comparing this string directly.
SERVICE_TOKEN = "service-token"


def is_service_token(request) -> bool:
    """Return True when the request was authenticated as a trusted service token."""
    return request.auth == SERVICE_TOKEN


def get_request_org(request):
    """
    Return the Organization associated with the current OAuth2 token, or None.

    Returns None (no scoping) for:
      - staff users (can see all orgs)
      - session-authenticated requests (backward compat)
      - partner-auth requests (Firebase, SAML — no org scoping)
      - service clients not linked to any organization
    """
    if request.user and getattr(request.user, 'is_staff', False):
        return None
    token = getattr(request, 'auth', None)
    if token is None or isinstance(token, TokenClaims):
        return None
    try:
        return token.application.org_profile.organization
    except AttributeError:
        return None

_SAFE_METHODS = frozenset(('GET', 'HEAD', 'OPTIONS'))
_READ_SCOPES = frozenset(('patient/*.read', 'user/*.read'))
_WRITE_SCOPES = frozenset(('patient/*.write', 'user/*.write'))
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

      service-token         → full access (trusted backend service)
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

    def has_permission(self, request, view):
        token = request.auth

        # Service-to-service: trusted backend — full access.
        if token == SERVICE_TOKEN:
            return True  # hmac already validated in ServiceTokenAuthentication.authenticate()

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

        token_scopes = frozenset(token.scope.split())

        if request.method in _SAFE_METHODS:
            return bool(token_scopes & _READ_SCOPES)
        return bool(token_scopes & _WRITE_SCOPES)


class VocabReadPermission(ScopedTokenPermission):
    """Read permission for the vocabulary release + snapshot endpoints.

    Vocabulary/concept data is reference (system) data, not patient data, so an
    OAuth2 consumer may read it with a ``system/*.read`` scope in addition to the
    patient/user read scopes the base class accepts (#344). All views using this
    class are GET-only; service-token, staff, and partner/session auth are handled
    by the base class exactly as before — only the OAuth2 safe-method read-scope
    set is broadened here.
    """

    def has_permission(self, request, view):
        token = request.auth
        # OAuth2 bearer token on a safe method: accept the broadened read-scope
        # set. Everything else (service token, staff, partner/session, expired
        # tokens, non-safe methods) falls through to the base class unchanged.
        if (token is not None and not isinstance(token, TokenClaims)
                and hasattr(token, 'scope')
                and request.method in _SAFE_METHODS
                and timezone.now() < token.expires):
            return bool(frozenset(token.scope.split()) & _VOCAB_READ_SCOPES)
        return super().has_permission(request, view)


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
      PatientDocument, PatientTrialEnrollment, PatientSurveyResponse)
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
    All other methods defer to the standard ScopedTokenPermission rules.
    """

    def has_permission(self, request, view):
        if request.method == 'DELETE':
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


class IsStaffOrAnyOrgAdmin(BasePermission):
    """Allow staff users or any org admin (including via trust expansion)."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        if getattr(request.user, 'is_staff', False):
            return True

        return has_org_admin_access(request.user)
