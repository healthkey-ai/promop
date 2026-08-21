from typing import Any, Callable, ContextManager

from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from patient_portal.models import Identity
from django.contrib.auth import logout, login, authenticate
from django.db import IntegrityError, models, transaction
from django.db.models import Q, F
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from omop_core.models import (
    Organization,
    Person, PatientRecord, Concept, ConceptClass, Domain, ProvenanceRecord, Vocabulary,
    ConditionOccurrence, DrugExposure, Measurement, MeasurementOwnership,
    Observation, ProcedureOccurrence, VisitOccurrence, VisitDetail, Location, Death,
    PatientDocument, PatientTrialEnrollment, PatientGroupMembership, Survey, PatientSurveyResponse,
    Relationship, ConceptRelationship, ConceptAncestor, ConceptSynonym,
    # Controlled vocabulary lookup models
    Ethnicity, StemCellTransplant, SctEligibility, HistologicType, EstrogenReceptorStatus,
    ProgesteroneReceptorStatus, Her2Status, HrStatus, HrdStatus,
    MutationOrigin, MutationGene, MutationInterpretation, MutationCode,
    TumorStage, NodesStage, DistantMetastasisStage, StagingModality,
    ToxicityGrade, Language, LanguageSkillLevel, BinetStage, ProteinExpression,
    RichterTransformation, TumorBurden, MorphologicVariant, DiseaseActivity,
    PreExistingConditionCategory,
    Disease, CancerStage, KarnofskyScore, EcogStatus, PeripheralNeuropathyGrade,
    InfectionStatus, DiseaseProgression, MeasurableDisease, GelfCriteria,
    FlipIScore, FollicularLymphomaGrade, PostTransformationOutcome,
    BreastCancerFirstLineTherapy, BreastCancerSecondLineTherapy, BreastCancerLaterLineTherapy,
    MyelomaType, WearableUpload,
    PERSON_YEAR_PLACEHOLDERS,
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_record_service import (
    FHIR_CONDITION_STAGE_SOURCE_VALUE,
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    refresh_patient_record,
)
from omop_core.services.patient_cleanup import delete_omop_clinical_rows
from omop_core.services.lot_inference_service import infer_lot_for_person
from omop_core.services.episode_service import upsert_therapy_line_episode
from omop_core.services.mappings import CONCEPT_GENERIC_LAB, get_gender_concept
from omop_core.services.demographics import resolve_concept as resolve_demographic_concept
from omop_core.services.pk import next_pk, next_pk_batch
from omop_core.signals import suppress_patient_record_refresh
from omop_core.services.rxnav_service import resolve_drug as _rxnav_resolve_drug
from omop_core.services.regimen_resolution import (
    get_or_create_quarantine_drug,
    get_or_create_quarantine_observation,
    get_or_create_quarantine_procedure,
    get_or_create_quarantine_regimen,
    match_hemonc_regimen_by_name,
    validate_hemonc_regimen,
)
from omop_core.services.concept_cache import concept_by_id as _cc_by_id, concept_by_loinc as _cc_by_loinc, concept_by_name_ilike as _cc_by_name, concept_by_vocab as _cc_by_vocab
from omop_core.services.access import get_visible_orgs, build_trusting_map, get_admin_orgs
from datetime import date as _date, datetime, timedelta
from django.utils.timezone import localdate, make_aware, is_naive
import datetime as _dt
import csv
import hashlib
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from io import StringIO
from .permissions import ScopedTokenPermission, VocabReadPermission, PatientCrudPermission, PatientSelfScopePermission, PatientDeletePermission, get_request_org, is_service_token
from .providers.base import TokenClaims
from .serializers import (
    UserSerializer, PatientRecordSerializer, PatientListSerializer, ProvenanceRecordSerializer,
    ConditionOccurrenceSerializer, DrugExposureSerializer, MeasurementSerializer,
    ObservationSerializer, ProcedureOccurrenceSerializer,
    EpisodeSerializer, EpisodeEventSerializer,
    PatientDocumentSerializer, PatientTrialEnrollmentSerializer,
    SurveySerializer, PatientSurveyResponseSerializer,
    PatientConsentSerializer,
    PatientMessageSerializer,
    ImmunizationSerializer, AllergySerializer,
)
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


class PatientRecordPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


class ClinicalOmopPagination(PageNumberPagination):
    """Opt-in clinical list pagination.

    ``?page_size=N`` and ``?limit=N`` both return DRF's paginated envelope with
    a default size of 100 and a hard cap of 1000. Omitting pagination params
    preserves the legacy bare-array response while clients migrate.
    """
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000

    def get_page_size(self, request):
        limit = request.query_params.get('limit')
        if limit is not None and self.page_size_query_param not in request.query_params:
            try:
                size = int(limit)
            except (TypeError, ValueError):
                return self.page_size
            if size <= 0:
                return self.page_size
            return min(size, self.max_page_size)
        return super().get_page_size(request)


def _serialize_omop_row(obj, include=None):
    """Serialize a model instance to a dict.

    *include* restricts to the named fields when provided.  FK concept fields
    use ``field.attname`` (e.g. ``drug_concept_id``) so the raw integer is
    emitted instead of the related object.
    """
    row = {}
    for field in obj._meta.fields:
        name = field.name
        if include is not None and name not in include:
            continue
        value = getattr(obj, field.attname)
        if isinstance(value, Decimal):
            value = float(value)
        row[name] = value
    return row


# -- OMOP tab: per-table column include lists --------------------------------
# Only columns that are typically populated (>10 %) are included.  ``person``
# is excluded from every clinical table (implied by page context).

_OMOP_COLUMNS = {
    'person': [
        'person_id', 'gender_concept', 'gender_source_value',
        'year_of_birth', 'month_of_birth', 'day_of_birth',
        'race_source_value', 'ethnicity_source_value',
        'given_name', 'family_name', 'email', 'location',
    ],
    'condition_occurrences': [
        'condition_occurrence_id', 'condition_concept',
        'condition_start_date', 'condition_start_datetime',
        'condition_type_concept', 'condition_status_concept',
        'condition_source_value', 'condition_status_source_value',
    ],
    'drug_exposures': [
        'drug_exposure_id', 'drug_concept',
        'drug_exposure_start_date', 'drug_exposure_start_datetime',
        'drug_exposure_end_date', 'drug_exposure_end_datetime',
        'drug_type_concept', 'lot_number', 'drug_source_value',
    ],
    'measurements': [
        'measurement_id', 'measurement_concept',
        'measurement_datetime',
        'measurement_type_concept',
        'value_as_number', 'value_as_string',
        'measurement_source_value', 'unit_source_value',
    ],
    'observations': [
        'observation_id', 'observation_concept',
        'observation_date', 'observation_datetime',
        'observation_type_concept',
        'value_as_number', 'value_as_string',
        'observation_source_value',
    ],
    'procedure_occurrences': [
        'procedure_occurrence_id', 'procedure_concept',
        'procedure_date', 'procedure_datetime',
        'procedure_type_concept',
        'procedure_source_value', 'procedure_source_concept',
    ],
    'episodes': [
        'episode_id', 'episode_concept',
        'episode_start_date', 'episode_end_date',
        'episode_number', 'episode_object_concept',
        'episode_type_concept',
        'episode_source_value', 'episode_source_concept',
    ],
    'episode_events': [
        'episode_id', 'event_id', 'episode_event_field_concept',
    ],
    'visit_occurrences': [
        'visit_occurrence_id', 'visit_concept',
        'visit_start_date', 'visit_start_datetime',
        'visit_end_date', 'visit_end_datetime',
        'visit_type_concept', 'visit_source_value',
    ],
    'visit_details': [
        'visit_detail_id', 'visit_detail_concept',
        'visit_detail_start_date', 'visit_detail_start_datetime',
        'visit_detail_end_date', 'visit_detail_end_datetime',
        'visit_detail_type_concept', 'visit_detail_source_value',
        'visit_occurrence',
    ],
    'death': [
        'death_date', 'death_datetime',
        'death_type_concept', 'cause_source_value',
    ],
}


def _resolve_concept_names(tables_data):
    """Batch-resolve concept FK IDs → concept_name across all tables.

    For every column whose name ends with ``_concept`` (the Django field name
    for a FK to Concept), inject a companion ``<col>_name`` column right after
    the ID with the concept's human-readable name.
    """
    # 1. Collect every concept ID referenced in all rows.
    concept_ids = set()
    for _key, rows in tables_data:
        for row in rows:
            for col, val in row.items():
                if col.endswith('_concept') and isinstance(val, int) and val != 0:
                    concept_ids.add(val)

    if not concept_ids:
        return

    # 2. Single batch query.
    names = dict(
        Concept.objects.filter(concept_id__in=concept_ids)
        .values_list('concept_id', 'concept_name')
    )

    # 3. Inject *_name columns.
    for _key, rows in tables_data:
        for row in rows:
            additions = {}
            for col, val in list(row.items()):
                if col.endswith('_concept') and isinstance(val, int):
                    additions[col + '_name'] = names.get(val) if val != 0 else None
            # Insert name columns right after their ID columns.
            if additions:
                new_row = {}
                for col, val in row.items():
                    new_row[col] = val
                    name_col = col + '_name'
                    if name_col in additions:
                        new_row[name_col] = additions[name_col]
                row.clear()
                row.update(new_row)


# ---------------------------------------------------------------------------
# SMART on FHIR discovery endpoint
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([AllowAny])
def smart_configuration(request):
    """
    HL7 SMART on FHIR well-known configuration endpoint.
    Advertises authorization / token endpoints and supported scopes.
    """
    from patient_portal.api.fhir.integrity import SUPPORTED_FHIR_VERSION
    base = request.build_absolute_uri('/').rstrip('/')
    oidc_issuer = getattr(settings, 'OAUTH2_PROVIDER', {}).get('OIDC_ISS_ENDPOINT', '') or base
    return Response({
        'issuer': oidc_issuer,
        # Declared interchange version (TI.5.2#01). R4 only — no multi-version
        # transforms; a request for another version is rejected with HTTP 406.
        'fhirVersion': SUPPORTED_FHIR_VERSION,
        'fhir_versions_supported': [SUPPORTED_FHIR_VERSION],
        'authorization_endpoint': f'{base}/o/authorize/',
        'token_endpoint': f'{base}/o/token/',
        'token_endpoint_auth_methods_supported': ['client_secret_basic', 'client_secret_post', 'none'],
        'revocation_endpoint': f'{base}/o/revoke_token/',
        'introspection_endpoint': f'{base}/o/introspect/',
        'scopes_supported': list(settings.OAUTH2_PROVIDER.get('SCOPES', {}).keys()),
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'client_credentials', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'capabilities': [
            'launch-standalone',
            'client-public',
            'sso-openid-connect',
            'context-standalone-patient',
            'permission-patient',
            'permission-user',
            'authorize-post',
        ],
    })


@method_decorator(csrf_exempt, name='dispatch')
class CurrentUserViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _ensure_patient_identity_resolved(self, user):
        """Auto-provision first-login patient identities for /api/v1/user/."""
        if not (user and user.is_authenticated):
            return
        if getattr(user, 'is_staff', False):
            return

        from omop_core.models import GroupAccess
        from patient_portal.models import PatientUser
        if PatientUser.objects.filter(identity=user).exists():
            return

        has_active_clinical_grant = GroupAccess.objects.filter(
            identity=user,
            role__in=['org_admin', 'doctor', 'analyst'],
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
        ).exists()
        if has_active_clinical_grant:
            return

        from patient_portal.services import resolve_or_create_person
        resolve_or_create_person(user)
    
    def list(self, request):
        """Just return the logged-in user info - they don't need to be a patient"""
        if not request.user.is_authenticated:
            return Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
        
        self._ensure_patient_identity_resolved(request.user)
        user_serializer = UserSerializer(request.user)
        return Response({
            'user': user_serializer.data
        })

def _extract_provenance(request):
    """Return (source, source_user_id, modification_reason) from headers or POST body.

    A bulk POST body is a JSON *list*, which has no top-level provenance fields and
    no ``.get()``. For those requests the headers are the only channel — per-row
    provenance keys are deliberately out of scope, so a list body reads headers only.
    """
    body = request.data if isinstance(request.data, dict) else {}
    source = (
        body.get('source')
        or request.META.get('HTTP_X_PROVENANCE_SOURCE')
    )
    source_user_id = (
        body.get('source_user_id')
        or request.META.get('HTTP_X_PROVENANCE_USER_ID', '')
    )
    modification_reason = body.get('modification_reason')
    return source, source_user_id, modification_reason


def _echoed_unchanged_fields(patient_info, patch_data):
    """Keys whose submitted value already equals what GET renders for this record.

    The React patient editor holds the whole GET response as its edit buffer and
    PATCHes all of it on every autosave, so each save carries ~270 OMOP-mapped and
    computed fields the user never touched, each one bearing the value the server
    itself just rendered. Judging the read-only guards on *presence* therefore
    rejects every ordinary edit: change one writable field and the untouched
    derived fields riding along trip a 405.

    Judging on *change* keeps the contract intact — an attempt to move a derived
    value is still refused — while letting an echo through as the no-op it is.
    Comparison is against the serialized representation rather than the model
    attributes, because that is the exact form the client received and is sending
    back; comparing to model attributes would read '12.5' != Decimal('12.5') and
    call an untouched field an edit.
    """
    if not patch_data:
        return set()
    rendered = PatientRecordSerializer(patient_info).data
    return {k for k, v in patch_data.items() if k in rendered and rendered[k] == v}


def _rendered_patient_name(person):
    """The display name PatientRecordSerializer.get_patient_name would return.

    Kept in step with the serializer deliberately: _apply_patient_name compares
    against it to recognise the server's own value coming back.
    """
    full_name = f"{person.given_name or ''} {person.family_name or ''}".strip()
    return full_name or f"Patient {person.person_id}"


def _pop_patient_name(data):
    """Split patient_name out of a PATCH body, returning (name_or_None, rest).

    patient_name is a SerializerMethodField over Person.given_name/family_name —
    an OMOP column, not a PatientRecord one — so it can only be applied by hand.
    Left in the body it is not merely ignored: it is not projection-owned, so it
    trips the writable-fields check and 405s the whole request.
    """
    if 'patient_name' not in data:
        return None, data
    return data['patient_name'], {k: v for k, v in data.items() if k != 'patient_name'}


def _apply_patient_name(person, name):
    """Write a display name onto the OMOP Person row. Returns True if it changed.

    The name lives on Person, never on PatientRecord: the projection is a derived
    read model and its patient_name is rendered from these two columns.

    No-ops on the value the serializer would already render. The React client
    PATCHes back the whole GET response, so patient_name arrives on every autosave
    carrying the server's own value. Writing that back is pointless for a named
    person and destructive for an unnamed one, because the rendered value is then
    the synthesised "Patient {id}", which would split into given_name='Patient',
    family_name='<id>'.
    """
    if name is None:
        return False
    name = str(name).strip()
    if not name or name == _rendered_patient_name(person):
        return False
    parts = name.split(None, 1)
    person.given_name = parts[0]
    person.family_name = parts[1] if len(parts) > 1 else ''
    person.save(update_fields=['given_name', 'family_name'])
    return True


def _record_provenance(record, source, source_user_id, target_patient_id=None, modification_reason=None, organization=None):
    """Create or update a ProvenanceRecord pointing at any model instance."""
    ProvenanceRecord.objects.update_or_create(
        content_type=ContentType.objects.get_for_model(record),
        object_id=record.pk,
        source_user_id=source_user_id or '',
        source=source,
        defaults={
            'target_patient_id': target_patient_id,
            'modification_reason': modification_reason,
            'organization': organization,
        },
    )


def _changed_fields(patient_record, previous_values, prev_val):
    """Fields whose stored value actually moved during this PATCH.

    ``previous_values`` was captured before ``serializer.save()`` and already
    excludes read-only fields and anything not on the model, so comparing it to
    the saved instance yields exactly the fields the user changed.

    The distinction matters because the React client autosaves by PATCHing the
    entire record back (``{...editedInfo, [field]: value}``), so the request body
    is not a statement of intent — every key is present whether or not it moved.
    Callers that treat the body as the change set attribute the whole record to
    the user on every keystroke.
    """
    return {
        field for field, old in previous_values.items()
        if prev_val(patient_record, field) != old
    }


def _write_record_revisions(patient_record, previous_values, request):
    """Persist field-level revision-history rows for a PatientRecord update.

    PHR-S FM TI.1.2#04. ``previous_values`` maps field name -> value BEFORE the
    update (as captured by the PATCH path). We compare against the freshly saved
    instance and write one RecordRevision per field whose value actually
    changed, so a record's contents can be reconstructed over time.

    Returns the list of created RecordRevision instances.
    """
    from omop_core.models import RecordRevision

    def _norm(v):
        # Normalize to a stable string form for storage + comparison.
        return None if v is None else str(v)

    user = getattr(request, 'user', None)
    changed_by = (
        str(user.pk) if getattr(user, 'is_authenticated', False) and user.pk is not None
        else 'system'
    )

    patient_record.refresh_from_db()
    rows = []
    for field, old in previous_values.items():
        # Resolve the new value the same way the PATCH path captured the old one
        # (FK fields are stored/compared via their {field}_id attribute).
        fk_id = f'{field}_id'
        new = getattr(patient_record, fk_id, None) if hasattr(patient_record, fk_id) else getattr(patient_record, field, None)
        old_s, new_s = _norm(old), _norm(new)
        if old_s == new_s:
            continue
        rows.append(RecordRevision(
            patient_record=patient_record,
            changed_by=changed_by,
            field=field,
            old_value=old_s,
            new_value=new_s,
        ))
    if rows:
        RecordRevision.objects.bulk_create(rows)
    return rows


def _delete_omop_clinical_rows(person):
    """Delete all OMOP clinical rows for a person, in FK dependency order.

    Must be called inside a transaction.atomic() block.
    Does NOT delete PatientRecord, PatientUser/Identity, or Person — callers
    handle those after this returns.
    """
    delete_omop_clinical_rows(person)


@method_decorator(csrf_exempt, name='dispatch')
class PatientRecordViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PatientRecordSerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    pagination_class = PatientRecordPagination

    DATE_FILTERS = {
        '7d': timedelta(days=7),
        '30d': timedelta(days=30),
        '90d': timedelta(days=90),
    }
    
    def get_queryset(self):
        qs = PatientRecord.objects.all().select_related('person', 'organization')
        # Trusted backend (service-token): full visibility across all patients.
        if is_service_token(self.request):
            return qs
        org = get_request_org(self.request)
        if org is not None:
            qs = qs.filter(organization=org)
        elif not (self.request.user and
            getattr(self.request.user, 'is_staff', False)
        ):
            # Session / partner-auth users: scope to only the patients they can
            # access — their own record (PatientUser) and any patients in their
            # professional groups (GroupAccess). Doctors/admins with
            # group access see their whole panel; is_staff bypasses this entirely.
            from patient_portal.models import PatientUser
            from omop_core.models import PatientGroupMembership, GroupAccess
            from django.utils import timezone
            from django.db.models import Q

            accessible_pids = set()

            # Self-access
            try:
                accessible_pids.add(
                    PatientUser.objects.get(identity=self.request.user).person_id
                )
            except PatientUser.DoesNotExist:
                pass

            now = timezone.now()
            active_grants = GroupAccess.objects.filter(
                identity=self.request.user,
            ).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now),
            )

            # Org-admin access includes trust-derived admin orgs.
            admin_org_ids = list(get_admin_orgs(self.request.user).values_list('id', flat=True))

            # Group grants: see patients in those groups
            actor_group_ids = list(
                active_grants.filter(group__isnull=False).values_list('group_id', flat=True)
            )
            if actor_group_ids:
                group_pids = PatientGroupMembership.objects.filter(
                    group_id__in=actor_group_ids
                ).values_list('person_id', flat=True)
                accessible_pids.update(group_pids)

            if not accessible_pids and not admin_org_ids:
                return qs.none()

            if admin_org_ids and accessible_pids:
                qs = qs.filter(
                    Q(organization_id__in=admin_org_ids) | Q(person_id__in=accessible_pids)
                )
            elif admin_org_ids:
                qs = qs.filter(organization_id__in=admin_org_ids)
            else:
                qs = qs.filter(person_id__in=accessible_pids)
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return PatientListSerializer
        return PatientRecordSerializer

    def _normalize_all_param(self, value):
        value = (value or '').strip()
        if not value or value.lower() == 'all':
            return None
        return value

    def _apply_patient_list_filters(self, queryset):
        params = self.request.query_params

        org_slug = self._normalize_all_param(params.get('org'))
        if org_slug:
            if org_slug == '__unassigned__':
                queryset = queryset.filter(organization__isnull=True)
            else:
                queryset = queryset.filter(organization__slug=org_slug)

        disease = self._normalize_all_param(params.get('disease'))
        if disease:
            queryset = queryset.filter(disease__iexact=disease)

        stage = self._normalize_all_param(params.get('stage'))
        if stage:
            stage = stage.upper()
            if stage not in {'I', 'II', 'III', 'IV'}:
                return queryset.none()
            queryset = queryset.filter(
                Q(stage__iexact=stage) |
                Q(stage__iexact=f'Stage {stage}') |
                Q(stage__iregex=rf'(^|[^A-Za-z0-9])stage\s+{stage}(?![IVX])[A-Z]*([^A-Za-z0-9]|$)')
            )

        date_filter = self._normalize_all_param(params.get('date'))
        if date_filter in self.DATE_FILTERS:
            queryset = queryset.filter(updated_at__gte=timezone.now() - self.DATE_FILTERS[date_filter])
        elif date_filter == 'this_year':
            start_of_year = timezone.make_aware(
                datetime.combine(localdate().replace(month=1, day=1), datetime.min.time())
            )
            queryset = queryset.filter(updated_at__gte=start_of_year)

        search = self._normalize_all_param(params.get('search'))
        if search:
            name_query = (
                Q(person__given_name__icontains=search) |
                Q(person__family_name__icontains=search) |
                Q(email__icontains=search)
            )
            try:
                name_query |= Q(person__person_id=int(search))
            except (TypeError, ValueError):
                pass
            queryset = queryset.filter(name_query)

        return queryset

    def _build_filter_options(self, queryset):
        org_rows = (
            queryset
            .exclude(organization__isnull=True)
            .values('organization__slug', 'organization__name')
            .distinct()
            .order_by('organization__name')
        )
        org_options = [
            {'value': row['organization__slug'], 'label': row['organization__name']}
            for row in org_rows
            if row['organization__slug'] and row['organization__name']
        ]
        if queryset.filter(organization__isnull=True).exists():
            org_options.append({'value': '__unassigned__', 'label': 'Unassigned'})

        diseases = (
            queryset
            .exclude(disease__isnull=True)
            .exclude(disease='')
            .values_list('disease', flat=True)
            .distinct()
            .order_by('disease')
        )

        return {
            'orgs': org_options,
            'diseases': list(diseases),
            'stages': ['I', 'II', 'III', 'IV'],
        }
    
    def list(self, request):
        """List all patients - accessible to authenticated users"""
        base_queryset = self.get_queryset()
        queryset = self._apply_patient_list_filters(base_queryset).order_by('-updated_at', '-created_at')

        if 'page' in request.query_params or 'page_size' in request.query_params:
            page = self.paginate_queryset(queryset)
            serializer = PatientListSerializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            try:
                page_num = int(request.query_params.get('page', 1))
            except (TypeError, ValueError):
                page_num = 1
            if page_num == 1:
                response.data['filter_options'] = self._build_filter_options(base_queryset)
            return response

        serializer = PatientListSerializer(queryset[:500], many=True)
        return Response(serializer.data)

    def retrieve(self, request, pk=None):
        """Get detailed patient info for a specific person"""
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        # Get the Identity associated with this person (not the logged-in user)
        from patient_portal.models import PatientUser
        try:
            patient_user = PatientUser.objects.get(person=person)
            user_serializer = UserSerializer(patient_user.identity)
            user_data = user_serializer.data
        except PatientUser.DoesNotExist:
            user_data = None

        # Pass request in context so PH.1.2#05 demographic redaction can
        # identify whether the reader is the account holder.
        patient_serializer = PatientRecordSerializer(patient_info, context={'request': request})

        return Response({
            'patient_info': patient_serializer.data,  # legacy wire format — preserved for frontend/federation host compatibility
            'user': user_data
        })

    def partial_update(self, request, pk=None):
        """Patch the PatientRecord compatibility surface.

        A mapped clinical PatientRecord field is output from an OMOP fact. It
        cannot safely supply the fact's concept, time, unit, or provenance, so
        it returns 405 rather than recreating the retired PatientRecord-to-OMOP
        write-through path. Write the appropriate OMOP resource (or ingest
        FHIR) and let refresh_patient_record rebuild this read model. See
        docs/omop_to_patientrecord.md for the field-to-source mapping.
        """
        try:
            person = Person.objects.get(person_id=pk)
            patient_info = PatientRecord.objects.get(person=person)
        except Person.DoesNotExist:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientRecord.DoesNotExist:
            return Response({'error': 'Patient information not found'}, status=status.HTTP_404_NOT_FOUND)

        org = get_request_org(request)
        if org is not None:
            if patient_info.organization != org:
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        elif not getattr(request.user, 'is_staff', False):
            from omop_core.authorization import can_access_patient, can_write_patient
            if not can_access_patient(request.user, person.person_id):
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
            if not can_write_patient(request.user, person.person_id):
                return Response(
                    {'error': 'Analysts have read-only access. Contact a doctor or org admin to update patient data.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # patient_name targets Person, not PatientRecord, so it is handled by hand
        # here exactly as /patient-info/me/ handles it — through the same pair of
        # helpers, so the two routes cannot drift apart again. This is the route
        # the provider UI PATCHes, and leaving the key in the body would 405 the
        # request below as a non-projection-owned field.
        patient_name, patch_data = _pop_patient_name(request.data)

        echoed = _echoed_unchanged_fields(patient_info, patch_data)
        mapped_fields = sorted((set(patch_data) & PATIENT_RECORD_OMOP_MAPPED_FIELDS) - echoed)
        if mapped_fields:
            return Response(
                {
                    'detail': (
                        'OMOP-mapped PatientRecord fields are read-only. Write a complete '
                        'clinical fact to the appropriate OMOP resource, then rederive the record.'
                    ),
                    'fields': mapped_fields,
                },
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        serializer = PatientRecordSerializer(patient_info, data=patch_data, partial=True)
        serializer.is_valid(raise_exception=True)

        # DRF intentionally discards serializer read-only fields. Surface those
        # attempts instead of returning success for a no-op, so ownership
        # boundaries are visible to API consumers — but only when the value is
        # actually being moved, not when the client echoes back what it read.
        writable_fields = {
            name for name, field in serializer.fields.items() if not field.read_only
        }
        unsupported_fields = sorted(set(patch_data) - writable_fields - echoed)
        if unsupported_fields:
            return Response(
                {'detail': 'Only projection-owned PatientRecord fields are writable.', 'fields': unsupported_fields},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        def previous_value(obj, field):
            fk_id = f'{field}_id'
            return getattr(obj, fk_id, None) if hasattr(obj, fk_id) else getattr(obj, field, None)

        previous_values = {
            field: previous_value(patient_info, field)
            for field in patch_data
            if hasattr(patient_info, field)
        }
        with transaction.atomic():
            _apply_patient_name(person, patient_name)
            serializer.save()
            _write_record_revisions(patient_info, previous_values, request)

        return Response({**serializer.data, 'previous_values': previous_values})

    @action(detail=True, methods=['get'], permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def provenance(self, request, pk=None):
        """GET /api/patient-info/{person_id}/provenance/ — full provenance history for a patient."""
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        from django.db.models import Q
        # Build a single query for all provenance records across PatientRecord + OMOP tables
        q = Q(
            content_type=ContentType.objects.get_for_model(PatientRecord),
            object_id=patient_info.pk,
        )
        for model_cls in [Measurement, ConditionOccurrence, DrugExposure, ProcedureOccurrence]:
            omop_ids = list(model_cls.objects.filter(person_id=person.person_id).values_list('pk', flat=True))
            if omop_ids:
                q |= Q(
                    content_type=ContentType.objects.get_for_model(model_cls),
                    object_id__in=omop_ids,
                )
        records = ProvenanceRecord.objects.filter(q).select_related('content_type').order_by('-created_at')
        return Response(ProvenanceRecordSerializer(records, many=True).data)

    @action(detail=True, methods=['get'], permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def revisions(self, request, pk=None):
        """GET /api/v1/patient-records/{person_id}/revisions/ — field-level
        revision history for a patient's record (PHR-S FM TI.1.2#04)."""
        from omop_core.models import RecordRevision
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        revisions = RecordRevision.objects.filter(
            patient_record=patient_info,
        ).order_by('-changed_at', 'field')
        data = [
            {
                'id': r.id,
                'field': r.field,
                'old_value': r.old_value,
                'new_value': r.new_value,
                'changed_by': r.changed_by,
                'changed_at': r.changed_at,
            }
            for r in revisions
        ]
        return Response(data)

    @action(
        detail=True,
        methods=['get'],
        url_path=r'field-provenance/(?P<field_name>[a-z_][a-z0-9_]*)',
        url_name='field-provenance',
        permission_classes=[ScopedTokenPermission, PatientSelfScopePermission],
    )
    def field_provenance(self, request, pk=None, field_name=None):
        """GET /api/v1/patient-records/{person_id}/field-provenance/{field_name}/

        Return the OMOP source rows that produced a specific PatientRecord field.
        """
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        from omop_core.services.provenance_service import get_field_provenance

        result = get_field_provenance(person, field_name)
        if result is None:
            return Response(
                {'error': f'Unknown field: {field_name}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(result)

    @action(
        detail=True,
        methods=['get'],
        url_path='field-provenance',
        url_name='field-provenance-bulk',
        permission_classes=[ScopedTokenPermission, PatientSelfScopePermission],
    )
    def field_provenance_bulk(self, request, pk=None):
        """GET /api/v1/patient-records/{person_id}/field-provenance/?fields=f1,f2

        Bulk provenance lookup for multiple fields.
        """
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        from omop_core.services.provenance_service import get_fields_provenance

        fields_param = request.query_params.get('fields', '')
        if not fields_param:
            return Response(
                {'error': 'Provide ?fields=field1,field2'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        field_names = [f.strip() for f in fields_param.split(',') if f.strip()]
        if len(field_names) > 20:
            return Response(
                {'error': 'Maximum 20 fields per request'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        results = get_fields_provenance(person, field_names)
        return Response(results)

    @action(detail=True, methods=['get'], url_path='omop',
            permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def omop(self, request, pk=None):
        """GET /api/v1/patient-records/{person_id}/omop/ — admin-only OMOP rows."""
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        actor = request.user
        is_admin = (
            is_service_token(request)
            or bool(getattr(actor, 'is_staff', False))
            or get_admin_orgs(actor).exists()
        )
        if not is_admin:
            return Response(
                {'detail': 'Only administrators can view raw OMOP rows.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        episode_ids = list(
            Episode.objects.filter(person=person).values_list('episode_id', flat=True)
        )
        table_specs = [
            ('person', 'Person', Person.objects.filter(person_id=person.person_id)),
            ('condition_occurrences', 'Condition Occurrences', ConditionOccurrence.objects.filter(person=person)),
            ('drug_exposures', 'Drug Exposures', DrugExposure.objects.filter(person=person)),
            ('measurements', 'Measurements', Measurement.objects.filter(person=person)),
            ('observations', 'Observations', Observation.objects.filter(person=person)),
            ('procedure_occurrences', 'Procedure Occurrences', ProcedureOccurrence.objects.filter(person=person)),
            ('episodes', 'Episodes', Episode.objects.filter(person=person)),
            ('episode_events', 'Episode Events', EpisodeEvent.objects.filter(episode_id__in=episode_ids)),
            ('visit_occurrences', 'Visit Occurrences', VisitOccurrence.objects.filter(person=person)),
            ('visit_details', 'Visit Details', VisitDetail.objects.filter(person=person)),
            ('death', 'Death', Death.objects.filter(person=person)),
        ]

        tables = []
        tables_data = []   # (key, rows) pairs for concept name resolution
        for key, label, qs in table_specs:
            include = _OMOP_COLUMNS.get(key)
            rows = [_serialize_omop_row(obj, include=include) for obj in qs]
            tables_data.append((key, rows))
            tables.append({
                'key': key,
                'label': label,
                'count': len(rows),
                'rows': rows,
            })

        _resolve_concept_names(tables_data)

        return Response({
            'person_id': person.person_id,
            'patient_record_id': patient_info.pk,
            'tables': tables,
        })

    def _resolve_patient_with_auth(self, request, pk):
        """Shared lookup + auth logic for detail-level patient endpoints.

        Used by retrieve(), provenance(), revisions(), and field_provenance*().
        Returns (person, patient_info, None) on success, or
        (None, None, Response) on error.
        """
        try:
            person = Person.objects.get(person_id=pk)
            patient_info = PatientRecord.objects.get(person=person)
        except Person.DoesNotExist:
            return None, None, Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientRecord.DoesNotExist:
            return None, None, Response({'error': 'Patient information not found'}, status=status.HTTP_404_NOT_FOUND)

        if not is_service_token(request):
            org = get_request_org(request)
            if org is not None:
                if patient_info.organization != org:
                    return None, None, Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
            elif not getattr(request.user, 'is_staff', False):
                from omop_core.authorization import can_access_patient
                admin_org_ids = set(get_admin_orgs(request.user).values_list('id', flat=True))
                is_admin_patient = (
                    patient_info.organization_id is not None
                    and patient_info.organization_id in admin_org_ids
                )
                if not is_admin_patient and not can_access_patient(request.user, person.person_id):
                    return None, None, Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)

        return person, patient_info, None

    @action(detail=False, methods=['get', 'patch', 'delete'], permission_classes=[PatientDeletePermission, PatientSelfScopePermission])
    def me(self, request):
        """GET/DELETE the current user's record; PATCH name or projection-owned fields.

        ``PatientRecord`` is a derived OMOP read model. This legacy endpoint
        remains available for its read wire format. Mapped clinical fields are
        read-only; ``patient_name`` updates ``Person`` and unmapped
        projection-owned fields remain writable.
        """
        if request.method == 'DELETE':
            return self._delete_patient_account(request)

        from patient_portal.models import PatientUser
        from patient_portal.services import resolve_or_create_person

        # Fast path: confirmed patient — use cached person directly, no extra query.
        existing_pu = PatientUser.objects.filter(identity=request.user).select_related('person').first()
        if existing_pu is not None:
            person = existing_pu.person
        else:
            # Determine whether this identity may auto-provision a patient record.
            # Staff/superusers and users with an *active* clinical-role grant are not
            # patients; resolve_or_create_person skips creation when allow_create=False
            # but still returns a Person via email match (re-links a deleted PatientUser).
            from omop_core.models import GroupAccess
            now = timezone.now()
            is_clinical = (
                getattr(request.user, 'is_staff', False)
                or GroupAccess.objects.filter(
                    identity=request.user,
                    role__in=['org_admin', 'doctor', 'analyst'],
                ).filter(
                    Q(expires_at__isnull=True) | Q(expires_at__gt=now)
                ).exists()
            )
            person = resolve_or_create_person(request.user, allow_create=not is_clinical)
            if person is None:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        patient_info, _ = PatientRecord.objects.get_or_create(person=person)

        if request.method == 'GET':
            user_serializer = UserSerializer(request.user)
            patient_serializer = PatientRecordSerializer(patient_info)
            full_name = f"{person.given_name or ''} {person.family_name or ''}".strip()
            return Response({
                'patient_info': patient_serializer.data,  # legacy wire format — preserved for frontend/federation host compatibility
                'user': user_serializer.data,
                'patient_name': full_name,
            })

        patient_name, patch_data = _pop_patient_name(request.data)
        echoed = _echoed_unchanged_fields(patient_info, patch_data)
        mapped_fields = sorted((set(patch_data) & PATIENT_RECORD_OMOP_MAPPED_FIELDS) - echoed)
        if mapped_fields:
            return Response(
                {
                    'detail': (
                        'OMOP-mapped PatientRecord fields are read-only. Write a complete '
                        'clinical fact to the appropriate OMOP resource, then rederive the record.'
                    ),
                    'fields': mapped_fields,
                },
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        serializer = PatientRecordSerializer(patient_info, data=patch_data, partial=True)
        serializer.is_valid(raise_exception=True)
        writable_fields = {
            name for name, field in serializer.fields.items() if not field.read_only
        }
        unsupported_fields = sorted(set(patch_data) - writable_fields - echoed)
        if unsupported_fields:
            return Response(
                {'detail': 'Only projection-owned PatientRecord fields are writable.', 'fields': unsupported_fields},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        if not patch_data and 'patient_name' not in request.data:
            return Response(
                {'detail': 'Supply patient_name or a projection-owned PatientRecord field.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def previous_value(obj, field):
            fk_id = f'{field}_id'
            return getattr(obj, fk_id, None) if hasattr(obj, fk_id) else getattr(obj, field, None)

        previous_values = {
            field: previous_value(patient_info, field)
            for field in patch_data
            if hasattr(patient_info, field)
        }
        with transaction.atomic():
            _apply_patient_name(person, patient_name)
            serializer.save()
            _write_record_revisions(patient_info, previous_values, request)

        return Response({
            'patient_info': PatientRecordSerializer(patient_info).data,
            'patient_name': f"{person.given_name or ''} {person.family_name or ''}".strip(),
        })

    def _delete_patient_account(self, request):
        """DELETE /api/patient-info/me/ — permanently delete the patient's account and all data."""
        from patient_portal.services import patient_person_for
        from patient_portal.models import PatientUser

        patient_person = patient_person_for(request.user)
        if patient_person is None:
            return Response(
                {'detail': 'Only patients can delete their own account.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        confirm = request.data.get('confirm')
        if confirm != 'DELETE':
            return Response(
                {'detail': 'Request body must include {"confirm": "DELETE"}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        person_id = patient_person.person_id
        identity = request.user

        with transaction.atomic():
            # Delete EpisodeEvent rows — bare integer FK, not covered by CASCADE
            episode_ids = list(
                Episode.objects.filter(person=patient_person)
                .values_list('episode_id', flat=True)
            )
            if episode_ids:
                EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()

            # Delete Person — cascades to all OMOP tables, PatientRecord, PatientUser
            patient_person.delete()

            # Delete the Identity (auth credential)
            identity.delete()

        # Log out the current session AFTER the transaction succeeds. If the
        # transaction rolled back, the identity would still exist and we must
        # not have flushed the session. Other open sessions resolve to
        # AnonymousUser once the Identity row is gone (Django's session
        # middleware loads the user by PK and falls back to AnonymousUser on
        # DoesNotExist).
        from django.contrib.auth import logout
        logout(request)

        logger.info(
            'patient_account_deleted person_id=%s identity_id=%s',
            person_id, identity.pk,
        )

        return Response({'detail': 'Account and all associated data have been permanently deleted.'})

    @action(detail=True, methods=['delete'], url_path='admin-delete',
            permission_classes=[IsAuthenticated])
    def admin_delete(self, request, pk=None):
        """DELETE /api/v1/patient-records/{person_id}/admin-delete/ — administrator
        deletion of a patient and all associated data (PHR-S FM TI.1.7, admin-initiated).

        ``pk`` is the person_id (consistent with retrieve/export_fhir). Distinct from
        the patient self-service ``me`` DELETE.

        Authorization:
          - staff / superuser: may delete any patient.
          - org_admin: may delete only when EVERY org the patient has a record in is
            one they administer (``get_admin_orgs``). This prevents cascade-deleting a
            patient that is also owned by an org the caller does not administer.
          - everyone else (doctors, analysts, patients): 403.

        Requires body ``{"confirm": "DELETE"}``. The request is audited by
        AuditLogMiddleware (record_delete); an explicit log line is also emitted.
        """
        from patient_portal.models import PatientUser
        from omop_core.models import GroupAccess, Person

        # Resolve by person_id directly — authorization here is the explicit
        # staff/org-admin check below, which is stricter than mere view access
        # (a viewer is not necessarily allowed to delete).
        try:
            person = Person.objects.get(person_id=pk)
        except (Person.DoesNotExist, ValueError, TypeError):
            return Response({'detail': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        actor = request.user
        is_staff_actor = bool(getattr(actor, 'is_staff', False))
        if not is_staff_actor:
            admin_org_ids = set(get_admin_orgs(actor).values_list('id', flat=True))
            person_org_ids = set(
                PatientRecord.objects.filter(person=person)
                .values_list('organization_id', flat=True)
            )
            # Every org this patient belongs to must be one the caller administers;
            # an org-less patient (empty set) is staff-only.
            if not person_org_ids or not person_org_ids.issubset(admin_org_ids):
                return Response(
                    {'detail': 'You do not have permission to delete this patient.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if request.data.get('confirm') != 'DELETE':
            return Response(
                {'detail': 'Request body must include {"confirm": "DELETE"}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        person_id = person.person_id
        # Remove a purely-patient login alongside the record; keep an Identity that
        # also holds provider access (GroupAccess) so we don't revoke a provider.
        linked_identity = None
        pu = PatientUser.objects.filter(person=person).select_related('identity').first()
        if pu and not GroupAccess.objects.filter(identity=pu.identity).exists():
            linked_identity = pu.identity

        with transaction.atomic():
            # EpisodeEvent has a bare integer FK not covered by CASCADE.
            episode_ids = list(
                Episode.objects.filter(person=person).values_list('episode_id', flat=True)
            )
            if episode_ids:
                EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()
            # Person delete cascades to OMOP rows, PatientRecord(s), PatientUser.
            person.delete()
            if linked_identity is not None:
                linked_identity.delete()

        logger.info(
            'admin_patient_deleted person_id=%s by_identity_id=%s is_staff=%s',
            person_id, actor.pk, is_staff_actor,
        )
        return Response(
            {'detail': 'Patient and all associated data have been permanently deleted.'}
        )

    @action(detail=True, methods=['get'], url_path='export-fhir',
            permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def export_fhir(self, request, pk=None):
        """GET /api/v1/patient-records/{person_id}/export-fhir/ — export as FHIR R4 Bundle.

        ``pk`` is interpreted as ``person_id`` (consistent with ``retrieve``).
        """
        from omop_core.services.fhir_export import serialize_signed_fhir_bundle
        from patient_portal.api.fhir.integrity import (
            check_fhir_version, EXPORT_DIGEST_HEADER, EXPORT_SIGNATURE_HEADER,
        )
        from django.http import HttpResponse

        # Bounded multi-version interchange (TI.5.2#01): only R4 is served.
        version_error = check_fhir_version(request)
        if version_error:
            return Response({'error': version_error}, status=status.HTTP_406_NOT_ACCEPTABLE)

        try:
            person = Person.objects.get(person_id=pk)
            patient_record = PatientRecord.objects.get(person=person)
        except (Person.DoesNotExist, PatientRecord.DoesNotExist):
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)

        # Object-level permission check (PatientSelfScopePermission)
        self.check_object_permissions(request, patient_record)

        # Content integrity + non-repudiation (S.3.6#10 / PH.2.3#09): serialize
        # the bundle to canonical bytes, then emit a SHA-256 digest and an HMAC
        # signature over EXACTLY those bytes so the recipient can verify content
        # integrity. We return an HttpResponse (not DRF Response) so the digest
        # matches the body verbatim.
        body_bytes, digest, signature = serialize_signed_fhir_bundle(person)
        response = HttpResponse(body_bytes, content_type='application/fhir+json')
        response[EXPORT_DIGEST_HEADER] = digest
        response[EXPORT_SIGNATURE_HEADER] = signature
        return response

    @action(detail=False, methods=['post'], permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def upload_csv(self, request):
        """Upload the documented CSV shape into OMOP source tables.

        CSV is intentionally limited to facts it can represent without making a
        PatientRecord column a write interface.  ``disease`` needs a known
        ``diagnosis_date`` (or ``disease_date``); it becomes a
        ConditionOccurrence.  PatientRecord is created only by the final
        refresh, never by this importer.
        """
        if 'file' not in request.FILES:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        file = request.FILES['file']
        if not file.name.endswith('.csv'):
            return Response({'error': 'File must be a CSV'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            decoded_file = file.read().decode('utf-8')
            csv_data = StringIO(decoded_file)
            reader = csv.DictReader(csv_data)
            
            created_count = 0
            errors = []
            source = request.META.get('HTTP_X_PROVENANCE_SOURCE', 'EHR_SYNC')
            request_user = getattr(request, 'user', None)
            source_user_id = request.META.get(
                'HTTP_X_PROVENANCE_USER_ID', str(getattr(request_user, 'pk', '') or ''),
            )
            provenance_org = get_request_org(request) if request_user is not None else None
            
            for row_num, row in enumerate(reader, start=2):
                try:
                    person_id = int(row.get('person_id', 0))
                    if person_id == 0:
                        last_person = Person.objects.all().order_by('-person_id').first()
                        person_id = last_person.person_id + 1 if last_person else 1000

                    dob_raw = (row.get('date_of_birth') or '').strip()
                    dob = parse_date(dob_raw) if dob_raw else None
                    if dob_raw and dob is None:
                        raise ValueError('date_of_birth must be ISO YYYY-MM-DD')

                    disease = (row.get('disease') or '').strip()
                    disease_date_raw = (row.get('diagnosis_date') or row.get('disease_date') or '').strip()
                    disease_date = parse_date(disease_date_raw) if disease_date_raw else None
                    if disease and disease_date is None:
                        raise ValueError('disease requires diagnosis_date or disease_date (ISO YYYY-MM-DD)')

                    with transaction.atomic(), suppress_patient_record_refresh():
                        gender_source = (row.get('gender') or 'unknown').strip()
                        gender_concept = get_gender_concept(gender_source)
                        person_defaults = {
                            'gender_concept': gender_concept,
                            'gender_source_value': gender_source,
                            'race_source_value': (row.get('race') or 'unknown').strip(),
                            'ethnicity_source_value': (row.get('ethnicity') or 'unknown').strip(),
                        }
                        if dob:
                            person_defaults.update({
                                'year_of_birth': dob.year,
                                'month_of_birth': dob.month,
                                'day_of_birth': dob.day,
                                'birth_datetime': timezone.make_aware(datetime.combine(dob, datetime.min.time())),
                            })
                        elif (year_raw := (row.get('year_of_birth') or '').strip()):
                            person_defaults['year_of_birth'] = int(year_raw)

                        person, created = Person.objects.get_or_create(
                            person_id=person_id, defaults=person_defaults,
                        )
                        if not created:
                            # CSV fields belong to Person, so update only values explicitly supplied.
                            for field in ('phone_number', 'email', 'given_name', 'family_name', 'facility_name'):
                                if row.get(field):
                                    setattr(person, field, row[field].strip())
                            if dob:
                                person.year_of_birth, person.month_of_birth, person.day_of_birth = dob.year, dob.month, dob.day
                                person.birth_datetime = timezone.make_aware(datetime.combine(dob, datetime.min.time()))
                            elif (year_raw := (row.get('year_of_birth') or '').strip()):
                                person.year_of_birth = int(year_raw)
                            person.save()
                        else:
                            for field in ('phone_number', 'email', 'given_name', 'family_name', 'facility_name'):
                                if row.get(field):
                                    setattr(person, field, row[field].strip())
                            person.save()

                        if disease:
                            concept = Concept.objects.filter(
                                domain_id='Condition', concept_name__iexact=disease,
                            ).order_by('-standard_concept').first()
                            # OMOP concept 0 preserves an unmapped source value without inventing
                            # a clinical code. A vocabulary mapping can be supplied in a future CSV
                            # version; the PatientRecord derivation still reads disease_source_value.
                            concept = concept or Concept.objects.get(concept_id=0)
                            if not ConditionOccurrence.objects.filter(
                                person=person,
                                condition_concept=concept,
                                condition_start_date=disease_date,
                                condition_source_value=disease,
                            ).exists():
                                condition = ConditionOccurrence.objects.create(
                                    condition_occurrence_id=next_pk(ConditionOccurrence, 'condition_occurrence_id'),
                                    person=person,
                                    condition_concept=concept,
                                    condition_start_date=disease_date,
                                    condition_type_concept=Concept.objects.get(concept_id=32817),
                                    condition_source_value=disease,
                                )
                                _record_provenance(
                                    condition, source, source_user_id,
                                    target_patient_id=str(person.person_id), organization=provenance_org,
                                )

                        # This is the only projection operation in CSV ingestion. It upserts the
                        # derived read model after all Person/OMOP source facts are committed.
                        refresh_patient_record(person)
                    if created:
                        created_count += 1
                        
                except Exception as e:
                    errors.append(f"Row {row_num}: {str(e)}")
            
            return Response({
                'success': True,
                'created_count': created_count,
                'errors': errors
            })
            
        except Exception as e:
            logger.exception('CSV upload failed')
            return Response({'error': 'Upload failed. Please check the file format and try again.'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def upload_fhir(self, request):
        """Upload patients from FHIR JSON file"""
        if 'file' not in request.FILES:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        file = request.FILES['file']
        if not file.name.endswith('.json'):
            return Response({'error': 'File must be a JSON file'}, status=status.HTTP_400_BAD_REQUEST)

        # Bounded multi-version interchange (TI.5.2#01): decline unsupported
        # FHIR versions cleanly rather than mis-parsing them.
        from patient_portal.api.fhir.integrity import (
            check_fhir_version, verify_content_digest,
        )
        version_error = check_fhir_version(request)
        if version_error:
            return Response({'error': version_error}, status=status.HTTP_406_NOT_ACCEPTABLE)

        try:
            raw_bytes = file.read()

            # Content integrity (S.3.6#10 / PH.2.3#09): if the client asserted a
            # SHA-256 digest of the payload, verify it against the received bytes.
            # Opt-in — no header means current behavior is unchanged.
            digest_error = verify_content_digest(request, raw_bytes)
            if digest_error:
                return Response({'error': digest_error}, status=status.HTTP_400_BAD_REQUEST)

            fhir_data = json.loads(raw_bytes)

            if fhir_data.get('resourceType') != 'Bundle':
                return Response({'error': 'FHIR file must be a Bundle'}, status=status.HTTP_400_BAD_REQUEST)

            prov_source, prov_user_id, prov_reason = _extract_provenance(request)
            if prov_source == 'ADMIN_CORRECTION' and not prov_reason:
                return Response(
                    {'error': 'modification_reason is required when source is ADMIN_CORRECTION'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            created_count = 0
            updated_count = 0
            errors = []
            patients_result = []

            # Group resources by patient. Some mCODE/Synthea bundles reference
            # Patient resources by entry.fullUrl (often urn:uuid:...) rather than
            # by Patient/{id}, so keep aliases for both forms.
            patients_data = {}
            patient_ref_aliases = {}
            # Keyed by resource id and fullUrl — used to resolve medicationReference
            # in MedicationRequest resources (Synthea uses this pattern instead of
            # inline medicationCodeableConcept).
            medication_resources: dict[str, dict] = {}

            def _resolve_patient_ref(ref: str) -> str:
                """Resolve a FHIR subject reference to the local patient bucket id."""
                ref = (ref or '').strip()
                if not ref:
                    return ''
                if ref in patient_ref_aliases:
                    return patient_ref_aliases[ref]
                bare_ref = ref[len('urn:uuid:'):] if ref.startswith('urn:uuid:') else ref
                if bare_ref in patient_ref_aliases:
                    return patient_ref_aliases[bare_ref]
                return ref.split('/')[-1] if '/' in ref else bare_ref

            for entry in fhir_data.get('entry', []):
                resource = entry.get('resource', {})
                resource_type = resource.get('resourceType')

                if resource_type == 'Patient':
                    patient_id = resource.get('id', '')
                    patients_data[patient_id] = {
                        'patient': resource,
                        'conditions': [],
                        'observations': [],
                        'medications': [],
                        'procedures': [],
                        'medication_requests': [],
                        'immunizations': [],
                        'diagnostic_reports': [],
                        'allergy_intolerances': [],
                        'encounters': [],
                    }
                    if patient_id:
                        patient_ref_aliases[patient_id] = patient_id
                        patient_ref_aliases[f'Patient/{patient_id}'] = patient_id
                    full_url = (entry.get('fullUrl') or '').strip()
                    if full_url:
                        patient_ref_aliases[full_url] = patient_id
                        if full_url.startswith('urn:uuid:'):
                            patient_ref_aliases[full_url[len('urn:uuid:'):]] = patient_id
                elif resource_type == 'Condition':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['conditions'].append(resource)
                elif resource_type == 'Observation':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['observations'].append(resource)
                elif resource_type == 'MedicationStatement':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['medications'].append(resource)
                elif resource_type == 'Medication':
                    # Collect standalone Medication resources so that MedicationRequest
                    # entries using medicationReference can resolve to a code.
                    med_id = resource.get('id', '')
                    if med_id:
                        medication_resources[med_id] = resource
                    full_url = (entry.get('fullUrl') or '').strip()
                    if full_url:
                        medication_resources[full_url] = resource
                        if full_url.startswith('urn:uuid:'):
                            medication_resources[full_url[len('urn:uuid:'):]] = resource
                elif resource_type == 'MedicationRequest':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['medication_requests'].append(resource)
                elif resource_type == 'Procedure':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['procedures'].append(resource)
                elif resource_type == 'Immunization':
                    patient_ref = resource.get('patient', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['immunizations'].append(resource)
                elif resource_type == 'DiagnosticReport':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['diagnostic_reports'].append(resource)
                elif resource_type == 'AllergyIntolerance':
                    patient_ref = resource.get('patient', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['allergy_intolerances'].append(resource)
                elif resource_type == 'Encounter':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = _resolve_patient_ref(patient_ref)
                    if patient_id in patients_data:
                        patients_data[patient_id]['encounters'].append(resource)
            
            # Hoist SCT vocabulary sets for FHIR upload validation (avoids N+1 per patient).
            _allowed_sct_titles = set(StemCellTransplant.objects.values_list('title', flat=True))
            _allowed_elig_titles = set(SctEligibility.objects.values_list('title', flat=True))

            # Hoist constant Concept lookups — these are the same for every patient and every
            # observation. Using the process-level concept_cache means each of these is a
            # zero-cost memory hit on all subsequent calls (across batches and requests).
            _concept_breast_cancer = (
                Concept.objects.filter(concept_code='254837009', vocabulary_id='SNOMED').first()
                or Concept.objects.filter(concept_name__icontains='breast cancer').first()
            )
            _concept_fl = (
                Concept.objects.filter(concept_code='413448000', vocabulary_id='SNOMED').first()
                or Concept.objects.filter(concept_name__icontains='follicular lymphoma').first()
                or Concept.objects.filter(concept_name__icontains='follicular non-hodgkin').first()
            )
            _concept_dlbcl = (
                Concept.objects.filter(concept_code='C83.30', vocabulary_id='ICD10CM').first()
                or Concept.objects.filter(concept_name__icontains='diffuse large b-cell').first()
            )
            _concept_ehr_type      = _cc_by_id(32817)    # EHR
            _concept_lab_type      = _cc_by_id(32856)    # Lab
            _concept_drug_type     = _cc_by_id(32869)    # EHR prescription
            _concept_tx_regimen    = _cc_by_id(32531)    # Treatment Regimen
            _concept_de_field      = _cc_by_id(1147094)  # DrugExposure field
            # OMOP's 'No matching concept'. Never a real analyte's id — see
            # CONCEPT_GENERIC_LAB in omop_core/services/mappings.py.
            _concept_generic_lab   = _cc_by_id(CONCEPT_GENERIC_LAB)

            def _get_or_create_visit_concept(class_code: str, class_display: str):
                concept_code = f'FHIR-VISIT-{class_code or "UNKNOWN"}'
                concept_name = class_display or class_code or 'FHIR Encounter'
                concept = Concept.objects.filter(
                    vocabulary_id='FHIR',
                    concept_code=concept_code,
                ).first()
                if concept:
                    return concept
                domain, _ = Domain.objects.get_or_create(
                    domain_id='Visit',
                    defaults={'domain_name': 'Visit', 'domain_concept_id': 0},
                )
                vocabulary, _ = Vocabulary.objects.get_or_create(
                    vocabulary_id='FHIR',
                    defaults={
                        'vocabulary_name': 'FHIR',
                        'vocabulary_reference': 'FHIR import',
                        'vocabulary_version': 'local',
                        'vocabulary_concept_id': 0,
                    },
                )
                concept_class, _ = ConceptClass.objects.get_or_create(
                    concept_class_id='Visit',
                    defaults={'concept_class_name': 'Visit', 'concept_class_concept_id': 0},
                )
                return Concept.objects.create(
                    concept_id=next_pk(Concept, 'concept_id'),
                    concept_name=concept_name,
                    domain=domain,
                    vocabulary=vocabulary,
                    concept_class=concept_class,
                    # Locally minted, so not Standard: only OHDSI assigns that,
                    # and a mint claiming 'S' is unreachable from
                    # concept_ancestor while appearing standard to tooling. The
                    # source tag below is already correct. See #453.
                    standard_concept=None,
                    source='HealthKey',
                    concept_code=concept_code,
                    valid_start_date=datetime(1970, 1, 1).date(),
                    valid_end_date=datetime(2099, 12, 31).date(),
                )

            # When skip_refresh=true the caller (e.g. load_fhir_bundle) will run
            # refresh_patient_record for all patients after the upload completes.
            # This eliminates the per-patient refresh cost during the tight write loop.
            _skip_refresh = request.query_params.get('skip_refresh', 'false').lower() in ('1', 'true')

            # Process each patient
            import time as _time
            for fhir_patient_id, data in patients_data.items():
                try:
                    _pt_start = _time.monotonic()
                    _pt_measurement_ids = []
                    _pt_condition_ids = []
                    _pt_drug_exposure_ids = []
                    _pt_procedure_ids = []
                    _pt_episode_ids = []
                    _pt_episode_event_ids = []
                    _pt_visit_ids = []

                    patient_resource = data['patient']

                    # Generate new person_id
                    last_person = Person.objects.all().order_by('-person_id').first()
                    person_id = last_person.person_id + 1 if last_person else 1000
                    
                    # Parse birth date
                    birth_date = None
                    year_of_birth = None
                    month_of_birth = None
                    day_of_birth = None
                    
                    if patient_resource.get('birthDate'):
                        birth_date = datetime.strptime(patient_resource['birthDate'], '%Y-%m-%d').date()
                        year_of_birth = birth_date.year
                        month_of_birth = birth_date.month
                        day_of_birth = birth_date.day
                    
                    # Extract address information from FHIR
                    country = None
                    region = None
                    city = None
                    postal_code = None
                    
                    if patient_resource.get('address') and len(patient_resource['address']) > 0:
                        address = patient_resource['address'][0]
                        country = address.get('country')
                        region = address.get('state')
                        city = address.get('city')
                        postal_code = address.get('postalCode')
                    
                    # Extract race, ethnicity and vital signs from extensions
                    race = None
                    ethnicity = None
                    weight = None
                    height = None
                    systolic_bp = None
                    diastolic_bp = None
                    heart_rate = None
                    ecog = None
                    cytogenetics_str = None
                    measurable_disease_imwg = None
                    sct_date_str = None
                    sct_history_str = None
                    sct_eligibility_str = None
                    
                    # Explicit extension URL → (value_key, parser) registry.
                    # Using exact URL matching avoids false positives from substring checks.
                    _PATIENT_EXTENSIONS = {
                        'https://healthkey.ai/fhir/StructureDefinition/race':
                            ('valueString', lambda e: e.get('valueString')),
                        'https://healthkey.ai/fhir/StructureDefinition/ethnicity':
                            ('valueString', lambda e: e.get('valueString')),
                        'https://healthkey.ai/fhir/StructureDefinition/bodyWeight':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'https://healthkey.ai/fhir/StructureDefinition/bodyHeight':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'https://healthkey.ai/fhir/StructureDefinition/systolic-bp':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'https://healthkey.ai/fhir/StructureDefinition/diastolic-bp':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'https://healthkey.ai/fhir/StructureDefinition/heartRate':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'https://healthkey.ai/fhir/StructureDefinition/ecog-performance-status':
                            ('valueInteger', lambda e: e.get('valueInteger')),
                        'https://healthkey.ai/fhir/StructureDefinition/mm-cytogenetic-markers':
                            ('valueString', lambda e: e.get('valueString')),
                        'https://healthkey.ai/fhir/StructureDefinition/mm-measurable-disease-imwg':
                            ('valueBoolean', lambda e: e.get('valueBoolean')),
                        'https://healthkey.ai/fhir/StructureDefinition/mm-sct-date':
                            ('valueString', lambda e: e.get('valueString')),
                        'https://healthkey.ai/fhir/StructureDefinition/mm-sct-history':
                            ('valueString', lambda e: e.get('valueString')),
                        'https://healthkey.ai/fhir/StructureDefinition/mm-sct-eligibility':
                            ('valueString', lambda e: e.get('valueString')),
                    }
                    ext_results = {}
                    for ext in patient_resource.get('extension', []):
                        url = ext.get('url', '')
                        if url in _PATIENT_EXTENSIONS:
                            _, parser = _PATIENT_EXTENSIONS[url]
                            ext_results[url] = parser(ext)

                    base = 'https://healthkey.ai/fhir/StructureDefinition/'
                    race            = ext_results.get(f'{base}race')
                    ethnicity       = ext_results.get(f'{base}ethnicity')

                    # US Core race / ethnicity extensions (mCODE / Synthea FHIR bundles).
                    # These are nested extensions; extract the 'text' sub-extension value.
                    if not race or not ethnicity:
                        for ext in patient_resource.get('extension', []):
                            url = ext.get('url', '')
                            if not race and url == (
                                'http://hl7.org/fhir/us/core/StructureDefinition/us-core-race'
                            ):
                                for sub in ext.get('extension', []):
                                    if sub.get('url') == 'text':
                                        race = sub.get('valueString')
                                        break
                            elif not ethnicity and url == (
                                'http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity'
                            ):
                                for sub in ext.get('extension', []):
                                    if sub.get('url') == 'text':
                                        ethnicity = sub.get('valueString')
                                        break
                    weight          = ext_results.get(f'{base}bodyWeight')
                    height          = ext_results.get(f'{base}bodyHeight')
                    systolic_bp     = ext_results.get(f'{base}systolic-bp')
                    diastolic_bp    = ext_results.get(f'{base}diastolic-bp')
                    heart_rate      = ext_results.get(f'{base}heartRate')
                    ecog            = ext_results.get(f'{base}ecog-performance-status')
                    cytogenetics_str        = ext_results.get(f'{base}mm-cytogenetic-markers')
                    measurable_disease_imwg = ext_results.get(f'{base}mm-measurable-disease-imwg')
                    sct_date_str            = ext_results.get(f'{base}mm-sct-date')
                    sct_history_str         = ext_results.get(f'{base}mm-sct-history')
                    sct_eligibility_str     = ext_results.get(f'{base}mm-sct-eligibility')
                    
                    # Get gender concept from FHIR
                    gender_concept = get_gender_concept(patient_resource.get('gender', ''))
                    
                    # Extract name from FHIR
                    name = patient_resource.get('name', [{}])[0] if patient_resource.get('name') else {}
                    given_name = ' '.join(name.get('given', [])) if name.get('given') else ''
                    family_name = name.get('family', '')
                    
                    # Suppress signal-triggered PatientRecord refreshes for all OMOP
                    # writes below. Use __enter__/__exit__ explicitly so the finally
                    # block guarantees cleanup even on BaseException (e.g. KeyboardInterrupt),
                    # without requiring 1000 lines of re-indentation.
                    from omop_core.signals import suppress_patient_record_refresh as _suppress_cm_fn
                    _suppress_cm = _suppress_cm_fn()
                    _suppress_cm.__enter__()

                    # Wrap all per-patient DB writes — including Person creation — in a
                    # savepoint so a failure mid-patient rolls back fully rather than
                    # leaving orphaned rows. _atomic_entered tracks whether __enter__
                    # was called so the finally block can roll back exactly once.
                    _atomic_cm = transaction.atomic()
                    _atomic_entered = False
                    _last_exc = None
                    _atomic_cm.__enter__()
                    _atomic_entered = True

                    # Upsert Person: match on name + full birth date to avoid duplicates on re-upload
                    person = None
                    person_is_new = False
                    _normalize_name = lambda value: re.sub(r'\d+', '', (value or '')).strip().lower()
                    if (given_name or family_name) and year_of_birth:
                        person_match = Person.objects.filter(
                            given_name=given_name,
                            family_name=family_name,
                            year_of_birth=year_of_birth,
                        )
                        if month_of_birth:
                            person_match = person_match.filter(month_of_birth=month_of_birth)
                        if day_of_birth:
                            person_match = person_match.filter(day_of_birth=day_of_birth)
                        person = person_match.first()
                        if person is None:
                            person_match = Person.objects.filter(
                                year_of_birth=year_of_birth,
                            )
                            if month_of_birth:
                                person_match = person_match.filter(month_of_birth=month_of_birth)
                            if day_of_birth:
                                person_match = person_match.filter(day_of_birth=day_of_birth)
                            normalized_given = _normalize_name(given_name)
                            normalized_family = _normalize_name(family_name)
                            for candidate in person_match.select_related('patient_record'):
                                if (
                                    _normalize_name(candidate.given_name) == normalized_given
                                    and _normalize_name(candidate.family_name) == normalized_family
                                ):
                                    person = candidate
                                    if candidate.given_name != given_name or candidate.family_name != family_name:
                                        candidate.given_name = given_name
                                        candidate.family_name = family_name
                                        candidate.save(update_fields=['given_name', 'family_name'])
                                    break
                    if person is None:
                        from omop_core.services.pk import next_pk as _next_pk
                        person = Person.objects.create(
                            person_id=_next_pk(Person, 'person_id'),
                            gender_concept=gender_concept,
                            year_of_birth=year_of_birth or datetime.now().year - 50,
                            month_of_birth=month_of_birth,
                            day_of_birth=day_of_birth,
                            race_concept=None,
                            race_source_value=race or None,
                            ethnicity_concept=None,
                            ethnicity_source_value=ethnicity or None,
                            given_name=given_name,
                            family_name=family_name,
                        )
                        person_is_new = True
                        full_name = f"{given_name} {family_name}".strip()
                        identity, _ = Identity.objects.get_or_create(
                            sub=f'patient{person.person_id}',
                            defaults={
                                'issuer': 'urn:local',
                                'name': full_name,
                            },
                        )

                    if country or region or city or postal_code:
                        location = Location.objects.filter(
                            country=country or None,
                            state=region or None,
                            city=city or None,
                            zip=postal_code or None,
                        ).order_by('location_id').first()
                        if location is None:
                            location = Location.objects.create(
                                location_id=next_pk(Location, 'location_id'),
                                country=country or None,
                                state=region or None,
                                city=city or None,
                                zip=postal_code or None,
                                address_1=None,
                                address_2=None,
                                county=None,
                                latitude=None,
                                longitude=None,
                                location_source_value='|'.join(
                                    part for part in [city, region, postal_code, country] if part
                                )[:50],
                            )
                        if person.location_id != location.location_id:
                            person.location_id = location.location_id
                            person.save(update_fields=['location_id'])

                    death_date = None
                    death_datetime = None
                    death_reason = None
                    deceased_dt_raw = patient_resource.get('deceasedDateTime')
                    if deceased_dt_raw:
                        try:
                            death_datetime = datetime.fromisoformat(
                                deceased_dt_raw.replace('Z', '+00:00')
                            )
                            death_date = death_datetime.date()
                        except ValueError:
                            try:
                                death_date = datetime.strptime(deceased_dt_raw[:10], '%Y-%m-%d').date()
                            except ValueError:
                                death_date = None
                    elif patient_resource.get('deceasedBoolean') is True:
                        # FHIR can indicate deceased without a date. Prefer a
                        # real deceasedDateTime when present; otherwise record
                        # a deterministic import-date event for downstream
                        # survival analytics rather than dropping mortality.
                        death_date = localdate()
                        death_reason = (
                            'FHIR Patient.deceasedBoolean=true had no deceasedDateTime; '
                            'death_date inferred as import date.'
                        )

                    if death_date:
                        death, _ = Death.objects.update_or_create(
                            person=person,
                            defaults={
                                'death_date': death_date,
                                'death_datetime': death_datetime,
                                'death_type_concept': _concept_ehr_type or _concept_tx_regimen,
                            },
                        )
                        _record_provenance(
                            death,
                            prov_source or 'EHR_SYNC',
                            prov_user_id,
                            target_patient_id=fhir_patient_id,
                            modification_reason=death_reason or prov_reason,
                            organization=get_request_org(request),
                        )

                    for encounter in data.get('encounters', []):
                        period = encounter.get('period') or {}
                        start_raw = period.get('start') or period.get('end')
                        end_raw = period.get('end') or period.get('start')
                        if not start_raw:
                            continue
                        try:
                            visit_start_date = datetime.strptime(start_raw[:10], '%Y-%m-%d').date()
                            visit_end_date = datetime.strptime((end_raw or start_raw)[:10], '%Y-%m-%d').date()
                        except ValueError:
                            continue
                        visit_class = (encounter.get('class') or {}).get('code') or 'VISIT'
                        visit_class_display = (encounter.get('class') or {}).get('display') or 'FHIR Encounter'
                        visit_concept = _get_or_create_visit_concept(visit_class, visit_class_display)
                        if not VisitOccurrence.objects.filter(
                            person=person,
                            visit_start_date=visit_start_date,
                            visit_source_value=visit_class[:255],
                        ).exists():
                            visit = VisitOccurrence.objects.create(
                                visit_occurrence_id=next_pk(VisitOccurrence, 'visit_occurrence_id'),
                                person=person,
                                visit_concept=visit_concept,
                                visit_start_date=visit_start_date,
                                visit_start_datetime=datetime.combine(visit_start_date, datetime.min.time()),
                                visit_end_date=visit_end_date,
                                visit_end_datetime=datetime.combine(visit_end_date, datetime.min.time()),
                                visit_type_concept=_concept_ehr_type or visit_concept,
                                provider_id=None,
                                care_site_id=None,
                                visit_source_value=visit_class[:255],
                                visit_source_concept=visit_concept,
                            )
                            _pt_visit_ids.append(visit.visit_occurrence_id)
                            VisitDetail.objects.get_or_create(
                                person=person,
                                visit_detail_start_date=visit_start_date,
                                visit_detail_source_value=visit_class[:255],
                                defaults={
                                    'visit_detail_id': next_pk(VisitDetail, 'visit_detail_id'),
                                    'visit_detail_concept': visit_concept,
                                    'visit_detail_start_datetime': datetime.combine(visit_start_date, datetime.min.time()),
                                    'visit_detail_end_date': visit_end_date,
                                    'visit_detail_end_datetime': datetime.combine(visit_end_date, datetime.min.time()),
                                    'visit_detail_type_concept': _concept_ehr_type or visit_concept,
                                    'provider_id': None,
                                    'care_site_id': None,
                                    'visit_detail_source_concept': visit_concept,
                                    'visit_occurrence': visit,
                                },
                            )

                    # Block analysts from updating existing patients via FHIR upload.
                    if not person_is_new and not getattr(request.user, 'is_staff', False):
                        request_org = get_request_org(request)
                        same_org_upload = (
                            request_org is not None
                            and PatientRecord.objects.filter(person=person, organization=request_org).exists()
                        )
                        from omop_core.authorization import can_write_patient
                        if not same_org_upload and not can_write_patient(request.user, person.person_id):
                            errors.append({
                                'patient': f'{given_name} {family_name}',
                                'error': 'Analysts have read-only access. Contact a doctor or org admin to update patient data.',
                            })
                            continue

                    # Extract disease, stage, and histologic type from Condition
                    disease = None
                    stage = ''
                    histologic_type = ''
                    condition_date = None
                    breast_cancer_onset = None  # onset from primary cancer condition
                    fl_onset = None             # onset from FL condition
                    dlbcl_onset = None          # onset from DLBCL (transformation) condition
                    _any_bc_condition = False   # True if any BC condition was seen in the bundle
                    _clinical_status_source = None  # FHIR Condition.clinicalStatus code for primary BC
                    _breast_cancer_stage = None  # condition-stage assertion, persisted to OMOP below
                    _breast_cancer_stage_datetime = None

                    def _disease_from_condition_code(codeable):
                        codeable = codeable or {}
                        condition_text = ' '.join(
                            filter(
                                None,
                                [
                                    codeable.get('text', ''),
                                    *[
                                        coding.get('display', '')
                                        for coding in codeable.get('coding', [])
                                    ],
                                ],
                            )
                        ).lower()
                        if 'myeloma' in condition_text:
                            return 'Multiple Myeloma'
                        if 'breast' in condition_text:
                            return 'Breast Cancer'
                        if 'follicular lymphoma' in condition_text:
                            return 'Follicular Lymphoma'
                        if 'chronic lymphocytic leukemia' in condition_text or 'cll' in condition_text:
                            return 'Chronic Lymphocytic Leukemia'
                        return None

                    def _disease_slug_from_name(name):
                        if not name:
                            return None
                        if name.strip().lower() == 'multiple myeloma':
                            return 'MM'
                        return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:100]

                    for condition in data['conditions']:
                        _condition_stage = None
                        # Get histologic type from code
                        code = condition.get('code', {})
                        coding_list = code.get('coding', [])
                        if code.get('text'):
                            histologic_type = code['text']
                        elif coding_list:
                            histologic_type = coding_list[0].get('display', '')

                        # Identify primary breast cancer condition.
                        # Check SNOMED 254837009 (mCODE canonical), coding display
                        # containing 'breast' (e.g. SNOMED 413448000), and histologic type.
                        snomed_code = next(
                            (c.get('code') for c in coding_list
                             if 'snomed' in c.get('system', '').lower()),
                            None
                        )
                        is_breast_cancer = (
                            snomed_code == '254837009'
                            or any('breast' in c.get('display', '').lower() for c in coding_list)
                            or bool(histologic_type and 'breast' in histologic_type.lower())
                        )
                        is_fl = (
                            # NOTE: do NOT match on snomed_code — 413448000 appears on
                            # breast-cancer conditions in the wild (see is_breast_cancer).
                            any('follicular' in c.get('display', '').lower()
                                and 'lymphoma' in c.get('display', '').lower() for c in coding_list)
                            or bool(histologic_type and 'follicular' in histologic_type.lower())
                        )
                        is_dlbcl = (
                            any('diffuse large b-cell' in c.get('display', '').lower() for c in coding_list)
                            or any(c.get('code', '').startswith('C83.3') for c in coding_list
                                   if 'icd' in c.get('system', '').lower())
                            or bool(histologic_type and 'diffuse large b-cell' in histologic_type.lower())
                        )
                        if is_breast_cancer:
                            _any_bc_condition = True
                            _cs = condition.get('clinicalStatus', {}).get('coding', [])
                            if _cs:
                                _clinical_status_source = _cs[0].get('code')
                        disease_from_code = _disease_from_condition_code(code)
                        if disease_from_code:
                            disease = disease_from_code

                        # Get stage and infer disease from stage text (e.g. "Breast Cancer Stage IIA").
                        # When several stage entries exist (MM records both ISS and R-ISS),
                        # prefer R-ISS as the more current MM staging system so the patched
                        # value matches the derived one instead of overriding it with ISS.
                        stages = condition.get('stage', [])
                        _stage_entry = next(
                            (s for s in stages
                             if (s.get('summary', {}).get('text') or '').upper().startswith('R-ISS')),
                            stages[0] if stages else None,
                        )
                        if _stage_entry is not None:
                            stage_summary = _stage_entry.get('summary', {})
                            if stage_summary.get('text'):
                                stage_text = stage_summary['text']
                                if 'Stage' in stage_text:
                                    stage_suffix = stage_text.split('Stage')[-1].strip()
                                    stage_prefix = stage_text.split('Stage')[0].strip()
                                    if stage_prefix.upper() in {'ISS', 'R-ISS', 'RISS'}:
                                        # Keep the staging system in the value, e.g. "R-ISS III".
                                        stage = f'{stage_prefix} {stage_suffix}'.strip()
                                    else:
                                        stage = stage_suffix
                                        if not disease:
                                            disease = stage_prefix or None
                            elif stage_summary.get('coding') and len(stage_summary['coding']) > 0:
                                # Prefer display (e.g. "Stage 2B") over the raw code
                                _sc = stage_summary['coding'][0]
                                stage_display = _sc.get('display', '')
                                if 'Stage' in stage_display:
                                    stage = stage_display.split('Stage')[-1].strip()
                                else:
                                    stage = stage_display or _sc.get('code', '')

                        if is_breast_cancer and stage:
                            _condition_stage = stage

                        # Get condition onset date (handles both 'YYYY-MM-DD' and ISO datetime)
                        if condition.get('onsetDateTime'):
                            try:
                                raw = condition['onsetDateTime']
                                if 'T' in raw:
                                    _parsed_date = datetime.fromisoformat(raw)
                                    if _parsed_date.tzinfo is None:
                                        _parsed_date = timezone.make_aware(_parsed_date)
                                else:
                                    _parsed_date = timezone.make_aware(
                                        datetime.strptime(raw, '%Y-%m-%d')
                                    )
                                condition_date = _parsed_date  # fallback: last wins
                                if is_breast_cancer and _parsed_date and (breast_cancer_onset is None or _parsed_date < breast_cancer_onset):
                                    breast_cancer_onset = _parsed_date
                                if is_breast_cancer and _condition_stage:
                                    # Keep the stage tied to the condition that
                                    # asserted it, not to an earlier/later
                                    # breast-cancer diagnosis in the bundle.
                                    _breast_cancer_stage = _condition_stage
                                    _breast_cancer_stage_datetime = _parsed_date
                                if is_fl and _parsed_date and (fl_onset is None or _parsed_date < fl_onset):
                                    fl_onset = _parsed_date
                                if is_dlbcl and _parsed_date and (dlbcl_onset is None or _parsed_date < dlbcl_onset):
                                    dlbcl_onset = _parsed_date
                            except (ValueError, TypeError):
                                pass

                    # Prefer primary cancer onset over any condition date
                    if breast_cancer_onset:
                        condition_date = breast_cancer_onset
                    
                    # Upsert ConditionOccurrence for the diagnosis
                    from omop_core.models import ConditionOccurrence

                    def _upsert_condition(concept, onset, source_value, status_source):
                        """Create one ConditionOccurrence per (person, concept, start date)."""
                        if not concept or not onset:
                            return
                        type_concept = _concept_ehr_type or concept
                        if ConditionOccurrence.objects.filter(
                            person=person,
                            condition_concept=concept,
                            condition_start_date=onset.date(),
                        ).exists():
                            return
                        _co = ConditionOccurrence(
                            condition_occurrence_id=next_pk(ConditionOccurrence, 'condition_occurrence_id'),
                            person=person,
                            condition_concept=concept,
                            condition_start_date=onset.date(),
                            condition_start_datetime=onset,
                            condition_type_concept=type_concept,
                            condition_source_value=source_value,
                            condition_status_source_value=status_source,
                        )
                        _co._skip_patient_record_refresh = True
                        try:
                            with transaction.atomic():
                                _co.save()
                                _pt_condition_ids.append(_co.condition_occurrence_id)
                                if prov_source:
                                    _record_provenance(_co, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
                        except Exception as _coex:
                            logger.warning(
                                '{"event": "condition_occurrence_save_failed", "error_type": "%s"}',
                                type(_coex).__name__,
                            )

                    if _any_bc_condition and condition_date:
                        _upsert_condition(_concept_breast_cancer, condition_date, disease, _clinical_status_source)

                    if _breast_cancer_stage and _breast_cancer_stage_datetime:
                        _stage_date = _breast_cancer_stage_datetime.date()
                        _stage_exists = Observation.objects.filter(
                            person=person,
                            observation_source_value=FHIR_CONDITION_STAGE_SOURCE_VALUE,
                            observation_date=_stage_date,
                            value_as_string=_breast_cancer_stage,
                        ).exists()
                        if not _stage_exists:
                            _stage_observation = Observation(
                                observation_id=next_pk(Observation, 'observation_id'),
                                person=person,
                                observation_concept=_concept_ehr_type or _concept_tx_regimen,
                                observation_date=_stage_date,
                                observation_datetime=_breast_cancer_stage_datetime,
                                observation_type_concept=_concept_ehr_type or _concept_tx_regimen,
                                value_as_string=_breast_cancer_stage,
                                observation_source_value=FHIR_CONDITION_STAGE_SOURCE_VALUE,
                            )
                            _stage_observation._skip_patient_record_refresh = True
                            _stage_observation.save()
                            _record_provenance(
                                _stage_observation,
                                prov_source or 'EHR_SYNC',
                                prov_user_id,
                                target_patient_id=fhir_patient_id,
                                modification_reason=prov_reason,
                                organization=get_request_org(request),
                            )

                    # FL diagnosis and DLBCL transformation conditions (FL → DLBCL
                    # transformation is derived from the DLBCL ConditionOccurrence).
                    _upsert_condition(_concept_fl, fl_onset, 'Follicular Lymphoma', None)
                    _upsert_condition(_concept_dlbcl, dlbcl_onset, 'Diffuse Large B-Cell Lymphoma', None)

                    # Process observations and create Measurement records
                    _timing_hash = hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12]
                    logger.info("TIMING patient=%s phase=person_setup elapsed=%.1fs", _timing_hash, _time.monotonic() - _pt_start)
                    from omop_core.models import Measurement
                    from omop_core.services.pk import next_pk_batch as _next_pk_batch
                    
                    # Extract tumor characteristics and lab values from observations
                    tumor_size = None
                    lymph_node_status = None
                    metastasis_status = None
                    tumor_stage = None
                    nodes_stage = None
                    distant_metastasis_stage = None
                    staging_modalities = None
                    measurable_disease_by_recist_status = None
                    bone_only_metastasis_status = None
                    clonal_bone_marrow_b_lymphocytes = None
                    er_status = None
                    pr_status = None
                    her2_status = None
                    ki67_index = None
                    pdl1_status = None
                    pdl1_percentage = None
                    genetic_mutations = []
                    
                    # Blood count values
                    hemoglobin_g_dl = None
                    hematocrit_percent = None
                    wbc_count = None
                    rbc_count = None
                    platelet_count = None
                    anc_count = None
                    alc_count = None
                    amc_count = None
                    
                    # Kidney function
                    serum_calcium = None
                    serum_creatinine = None
                    creatinine_clearance = None
                    egfr = None
                    bun = None
                    
                    # Electrolytes
                    sodium = None
                    potassium = None
                    calcium = None
                    magnesium = None
                    
                    # Liver function
                    bilirubin_total = None
                    bilirubin_direct = None
                    alt = None
                    ast = None
                    alkaline_phosphatase = None
                    albumin = None
                    total_protein = None
                    
                    # Cardiac & Other
                    troponin = None
                    bnp = None
                    glucose = None
                    hba1c = None
                    ldh = None
                    
                    # Other markers
                    beta2_microglobulin = None
                    c_reactive_protein = None
                    esr = None
                    creatinine_clearance_rate = None
                    
                    # Coagulation
                    inr = None
                    pt = None
                    ptt = None
                    
                    # Tumor markers
                    cea = None
                    ca19_9 = None
                    psa = None
                    
                    # Behavior tab - Lifestyle
                    smoking_status = None
                    pack_years = None
                    alcohol_use = None
                    drinks_per_week = None
                    exercise_frequency = None
                    exercise_minutes_per_week = None
                    diet_type = None
                    
                    # Behavior tab - Sleep & Wellbeing
                    sleep_hours_per_night = None
                    sleep_quality = None
                    stress_level = None
                    social_support = None
                    
                    # Behavior tab - Socioeconomic
                    employment_status = None
                    education_level = None
                    marital_status = None
                    insurance_type = None
                    number_of_dependents = None
                    annual_household_income = None
                    
                    # Cancer Assessment Fields
                    ecog_assessment_date = None
                    test_methodology = None
                    test_date = None
                    test_specimen_type = None
                    report_interpretation = None
                    oncotype_dx_score = None
                    androgen_receptor_status = None
                    
                    # Treatment Fields
                    therapy_intent = None
                    reason_for_discontinuation = None
                    therapy_intent_observations = []  # List of {'date': date, 'value': value}
                    discontinuation_observations = []  # List of {'date': date, 'value': value}
                    
                    # Additional Lab Values
                    ldh_new = None
                    alkaline_phosphatase = None
                    magnesium = None
                    phosphorus = None
                    
                    # Reproductive Health
                    pregnancy_test_date = None
                    pregnancy_test_result_value = None
                    contraceptive_use = None
                    
                    # Consent and Support
                    consent_capability = None
                    caregiver_availability_status = None
                    
                    # Mental Health and Substance Use
                    no_mental_health_disorder_status = None
                    no_substance_use_status = None
                    substance_use_details = None
                    
                    # Geographic Exposure
                    no_geographic_exposure_risk = None
                    geographic_exposure_risk_details = None
                    
                    for observation in data['observations']:
                        obs_code = observation.get('code', {})
                        obs_text = obs_code.get('text', '').lower()
                        value_number = observation.get('valueQuantity', {}).get('value') if observation.get('valueQuantity') else None
                        value_codeable = observation.get('valueCodeableConcept', {}).get('text') if observation.get('valueCodeableConcept') else None
                        
                        # Get LOINC code for lab mapping
                        loinc_code = None
                        if obs_code.get('coding'):
                            for coding in obs_code['coding']:
                                if coding.get('system') == 'http://loinc.org':
                                    loinc_code = coding.get('code')
                                    break
                        
                        # Map LOINC codes to blood count fields
                        if loinc_code == '718-7':  # Hemoglobin
                            hemoglobin_g_dl = value_number
                        elif loinc_code == '4544-3':  # Hematocrit
                            hematocrit_percent = value_number
                        elif loinc_code == '6690-2':  # WBC
                            wbc_count = value_number
                        elif loinc_code == '789-8':  # RBC
                            rbc_count = value_number
                        elif loinc_code == '777-3':  # Platelets
                            platelet_count = value_number
                        elif loinc_code == '751-8':  # ANC
                            anc_count = value_number
                        elif loinc_code == '731-0':  # ALC
                            alc_count = value_number
                        elif loinc_code == '742-7':  # AMC
                            amc_count = value_number
                        # Kidney function
                        elif loinc_code == '17861-6' or loinc_code == '2000-8':  # Serum Calcium / Calcium
                            serum_calcium = value_number
                            calcium = value_number
                        elif loinc_code == '2160-0':  # Serum Creatinine
                            serum_creatinine = value_number
                        elif loinc_code == '2164-2':  # Creatinine Clearance
                            creatinine_clearance = value_number
                        elif loinc_code == '33914-3':  # eGFR
                            egfr = value_number
                        elif loinc_code == '3094-0':  # BUN
                            bun = value_number
                        # Electrolytes
                        elif loinc_code == '2951-2':  # Sodium
                            sodium = value_number
                        elif loinc_code == '2823-3':  # Potassium
                            potassium = value_number
                        elif loinc_code == '19123-9':  # Magnesium
                            magnesium = value_number
                        # Liver function
                        elif loinc_code == '1975-2':  # Total Bilirubin
                            bilirubin_total = value_number
                        elif loinc_code == '1968-7':  # Direct Bilirubin
                            bilirubin_direct = value_number
                        elif loinc_code == '1742-6':  # ALT
                            alt = value_number
                        elif loinc_code == '1920-8':  # AST
                            ast = value_number
                        elif loinc_code == '6768-6':  # Alkaline Phosphatase
                            alkaline_phosphatase = value_number
                        elif loinc_code == '1751-7':  # Albumin
                            albumin = value_number
                        elif loinc_code == '2885-2':  # Total Protein
                            total_protein = value_number
                        # Other markers
                        elif loinc_code == '1754-1' or loinc_code == '48346-3':  # Beta-2 Microglobulin
                            beta2_microglobulin = value_number
                        elif loinc_code == '1988-5':  # C-Reactive Protein
                            c_reactive_protein = value_number
                        elif loinc_code == '4537-7' or loinc_code == '30341-2':  # ESR
                            esr = value_number
                        elif loinc_code == '2164-2' or loinc_code == '33558-8':  # Creatinine Clearance Rate
                            creatinine_clearance_rate = value_number
                        # Cardiac & Other
                        elif loinc_code == '10839-9' or loinc_code == '6598-7':  # Troponin
                            troponin = value_number
                        elif loinc_code == '42637-9':  # BNP
                            bnp = value_number
                        elif loinc_code == '2345-7':  # Glucose
                            glucose = value_number
                        elif loinc_code == '4548-4':  # HbA1c
                            hba1c = value_number
                        elif loinc_code == '2532-0':  # LDH
                            ldh = value_number
                        # Coagulation
                        elif loinc_code == '6301-6':  # INR
                            inr = value_number
                        elif loinc_code == '5902-2':  # PT
                            pt = value_number
                        elif loinc_code == '3173-2':  # PTT
                            ptt = value_number
                        # Tumor markers
                        elif loinc_code == '2039-6':  # CEA
                            cea = value_number
                        elif loinc_code == '25390-6':  # CA 19-9
                            ca19_9 = value_number
                        elif loinc_code == '2857-1':  # PSA
                            psa = value_number
                        # Behavior - Lifestyle
                        elif loinc_code == '72166-2':  # Smoking Status
                            smoking_status = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '63640-7':  # Pack Years
                            pack_years = value_number
                        elif loinc_code == '74013-4':  # Alcohol Use
                            alcohol_use = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '11286-7':  # Drinks per Week
                            drinks_per_week = value_number
                        elif loinc_code == '68516-4':  # Exercise Frequency
                            exercise_frequency = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '89555-7':  # Exercise Minutes per Week
                            exercise_minutes_per_week = value_number
                        elif loinc_code == '88365-2':  # Diet Type
                            diet_type = value_codeable
                        # Behavior - Sleep & Wellbeing
                        elif loinc_code == '93832-4':  # Sleep Hours per Night
                            sleep_hours_per_night = value_number
                        elif loinc_code == '93831-6':  # Sleep Quality
                            sleep_quality = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '73985-4':  # Stress Level
                            stress_level = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '93033-9':  # Social Support
                            social_support = value_codeable[:50] if value_codeable else value_codeable
                        # Behavior - Socioeconomic
                        elif loinc_code == '74165-2':  # Employment Status
                            employment_status = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '82589-3':  # Education Level
                            education_level = value_codeable
                        elif loinc_code == '45404-1':  # Marital Status
                            marital_status = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '76513-1':  # Insurance Type
                            insurance_type = value_codeable
                        elif loinc_code == '63512-8':  # Number of Dependents
                            number_of_dependents = value_number
                        elif loinc_code == '77243-3':  # Annual Household Income
                            annual_household_income = value_number
                        # Cancer Assessment Fields
                        elif loinc_code == '89247-1':  # ECOG Performance Status
                            if observation.get('effectiveDateTime'):
                                ecog_assessment_date = observation['effectiveDateTime'][:10]
                            # Capture integer score (0-4) for Measurement row creation in Phase 2
                            if observation.get('valueInteger') is not None:
                                value_number = float(observation['valueInteger'])
                            elif value_codeable:
                                # Some implementations encode as "0", "1", etc. in text
                                try:
                                    value_number = float(value_codeable)
                                except (ValueError, TypeError):
                                    pass
                        elif loinc_code == '85337-4':  # Test Methodology
                            test_methodology = value_codeable[:50] if value_codeable else value_codeable
                            # Also check if this is Oncotype DX score
                            if value_number is not None:
                                oncotype_dx_score = value_number
                        elif loinc_code == '31208-2':  # Specimen Source
                            test_specimen_type = value_codeable[:50] if value_codeable else value_codeable
                            if observation.get('effectiveDateTime'):
                                test_date = observation['effectiveDateTime'][:10]
                        elif loinc_code == '69548-6':  # Test Interpretation
                            report_interpretation = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '16112-5':  # Estrogen Receptor (ER) — mCODE tumor marker
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                er_status = value_concept.get('text') or (value_concept.get('coding') or [{}])[0].get('display')
                        elif loinc_code == '16113-3':  # Progesterone Receptor (PR) — mCODE tumor marker
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                pr_status = value_concept.get('text') or (value_concept.get('coding') or [{}])[0].get('display')
                        elif loinc_code == '48676-1':  # HER2 [Interpretation] in Tissue — mCODE tumor marker
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                her2_status = value_concept.get('text') or (value_concept.get('coding') or [{}])[0].get('display')
                        elif loinc_code == '42804-5':  # Therapy Intent
                            obs_date = observation.get('effectiveDateTime', '')[:10] if observation.get('effectiveDateTime') else None
                            therapy_intent_observations.append({'date': obs_date, 'value': value_codeable})
                            if not therapy_intent:  # Keep first for backwards compatibility
                                therapy_intent = value_codeable[:50] if value_codeable else value_codeable
                        elif loinc_code == '91379-3':  # Reason for Discontinuation
                            obs_date = observation.get('effectiveDateTime', '')[:10] if observation.get('effectiveDateTime') else None
                            discontinuation_observations.append({'date': obs_date, 'value': value_codeable})
                            if not reason_for_discontinuation:  # Keep first for backwards compatibility
                                reason_for_discontinuation = value_codeable
                        # Additional Lab Values
                        elif loinc_code == '14804-9':  # LDH
                            ldh_new = value_number
                        elif loinc_code == '6768-6':  # Alkaline Phosphatase
                            alkaline_phosphatase = value_number
                        elif loinc_code == '2601-3':  # Magnesium
                            magnesium = value_number
                        elif loinc_code == '2777-1':  # Phosphorus
                            phosphorus = value_number
                        # Reproductive Health
                        elif loinc_code == '2106-3':  # Pregnancy Test
                            pregnancy_test_result_value = value_codeable[:50] if value_codeable else value_codeable
                            if observation.get('effectiveDateTime'):
                                pregnancy_test_date = observation['effectiveDateTime'][:10]
                        elif loinc_code == '8659-8':  # Contraceptive Use
                            contraceptive_use = value_codeable and value_codeable.lower() in ['yes', 'true']
                        # Consent and Support
                        elif loinc_code == '75985-6':  # Ability to Consent
                            consent_capability = value_codeable and value_codeable.lower() in ['yes', 'true']
                        elif loinc_code == '74014-2':  # Caregiver Availability
                            caregiver_availability_status = value_codeable and value_codeable.lower() in ['yes', 'true']
                        # Mental Health and Substance Use
                        elif loinc_code == '75618-3':  # Mental Health Disorders
                            no_mental_health_disorder_status = value_codeable and value_codeable.lower() in ['no', 'false']
                        elif loinc_code == '74204-0':  # Non-prescription Drug Use
                            no_substance_use_status = value_codeable and value_codeable.lower() in ['no', 'false']
                            if observation.get('note'):
                                substance_use_details = observation['note'][0].get('text')
                        # Geographic Exposure
                        elif loinc_code == '82593-5':  # Geographic/Environmental Exposure Risk
                            no_geographic_exposure_risk = value_codeable and value_codeable.lower() in ['no', 'false']
                            if observation.get('note'):
                                geographic_exposure_risk_details = observation['note'][0].get('text')
                        
                        # Check for lymph node status — exclude TNM N-stage LOINC (21906-3)
                        # which carries AJCC notation, not a binary status
                        elif ('lymph node' in obs_text or 'lymph nodes' in obs_text) and loinc_code != '21906-3':
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                raw = (value_concept.get('text') or
                                       (value_concept.get('coding') or [{}])[0].get('display'))
                                if raw:
                                    lymph_node_status = raw[:50]

                        # Check for metastasis status — exclude TNM M-stage LOINCs (21907-1, 21901-4)
                        # which carry AJCC notation and are routed to distant_metastasis_stage below
                        elif ('metastasis' in obs_text or 'metastases' in obs_text) and loinc_code not in ('21907-1', '21901-4'):
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                raw = (value_concept.get('text') or
                                       (value_concept.get('coding') or [{}])[0].get('display'))
                                if raw:
                                    metastasis_status = raw[:50]

                        # TNM staging fields
                        if obs_text == 'tumor stage' or loinc_code == '21905-5':
                            tumor_stage = (observation.get('valueCodeableConcept') or {}).get('text')
                        elif obs_text == 'nodes stage' or loinc_code == '21906-3':
                            nodes_stage = (observation.get('valueCodeableConcept') or {}).get('text')
                        elif obs_text == 'distant metastasis stage' or loinc_code in ('21901-4', '21907-1'):
                            distant_metastasis_stage = (observation.get('valueCodeableConcept') or {}).get('text')
                        elif obs_text == 'staging modality':
                            staging_modalities = observation.get('valueString')
                        elif loinc_code == '21908-9':
                            # mCODE TNM clinical stage group — valueCodeableConcept e.g. "Stage 2B"
                            # or Synthea AJCC form "American Joint Committee on Cancer stage IA (qualifier value)"
                            val_concept = observation.get('valueCodeableConcept') or {}
                            stage_text = val_concept.get('text') or (val_concept.get('coding') or [{}])[0].get('display', '')
                            if stage_text:
                                # Skip optional "group" word: "stage group IIB" → "IIB"
                                _m = re.search(r'\bstage\s+(?:group\s+)?(\S+)', stage_text, re.IGNORECASE)
                                stage = _m.group(1).rstrip(')') if _m else stage_text
                        elif 'recist' in obs_text:
                            val = observation.get('valueBoolean')
                            if val is not None:
                                measurable_disease_by_recist_status = val
                        elif 'bone only metastasis' in obs_text or loinc_code == '44667-4':
                            val = observation.get('valueBoolean')
                            if val is not None:
                                bone_only_metastasis_status = val
                        elif 'clonal bone marrow b lymphocyte' in obs_text or loinc_code == '85319-5':
                            if observation.get('valueQuantity'):
                                clonal_bone_marrow_b_lymphocytes = observation['valueQuantity'].get('value')
                        
                        # Check for ER status
                        elif 'estrogen receptor' in obs_text or obs_text == 'er':
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    er_status = value_concept['text']
                                elif value_concept.get('coding'):
                                    er_status = value_concept['coding'][0].get('display')
                        
                        # Check for PR status
                        elif 'progesterone receptor' in obs_text or obs_text == 'pr':
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    pr_status = value_concept['text']
                                elif value_concept.get('coding'):
                                    pr_status = value_concept['coding'][0].get('display')
                        
                        # Check for HER2 status
                        elif 'her2' in obs_text or 'her-2' in obs_text:
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    her2_status = value_concept['text']
                                elif value_concept.get('coding'):
                                    her2_status = value_concept['coding'][0].get('display')
                        
                        # Check for Ki67
                        elif 'ki67' in obs_text or 'ki-67' in obs_text:
                            if observation.get('valueQuantity'):
                                ki67_index = observation['valueQuantity'].get('value')
                        
                        # Check for PD-L1
                        elif 'pd-l1' in obs_text or 'pdl1' in obs_text:
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    pdl1_status = value_concept['text']
                                elif value_concept.get('coding'):
                                    pdl1_status = value_concept['coding'][0].get('display')
                            # Check for PD-L1 percentage in component
                            if observation.get('component'):
                                for component in observation['component']:
                                    comp_text = component.get('code', {}).get('text', '').lower()
                                    if 'percentage' in comp_text or 'tumor cells' in comp_text:
                                        if component.get('valueQuantity'):
                                            pdl1_percentage = component['valueQuantity'].get('value')
                        
                        # Check for genetic mutations (component-based observations)
                        elif 'gene' in obs_text and 'mutation' in obs_text:
                            mutation_data = {
                                'gene': None,
                                'mutation': None,
                                'origin': None,
                                'interpretation': None
                            }
                            
                            # Get interpretation from main valueCodeableConcept
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    mutation_data['interpretation'] = value_concept['text']
                                elif value_concept.get('coding'):
                                    mutation_data['interpretation'] = value_concept['coding'][0].get('display')
                            
                            # Extract gene, mutation, and origin from components
                            if observation.get('component'):
                                for component in observation['component']:
                                    comp_code = component.get('code', {})
                                    comp_text = comp_code.get('text', '').lower()
                                    
                                    if 'gene' in comp_text:
                                        if component.get('valueCodeableConcept'):
                                            mutation_data['gene'] = component['valueCodeableConcept'].get('text')
                                    elif 'mutation' in comp_text or 'dna change' in comp_text:
                                        if component.get('valueCodeableConcept'):
                                            mutation_data['mutation'] = component['valueCodeableConcept'].get('text')
                                    elif 'origin' in comp_text or 'source class' in comp_text:
                                        if component.get('valueCodeableConcept'):
                                            value = component['valueCodeableConcept'].get('text')
                                            if value:
                                                mutation_data['origin'] = value
                                            elif component['valueCodeableConcept'].get('coding'):
                                                mutation_data['origin'] = component['valueCodeableConcept']['coding'][0].get('display')
                            
                            # Only add if we have at least gene and mutation
                            if mutation_data['gene'] and mutation_data['mutation']:
                                genetic_mutations.append(mutation_data)
                    
                    # Pre-fetch all existing Measurements for this person so the
                    # upsert check below is a dict lookup instead of one SELECT
                    # per observation (48 round-trips → 1 round-trip).
                    _t_mfetch = _time.monotonic()
                    _existing_measurements: dict[tuple, object] = {
                        (m.measurement_concept_id, m.measurement_date, m.measurement_source_value): m
                        for m in Measurement.objects.filter(person=person)
                    }
                    logger.info(
                        "TIMING patient=%s phase=measurements_bulk_fetch elapsed=%.3fs existing=%d",
                        _timing_hash, _time.monotonic() - _t_mfetch, len(_existing_measurements),
                    )

                    # Accumulate new Measurement objects for bulk_create after the loop
                    # (one INSERT instead of one per observation = eliminates ~48 round-trips).
                    _pending_measurements: list = []
                    _pending_provenances: list = []  # (measurement, prov_source, prov_user_id, prov_reason)
                    _t_loop_start = _time.monotonic()
                    _obs_idx = 0

                    for observation in data['observations']:
                        _obs_idx += 1
                        _t_obs = _time.monotonic()
                        obs_date = None
                        if observation.get('effectiveDateTime'):
                            try:
                                raw_dt = observation['effectiveDateTime']
                                if 'T' in raw_dt:
                                    obs_date = datetime.fromisoformat(raw_dt)
                                    if obs_date.tzinfo is None:
                                        obs_date = timezone.make_aware(obs_date)
                                else:
                                    obs_date = timezone.make_aware(datetime.strptime(raw_dt, '%Y-%m-%d'))
                            except (ValueError, TypeError):
                                continue

                        if not obs_date:
                            continue
                        
                        # Get observation name and value
                        obs_code = observation.get('code', {})
                        obs_name = obs_code.get('text', '')
                        if not obs_name and obs_code.get('coding'):
                            obs_name = obs_code['coding'][0].get('display', '')
                        
                        # Get value
                        value_number = None
                        value_string = None
                        unit = None
                        qualifier_source_value = None
                        
                        if observation.get('valueQuantity'):
                            value_qty = observation['valueQuantity']
                            value_number = value_qty.get('value')
                            unit = value_qty.get('unit')
                        elif observation.get('valueInteger') is not None:
                            # FHIR integer type — used for ECOG (0-4), Karnofsky, grades, etc.
                            value_number = float(observation['valueInteger'])
                        elif observation.get('valueCodeableConcept'):
                            value_concept = observation['valueCodeableConcept']
                            if value_concept.get('text'):
                                value_string = value_concept['text']
                            elif value_concept.get('coding'):
                                value_string = value_concept['coding'][0].get('display')
                        elif observation.get('valueBoolean') is not None:
                            # FHIR boolean — store as 1/0 in value_as_number for easy extraction
                            value_number = 1.0 if observation['valueBoolean'] else 0.0
                        elif observation.get('valueString') is not None:
                            # FHIR string — e.g. ISS/R-ISS stage, disease progression
                            # status. Without this branch the value is dropped and
                            # downstream derivation (stage, etc.) finds an empty row.
                            value_string = str(observation['valueString'])

                        # value_as_string is CharField(max_length=60); truncate whatever
                        # source set it (valueString or valueCodeableConcept text/display).
                        if value_string is not None:
                            value_string = value_string[:60]

                        # Find measurement concept — LOINC lookup first (FHIR-06/07/08),
                        # fall back to name-based, then generic lab concept.
                        measurement_concept = None
                        obs_loinc = None
                        for _c in obs_code.get('coding', []):
                            if _c.get('system') == 'http://loinc.org':
                                obs_loinc = _c.get('code')
                                break

                        # BP panel (85354-9) — expand components to individual measurements
                        # for systolic (8480-6) and diastolic (8462-4) so refresh_patient_record
                        # can derive BP from the Measurement table.
                        if obs_loinc == '85354-9':
                            _bp_type = _concept_lab_type or _concept_generic_lab
                            _bp_any_written = False
                            for _comp in observation.get('component', []):
                                _comp_loinc = next(
                                    (c.get('code') for c in _comp.get('code', {}).get('coding', [])
                                     if c.get('system') == 'http://loinc.org'),
                                    None
                                )
                                if not _comp_loinc:
                                    continue
                                _comp_concept = _cc_by_loinc(_comp_loinc)
                                if not _comp_concept:
                                    continue
                                _comp_value = (_comp.get('valueQuantity') or {}).get('value')
                                _comp_unit = (_comp.get('valueQuantity') or {}).get('unit')
                                _comp_key = (_comp_concept.pk, obs_date.date(), _comp_loinc)
                                if _comp_key not in _existing_measurements:
                                    _cm = Measurement(
                                        measurement_id=0,
                                        person=person,
                                        measurement_concept=_comp_concept,
                                        measurement_date=obs_date.date(),
                                        measurement_datetime=obs_date,
                                        measurement_type_concept=_bp_type,
                                        value_as_number=_comp_value,
                                        measurement_source_value=_comp_loinc,
                                        unit_source_value=_comp_unit[:50] if _comp_unit else None,
                                    )
                                    _cm._skip_patient_record_refresh = True
                                    _existing_measurements[_comp_key] = _cm
                                    _pending_measurements.append(_cm)
                                    _bp_any_written = True
                            if _bp_any_written:
                                continue  # skip writing a measurement for the panel itself
                            # No component concepts were found — fall through and write the
                            # panel row itself using measurement_source_value='85354-9' as
                            # a fallback so the observation is not silently discarded.

                        if obs_loinc:
                            measurement_concept = _cc_by_loinc(obs_loinc)
                        if not measurement_concept and obs_name:
                            measurement_concept = _cc_by_name(obs_name[:50])
                        if not measurement_concept:
                            # Use pre-hoisted generic lab test concept if not found
                            measurement_concept = _concept_generic_lab

                        if measurement_concept:
                            # Use pre-hoisted Lab type concept (32856 = Lab)
                            type_concept = _concept_lab_type or measurement_concept

                            # Use LOINC code as source_value when available — it's short,
                            # unique, and avoids collisions from truncating long display names.
                            source_value = obs_loinc if obs_loinc else obs_name[:50]
                            # LOINC 21889-1 is officially Size Tumor.  A
                            # legacy lymph-node feed may reuse that code, but
                            # only its explicit text/context can authorize the
                            # separate lymph-node projection.
                            if obs_loinc == '21889-1' and 'lymph node' in obs_name.lower():
                                qualifier_source_value = 'lymph-node'
                            _mkey = (
                                measurement_concept.pk if measurement_concept else None,
                                obs_date.date(),
                                source_value,
                            )
                            existing_m = _existing_measurements.get(_mkey)
                            if existing_m:
                                # Only UPDATE if value actually changed — avoids
                                # pointless writes on every re-import of the same bundle.
                                if (existing_m.value_as_number != value_number
                                        or existing_m.value_as_string != value_string):
                                    existing_m.value_as_number = value_number
                                    existing_m.value_as_string = value_string
                                    existing_m.qualifier_source_value = qualifier_source_value
                                    existing_m._skip_patient_record_refresh = True
                                    existing_m.save()
                            else:
                                _m = Measurement(
                                    measurement_id=0,  # placeholder — batch-allocated before bulk_create
                                    person=person,
                                    measurement_concept=measurement_concept,
                                    measurement_date=obs_date.date(),
                                    measurement_datetime=obs_date,
                                    measurement_type_concept=type_concept,
                                    value_as_number=value_number,
                                    value_as_string=value_string,
                                    measurement_source_value=source_value,
                                    unit_source_value=unit[:50] if unit else None,
                                    qualifier_source_value=qualifier_source_value,
                                )
                                _m._skip_patient_record_refresh = True
                                # Keep the dict current so duplicate observations
                                # in the same patient don't re-insert the same row.
                                _existing_measurements[_mkey] = _m
                                _pending_measurements.append(_m)
                                if prov_source:
                                    _pending_provenances.append((_m, prov_source, prov_user_id, prov_reason))
                        # Log slow observations (>0.5s) to catch future bottlenecks
                        _t_obs_elapsed = _time.monotonic() - _t_obs
                        if _t_obs_elapsed > 0.5:
                            logger.info(
                                "TIMING patient=%s obs=%d/%d name=%.30s elapsed=%.3fs",
                                _timing_hash, _obs_idx, len(data['observations']),
                                obs_name, _t_obs_elapsed,
                            )

                    # Bulk-insert all new Measurements in one round-trip instead of
                    # one INSERT per observation (~48 round-trips → 1 round-trip).
                    if _pending_measurements:
                        # Batch-allocate PKs from the sequence — race-safe under concurrent uploads.
                        _m_ids = _next_pk_batch(Measurement, 'measurement_id', len(_pending_measurements))
                        for _pending_m, _mid in zip(_pending_measurements, _m_ids):
                            _pending_m.measurement_id = _mid
                        _t_bulk_insert = _time.monotonic()
                        try:
                            Measurement.objects.bulk_create(_pending_measurements)
                            for _bm in _pending_measurements:
                                _pt_measurement_ids.append(_bm.measurement_id)
                            for (_bm, _psrc, _puid, _preason) in _pending_provenances:
                                _record_provenance(_bm, _psrc, _puid, modification_reason=_preason, organization=get_request_org(request))
                        except Exception as _bcex:
                            logger.warning(
                                '{"event": "measurement_bulk_create_failed", "count": %d, "error": "%s"}',
                                len(_pending_measurements), _bcex,
                            )
                        logger.info(
                            "TIMING patient=%s phase=measurements_bulk_insert elapsed=%.3fs count=%d",
                            _timing_hash, _time.monotonic() - _t_bulk_insert, len(_pending_measurements),
                        )

                    # --- Write OMOP Observation rows for clinical assessments ---
                    # These are non-lab FHIR observations (ECOG, TNM staging, smoking status,
                    # treatment response) that the patient_record_service extractors read from
                    # the OMOP Observation table, not the Measurement table.
                    _ASSESSMENT_LOINC = {
                        '89247-1',  # ECOG Performance Status
                        '89243-0',  # Karnofsky Performance Status
                        '21908-9',  # Stage group.clinical Cancer (TNM overall)
                        '21905-5',  # Primary tumor.clinical [Class] Cancer (T)
                        '21906-3',  # Regional lymph nodes.clinical [Class] Cancer (N)
                        '21901-4',  # Distant metastasis.clinical [Class] Cancer (M)
                        '93832-4',  # Sleep duration
                    }
                    _ASSESSMENT_SNOMED = {
                        # Smoking/tobacco status
                        '266919005',  # Never smoked tobacco
                        '8517006',    # Ex-smoker
                        '77176002',   # Smoker
                        # Treatment response (RECIST)
                        '182840001',  # Complete response
                        '182841002',  # Partial response
                        '182843004',  # Stable disease
                        '182842009',  # Progressive disease
                    }
                    _existing_obs_keys = {
                        (o.observation_concept_id, o.observation_date, o.observation_source_value)
                        for o in Observation.objects.filter(person=person)
                    }
                    _pending_observations = []
                    for _obs_fhir in data['observations']:
                        _obs_fhir_code = _obs_fhir.get('code', {})
                        _obs_date_str = _obs_fhir.get('effectiveDateTime') or _obs_fhir.get('effectivePeriod', {}).get('start')
                        if not _obs_date_str:
                            continue
                        try:
                            _obs_dt = datetime.fromisoformat(_obs_date_str[:10])
                        except (ValueError, TypeError):
                            continue
                        _obs_date_only = _obs_dt.date()

                        _obs_concept = None
                        _obs_src = None

                        # Check for LOINC-coded assessment observations
                        for _coding in _obs_fhir_code.get('coding', []):
                            _sys = _coding.get('system', '')
                            _code = _coding.get('code', '')
                            if 'loinc.org' in _sys and _code in _ASSESSMENT_LOINC:
                                _obs_concept = _cc_by_loinc(_code)
                                _obs_src = _code
                                break
                            if 'snomed' in _sys.lower() and _code in _ASSESSMENT_SNOMED:
                                _obs_concept = _cc_by_vocab('SNOMED', _code)
                                _obs_src = _code
                                break

                        if not _obs_concept:
                            continue

                        _obs_key = (_obs_concept.pk, _obs_date_only, _obs_src)
                        if _obs_key in _existing_obs_keys:
                            continue

                        # Extract value
                        _obs_val_num = None
                        _obs_val_str = None
                        if _obs_fhir.get('valueQuantity'):
                            _obs_val_num = _obs_fhir['valueQuantity'].get('value')
                        elif _obs_fhir.get('valueInteger') is not None:
                            _obs_val_num = float(_obs_fhir['valueInteger'])
                        elif _obs_fhir.get('valueCodeableConcept'):
                            _vc = _obs_fhir['valueCodeableConcept']
                            _obs_val_str = _vc.get('text') or (_vc.get('coding') or [{}])[0].get('display')
                            if _obs_val_str and len(_obs_val_str) > 60:
                                _obs_val_str = _obs_val_str[:60]
                        elif _obs_fhir.get('valueBoolean') is not None:
                            _obs_val_num = 1.0 if _obs_fhir['valueBoolean'] else 0.0

                        _new_obs = Observation(
                            observation_id=0,  # allocated below
                            person=person,
                            observation_concept=_obs_concept,
                            observation_date=_obs_date_only,
                            observation_datetime=timezone.make_aware(_obs_dt) if _obs_dt.tzinfo is None else _obs_dt,
                            observation_type_concept=_concept_ehr_type or _obs_concept,
                            value_as_number=_obs_val_num,
                            value_as_string=_obs_val_str,
                            observation_source_value=_obs_src[:50] if _obs_src else None,
                        )
                        _new_obs._skip_patient_record_refresh = True
                        _existing_obs_keys.add(_obs_key)
                        _pending_observations.append(_new_obs)

                    if _pending_observations:
                        _obs_ids = _next_pk_batch(Observation, 'observation_id', len(_pending_observations))
                        for _po, _oid in zip(_pending_observations, _obs_ids):
                            _po.observation_id = _oid
                        try:
                            Observation.objects.bulk_create(_pending_observations)
                            logger.info(
                                '{"event": "observations_written", "person_id": %d, "count": %d}',
                                person.person_id, len(_pending_observations),
                            )
                        except Exception as _obs_ex:
                            logger.warning(
                                '{"event": "observation_bulk_create_failed", "count": %d, "error": "%s"}',
                                len(_pending_observations), _obs_ex,
                            )

                    # Extract therapy information from MedicationStatement resources
                    therapy_lines = {}  # {line_number: {'regimen': name, 'start_date': date, 'end_date': date, 'outcome': outcome}}
                    
                    for medication in data.get('medications', []):
                        # Get therapy line from extension
                        therapy_line = None
                        therapy_outcome = None
                        
                        for ext in medication.get('extension', []):
                            if 'therapy-line' in ext.get('url', ''):
                                therapy_line = ext.get('valueInteger')
                            elif 'therapy-outcome' in ext.get('url', ''):
                                therapy_outcome = ext.get('valueString')
                        
                        if therapy_line is None:
                            continue
                        
                        # Check if this is a regimen (parent) or individual drug (partOf).
                        # Only treat as sub-resource when partOf references a MedicationStatement;
                        # a Procedure/ reference (radiation 1L + maintenance) should NOT suppress
                        # processing of the maintenance MedicationStatement.
                        _part_of = medication.get('partOf', [])
                        _is_med_sub_resource = any(
                            ref.get('reference', '').startswith('MedicationStatement/')
                            for ref in _part_of
                        )
                        if not _is_med_sub_resource:
                            # This is the named regimen
                            regimen_name = medication.get('medicationCodeableConcept', {}).get('text', '')
                            effective_period = medication.get('effectivePeriod', {})
                            start_date = effective_period.get('start')
                            end_date = effective_period.get('end')
                            # Also support effectiveDateTime for backwards compatibility
                            if not start_date:
                                start_date = medication.get('effectiveDateTime')
                            # Extract HemOnc concept_id from coding if present
                            hemonc_concept_id = None
                            for _coding in medication.get('medicationCodeableConcept', {}).get('coding', []):
                                if _coding.get('system') == 'http://ohdsi.org/omop/HemOnc':
                                    try:
                                        hemonc_concept_id = int(_coding.get('code', ''))
                                    except (ValueError, TypeError):
                                        pass

                            if therapy_line not in therapy_lines:
                                therapy_lines[therapy_line] = {
                                    'regimen': regimen_name,
                                    'start_date': start_date,
                                    'end_date': end_date,
                                    'hemonc_concept_id': hemonc_concept_id,
                                }
                                if therapy_outcome is not None:
                                    therapy_lines[therapy_line]['outcome'] = therapy_outcome
                            else:
                                therapy_lines[therapy_line]['regimen'] = regimen_name
                                if start_date:
                                    therapy_lines[therapy_line]['start_date'] = start_date
                                if end_date:
                                    therapy_lines[therapy_line]['end_date'] = end_date
                                if therapy_outcome is not None:
                                    therapy_lines[therapy_line]['outcome'] = therapy_outcome
                                if hemonc_concept_id:
                                    therapy_lines[therapy_line]['hemonc_concept_id'] = hemonc_concept_id
                    
                    # Map therapy lines to first/second/later fields
                    first_line_therapy = None
                    first_line_date = None
                    first_line_start_date = None
                    first_line_end_date = None
                    first_line_outcome = None
                    first_line_intent = None
                    first_line_discontinuation_reason = None
                    second_line_therapy = None
                    second_line_date = None
                    second_line_start_date = None
                    second_line_end_date = None
                    second_line_outcome = None
                    second_line_intent = None
                    second_line_discontinuation_reason = None
                    later_therapy = None
                    later_date = None
                    later_start_date = None
                    later_end_date = None
                    later_outcome = None
                    later_intent = None
                    later_discontinuation_reason = None
                    
                    if 1 in therapy_lines:
                        first_line_therapy = therapy_lines[1]['regimen']
                        if therapy_lines[1].get('start_date'):
                            try:
                                first_line_start_date = datetime.strptime(therapy_lines[1]['start_date'][:10], '%Y-%m-%d').date()
                                first_line_date = first_line_start_date  # Keep for backwards compatibility
                            except (ValueError, TypeError, IndexError):
                                pass
                        if therapy_lines[1].get('end_date'):
                            try:
                                first_line_end_date = datetime.strptime(therapy_lines[1]['end_date'][:10], '%Y-%m-%d').date()
                            except (ValueError, TypeError, IndexError):
                                pass
                        first_line_outcome = therapy_lines[1].get('outcome')
                    
                    if 2 in therapy_lines:
                        second_line_therapy = therapy_lines[2]['regimen']
                        if therapy_lines[2].get('start_date'):
                            try:
                                second_line_start_date = datetime.strptime(therapy_lines[2]['start_date'][:10], '%Y-%m-%d').date()
                                second_line_date = second_line_start_date  # Keep for backwards compatibility
                            except (ValueError, TypeError, IndexError):
                                pass
                        if therapy_lines[2].get('end_date'):
                            try:
                                second_line_end_date = datetime.strptime(therapy_lines[2]['end_date'][:10], '%Y-%m-%d').date()
                            except (ValueError, TypeError, IndexError):
                                pass
                        second_line_outcome = therapy_lines[2].get('outcome')
                    
                    # Map line 3 and 4 to "later" field (prioritize most recent)
                    if 4 in therapy_lines:
                        later_therapy = therapy_lines[4]['regimen']
                        if therapy_lines[4].get('start_date'):
                            try:
                                later_start_date = datetime.strptime(therapy_lines[4]['start_date'][:10], '%Y-%m-%d').date()
                                later_date = later_start_date  # Keep for backwards compatibility
                            except (ValueError, TypeError, IndexError):
                                pass
                        if therapy_lines[4].get('end_date'):
                            try:
                                later_end_date = datetime.strptime(therapy_lines[4]['end_date'][:10], '%Y-%m-%d').date()
                            except (ValueError, TypeError, IndexError):
                                pass
                        later_outcome = therapy_lines[4].get('outcome')
                    elif 3 in therapy_lines:
                        later_therapy = therapy_lines[3]['regimen']
                        if therapy_lines[3].get('start_date'):
                            try:
                                later_start_date = datetime.strptime(therapy_lines[3]['start_date'][:10], '%Y-%m-%d').date()
                                later_date = later_start_date  # Keep for backwards compatibility
                            except (ValueError, TypeError, IndexError):
                                pass
                        if therapy_lines[3].get('end_date'):
                            try:
                                later_end_date = datetime.strptime(therapy_lines[3]['end_date'][:10], '%Y-%m-%d').date()
                            except (ValueError, TypeError, IndexError):
                                pass
                        later_outcome = therapy_lines[3].get('outcome')
                    
                    # Match therapy intent and discontinuation observations to therapy lines by date
                    for intent_obs in therapy_intent_observations:
                        if intent_obs['date']:
                            intent_date = intent_obs['date']
                            # Match to first line
                            if first_line_start_date and intent_date == str(first_line_start_date):
                                first_line_intent = intent_obs['value']
                            # Match to second line
                            elif second_line_start_date and intent_date == str(second_line_start_date):
                                second_line_intent = intent_obs['value']
                            # Match to later line
                            elif later_start_date and intent_date == str(later_start_date):
                                later_intent = intent_obs['value']
                    
                    for disc_obs in discontinuation_observations:
                        if disc_obs['date']:
                            disc_date = disc_obs['date']
                            # Match to first line
                            if first_line_end_date and disc_date == str(first_line_end_date):
                                first_line_discontinuation_reason = disc_obs['value']
                            # Match to second line
                            elif second_line_end_date and disc_date == str(second_line_end_date):
                                second_line_discontinuation_reason = disc_obs['value']
                            # Match to later line
                            elif later_end_date and disc_date == str(later_end_date):
                                later_discontinuation_reason = disc_obs['value']
                    
                    # --- Write DrugExposure records for each therapy line ---
                    logger.info("TIMING patient=%s phase=measurements elapsed=%.1fs", _timing_hash, _time.monotonic() - _pt_start)
                    # PKs allocated on-demand via sequence — see next_pk() calls below.

                    def _looks_like_regimen_name(name):
                        name = (name or '').strip()
                        if not name:
                            return False
                        if any(sep in name for sep in ('/', '+', '(', ')')):
                            return True
                        if '-' in name:
                            return True
                        if len(name) <= 8 and name.upper() == name and any(ch.isalpha() for ch in name):
                            return True
                        # Short mixed-case acronyms (VRd, KRd, DaraRd) are
                        # regimen names, not generic drug names.
                        return len(name) <= 8 and sum(1 for ch in name if ch.isupper()) >= 2

                    def _coding_parts(codeable):
                        codeable = codeable or {}
                        coding = (codeable.get('coding') or [{}])[0]
                        return (
                            coding.get('system', ''),
                            coding.get('code', ''),
                            coding.get('display') or codeable.get('text') or '',
                        )

                    def _vocab_from_system(system):
                        system = (system or '').lower()
                        if 'rxnorm' in system:
                            return 'RxNorm'
                        if 'cvx' in system:
                            return 'CVX'
                        if 'loinc' in system:
                            return 'LOINC'
                        if 'snomed' in system:
                            return 'SNOMED'
                        return 'FHIR'

                    def _drug_concept_from_codeable(codeable):
                        system, code, display = _coding_parts(codeable)
                        vocabulary_id = _vocab_from_system(system)
                        concept = Concept.objects.filter(
                            vocabulary_id=vocabulary_id,
                            concept_code=code,
                        ).first() if code else None
                        if concept:
                            return concept
                        if vocabulary_id == 'RxNorm' and display:
                            concept = Concept.objects.filter(
                                concept_name__icontains=display,
                                domain__domain_id='Drug',
                            ).first()
                            if concept:
                                return concept
                            try:
                                concept = _rxnav_resolve_drug(display)
                            except Exception as rxnav_exc:
                                logger.warning(
                                    '{"event": "rxnav_resolve_failed", "drug": "%s", "error": "%s"}',
                                    display, rxnav_exc,
                                )
                            if concept:
                                return concept
                        # Never mint under the licensed source vocabulary —
                        # quarantine under HK-Drug and record the gap (#236).
                        # Returns None only when there is no code AND no display;
                        # callers must skip the write in that case (drug_concept
                        # is NOT NULL on DrugExposure).
                        return get_or_create_quarantine_drug(
                            source_vocabulary_id=vocabulary_id,
                            concept_code=code,
                            concept_name=display,
                            source_system='fhir-upload',
                        )

                    for lot_num, lot_data in sorted(therapy_lines.items()):
                        try:
                            with transaction.atomic():
                                lot_start = None
                                lot_end = None
                                if lot_data.get('start_date'):
                                    lot_start = datetime.strptime(lot_data['start_date'][:10], '%Y-%m-%d').date()
                                if lot_data.get('end_date'):
                                    lot_end = datetime.strptime(lot_data['end_date'][:10], '%Y-%m-%d').date()

                                regimen_name = lot_data.get('regimen', '')
                                # Resolution ladder (issue #236 — namespace hygiene):
                                #   1. Validate an inbound HemOnc concept_id — must be a
                                #      currently-valid standard HemOnc Regimen.
                                #   2. Match a real HemOnc regimen by name/synonym.
                                #   3. Quarantine under HK-Regimen (never mint under HemOnc).
                                _hemonc_cid = lot_data.get('hemonc_concept_id')
                                regimen_concept = None
                                _regimen_source_concept = None
                                if _hemonc_cid:
                                    # Validation must hit the DB, not the
                                    # process-level concept cache — a stale
                                    # cached row could pass/fail validation
                                    # against outdated invalid_reason /
                                    # standard_concept state.
                                    _candidate = Concept.objects.filter(
                                        concept_id=_hemonc_cid,
                                    ).first()
                                    if validate_hemonc_regimen(_candidate):
                                        regimen_concept = _candidate
                                        _regimen_source_concept = _candidate
                                    else:
                                        logger.warning(
                                            '{"event": "hemonc_concept_id_rejected", "concept_id": %s, "patient": "%s"}',
                                            _hemonc_cid, _timing_hash,
                                        )
                                if regimen_concept is None and regimen_name:
                                    regimen_concept = match_hemonc_regimen_by_name(regimen_name)
                                if regimen_concept is None and regimen_name:
                                    if _looks_like_regimen_name(regimen_name):
                                        regimen_concept = get_or_create_quarantine_regimen(
                                            regimen_name, source_system='fhir-upload',
                                        )
                                    else:
                                        # Plain drug name — generic drug lookup, then
                                        # RxNav fallback.
                                        regimen_concept = Concept.objects.filter(
                                            concept_name__icontains=regimen_name,
                                            domain__domain_id='Drug',
                                        ).first()
                                        if regimen_concept is None:
                                            try:
                                                regimen_concept = _rxnav_resolve_drug(regimen_name)
                                            except Exception as rxnav_exc:
                                                logger.warning(
                                                    '{"event": "rxnav_resolve_failed", "drug": "%s", "error": "%s"}',
                                                    regimen_name, rxnav_exc,
                                                )
                                # Last resort: quarantine the unmatched name under
                                # HK-Drug and record the gap.  Never pick an
                                # arbitrary Drug-domain concept — that fabricates
                                # a wrong clinical link (#236).
                                if regimen_concept is None and regimen_name:
                                    regimen_concept = get_or_create_quarantine_drug(
                                        source_vocabulary_id='unknown',
                                        concept_code='',
                                        concept_name=regimen_name,
                                        source_system='fhir-upload',
                                    )
                                if regimen_concept is None:
                                    # No name and no valid inbound concept_id —
                                    # nothing to resolve, quarantine, or write.
                                    logger.warning(
                                        '{"event": "lot_write_skipped", "reason": "unresolvable_regimen", "lot_num": %d, "patient": "%s"}',
                                        lot_num, _timing_hash,
                                    )
                                    continue
                                drug_type_concept = _concept_drug_type or regimen_concept

                                # Upsert DrugExposure: skip if same person+regimen+start already exists
                                _de = DrugExposure.objects.filter(
                                    person=person,
                                    drug_source_value=(lot_data.get('regimen') or '')[:50],
                                    drug_exposure_start_date=lot_start,
                                ).first()
                                if _de is None:
                                    _de = DrugExposure(
                                        drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
                                        person=person,
                                        drug_concept=regimen_concept,
                                        drug_exposure_start_date=lot_start,
                                        drug_exposure_end_date=lot_end,
                                        drug_type_concept=drug_type_concept,
                                        drug_source_value=(lot_data.get('regimen') or '')[:50],
                                    )
                                    _de._skip_patient_record_refresh = True
                                    _de.save()
                                    _pt_drug_exposure_ids.append(_de.drug_exposure_id)
                                    if prov_source:
                                        _record_provenance(_de, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))

                                # Episode + EpisodeEvent + per-line outcome via
                                # the shared LOT writer so CDM tagging and the
                                # idempotency key match every other path, and the
                                # outcome lands in OMOP (LOT-{n}-outcome
                                # Observation) as the source of truth. The direct
                                # PatientRecord outcome patch below is retained as
                                # a belt-and-suspenders for the no-episode edge.
                                _ep_result = upsert_therapy_line_episode(
                                    person,
                                    line_number=lot_num,
                                    regimen_concept=regimen_concept,
                                    regimen_source_concept=_regimen_source_concept,
                                    start_date=lot_start,
                                    end_date=lot_end,
                                    drug_exposure_ids=[_de.drug_exposure_id],
                                    outcome=lot_data.get('outcome'),
                                    today=datetime.now().date(),
                                )
                                if _ep_result.created:
                                    _pt_episode_ids.append(_ep_result.episode.episode_id)
                                _pt_episode_event_ids.extend(_ep_result.event_ids)
                        except Exception as _e:
                            logger.warning('{"event": "drug_exposure_write_failed", "lot_num": %d, "error_type": "%s", "patient": "%s"}',
                                           lot_num, type(_e).__name__, _timing_hash)

                    # --- Write supplemental MedicationRequest and Immunization rows ---
                    _existing_drug_keys = {
                        (d.drug_source_value, d.drug_exposure_start_date)
                        for d in DrugExposure.objects.filter(person=person)
                    }

                    def _write_drug_exposure(codeable, start_str, end_str=None, sig=None, route_source_value=None):
                        if not start_str:
                            return
                        try:
                            start_date = datetime.fromisoformat(start_str[:10]).date()
                        except (ValueError, TypeError):
                            return
                        end_date = None
                        if end_str:
                            try:
                                end_date = datetime.fromisoformat(end_str[:10]).date()
                            except (ValueError, TypeError):
                                end_date = None
                        _, code, display = _coding_parts(codeable)
                        source_value = (code or display or 'FHIR medication')[:50]
                        key = (source_value, start_date)
                        if key in _existing_drug_keys:
                            return
                        drug_concept = _drug_concept_from_codeable(codeable)
                        if drug_concept is None:
                            # Empty codeable (no code, no display) — nothing
                            # to resolve or quarantine; skip rather than write
                            # a NULL drug_concept (IntegrityError).
                            logger.warning(
                                '{"event": "drug_exposure_write_skipped", "reason": "unresolvable_concept", "patient": "%s"}',
                                _timing_hash,
                            )
                            return
                        drug_type_concept = _concept_drug_type or drug_concept
                        _de = DrugExposure(
                            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
                            person=person,
                            drug_concept=drug_concept,
                            drug_exposure_start_date=start_date,
                            drug_exposure_end_date=end_date,
                            drug_type_concept=drug_type_concept,
                            drug_source_value=source_value,
                            drug_source_concept=drug_concept,
                            sig=(sig or '')[:255],
                            route_source_value=route_source_value,
                        )
                        _de._skip_patient_record_refresh = True
                        _de.save()
                        _pt_drug_exposure_ids.append(_de.drug_exposure_id)
                        _existing_drug_keys.add(key)
                        if prov_source:
                            _record_provenance(_de, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))

                    for _med_request in data.get('medication_requests', []):
                        # Prefer inline medicationCodeableConcept; fall back to
                        # resolving a medicationReference to the linked Medication
                        # resource (Synthea and US Core R4 use this pattern).
                        _codeable = _med_request.get('medicationCodeableConcept')
                        if not _codeable:
                            _med_ref = (_med_request.get('medicationReference') or {}).get('reference', '')
                            _bare_ref = _med_ref[len('urn:uuid:'):] if _med_ref.startswith('urn:uuid:') else _med_ref.split('/')[-1]
                            _med_resource = medication_resources.get(_med_ref) or medication_resources.get(_bare_ref)
                            if _med_resource:
                                _codeable = _med_resource.get('code')
                        _write_drug_exposure(
                            _codeable,
                            _med_request.get('authoredOn') or (_med_request.get('effectivePeriod') or {}).get('start'),
                            (_med_request.get('effectivePeriod') or {}).get('end'),
                            ((_med_request.get('dosageInstruction') or [{}])[0].get('text') or ''),
                        )

                    for _immunization in data.get('immunizations', []):
                        _write_drug_exposure(
                            _immunization.get('vaccineCode'),
                            _immunization.get('occurrenceDateTime') or _immunization.get('recorded'),
                            route_source_value='VACCINE',
                        )

                    logger.info(
                        "TIMING patient=%s phase=supplemental_drugs elapsed=%.1fs count=%d",
                        _timing_hash, _time.monotonic() - _pt_start, len(_pt_drug_exposure_ids),
                    )

                    # --- Write DiagnosticReport rows into OMOP Observation ---
                    _existing_report_keys = {
                        (o.observation_source_value, o.observation_date, o.value_as_string, o.qualifier_source_value)
                        for o in Observation.objects.filter(person=person)
                    }
                    for _report in data.get('diagnostic_reports', []):
                        _report_date_str = _report.get('effectiveDateTime') or _report.get('issued')
                        if not _report_date_str:
                            continue
                        try:
                            _report_dt = datetime.fromisoformat(_report_date_str[:10])
                        except (ValueError, TypeError):
                            continue
                        _report_date = _report_dt.date()
                        _system, _code, _display = _coding_parts(_report.get('code'))
                        _vocab = _vocab_from_system(_system)
                        # Look up the reported code in its own vocabulary first
                        # (any licensed vocab, not just LOINC) — only genuinely
                        # unmapped codes go to quarantine.
                        _report_concept = None
                        if _code:
                            if _vocab == 'LOINC':
                                _report_concept = _cc_by_loinc(_code)
                            else:
                                _report_concept = _cc_by_vocab(_vocab, _code)
                        if _report_concept is None:
                            # Never mint under the licensed source vocabulary —
                            # quarantine under HK-Observation and record the gap (#236).
                            _report_concept = get_or_create_quarantine_observation(
                                source_vocabulary_id=_vocab,
                                concept_code=_code,
                                concept_name=_display or 'FHIR DiagnosticReport',
                                source_system='fhir-upload',
                            )
                        _value = (_report.get('conclusion') or _display or 'Diagnostic report')[:60]
                        _source = (_code or _display or 'DiagnosticReport')[:50]
                        _report_key = (_source, _report_date, _value, None)
                        if _report_key in _existing_report_keys:
                            continue
                        _obs = Observation(
                            observation_id=next_pk(Observation, 'observation_id'),
                            person=person,
                            observation_concept=_report_concept,
                            observation_date=_report_date,
                            observation_datetime=timezone.make_aware(_report_dt) if _report_dt.tzinfo is None else _report_dt,
                            observation_type_concept=_concept_ehr_type or _report_concept,
                            value_as_string=_value,
                            observation_source_value=_source,
                            observation_source_concept=_report_concept,
                        )
                        _obs._skip_patient_record_refresh = True
                        _obs.save()
                        _existing_report_keys.add(_report_key)
                        if prov_source:
                            _record_provenance(_obs, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))

                    logger.info(
                        "TIMING patient=%s phase=diagnostic_reports elapsed=%.1fs count=%d",
                        _timing_hash, _time.monotonic() - _pt_start, len(data.get('diagnostic_reports', [])),
                    )

                    # --- Write AllergyIntolerance rows into OMOP Observation ---
                    # Tagged with qualifier_source_value='ALLERGY' for the allergy
                    # list endpoint (PHR-S FM PH.2.5).
                    for _allergy in data.get('allergy_intolerances', []):
                        _allergy_date_str = _allergy.get('recordedDate') or _allergy.get('onsetDateTime')
                        if not _allergy_date_str:
                            continue
                        try:
                            _allergy_dt = datetime.fromisoformat(_allergy_date_str[:10])
                        except (ValueError, TypeError):
                            continue
                        _allergy_date = _allergy_dt.date()
                        _system, _code, _display = _coding_parts(_allergy.get('code'))
                        _vocab = _vocab_from_system(_system)
                        _allergy_concept = None
                        if _code:
                            if _vocab == 'LOINC':
                                _allergy_concept = _cc_by_loinc(_code)
                            else:
                                _allergy_concept = _cc_by_vocab(_vocab, _code)
                        if _allergy_concept is None:
                            _allergy_concept = get_or_create_quarantine_observation(
                                source_vocabulary_id=_vocab,
                                concept_code=_code,
                                concept_name=_display or 'FHIR AllergyIntolerance',
                                source_system='fhir-upload',
                            )
                        _criticality = (_allergy.get('criticality') or '')[:60]
                        _, _cs_code, _cs_display = _coding_parts(_allergy.get('clinicalStatus'))
                        _clinical_status = (_cs_display or _cs_code or '')[:50]
                        _allergy_source = (_code or _display or 'AllergyIntolerance')[:50]
                        _allergy_key = (_allergy_source, _allergy_date, _criticality, 'ALLERGY')
                        if _allergy_key in _existing_report_keys:
                            continue
                        _obs = Observation(
                            observation_id=next_pk(Observation, 'observation_id'),
                            person=person,
                            observation_concept=_allergy_concept,
                            observation_date=_allergy_date,
                            observation_datetime=timezone.make_aware(_allergy_dt) if _allergy_dt.tzinfo is None else _allergy_dt,
                            observation_type_concept=_concept_ehr_type or _allergy_concept,
                            value_as_string=_criticality,
                            observation_source_value=_allergy_source,
                            observation_source_concept=_allergy_concept,
                            qualifier_source_value='ALLERGY',
                            value_source_value=_clinical_status,
                        )
                        _obs._skip_patient_record_refresh = True
                        _obs.save()
                        _existing_report_keys.add(_allergy_key)
                        if prov_source:
                            _record_provenance(_obs, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))

                    # --- Write ProcedureOccurrence records ---
                    _existing_proc_keys = {
                        (p.procedure_source_value, p.procedure_date)
                        for p in ProcedureOccurrence.objects.filter(person=person)
                    }
                    for _proc_fhir in data.get('procedures', []):
                        _performed = (
                            _proc_fhir.get('performedDateTime')
                            or _proc_fhir.get('performedPeriod', {}).get('start')
                        )
                        if not _performed:
                            continue
                        try:
                            _proc_dt = datetime.fromisoformat(_performed[:10])
                        except (ValueError, TypeError):
                            continue
                        _proc_date = _proc_dt.date()

                        _proc_code = _proc_fhir.get('code', {})
                        _proc_coding = (_proc_code.get('coding') or [{}])[0]
                        _proc_system = _proc_coding.get('system', '')
                        _proc_code_value = _proc_coding.get('code', '')
                        _proc_display = (
                            _proc_coding.get('display')
                            or _proc_code.get('text')
                            or _proc_fhir.get('id')
                            or 'FHIR Procedure'
                        )
                        _proc_source = (_proc_code_value or _proc_display or '')[:50]
                        _proc_key = (_proc_source, _proc_date)
                        if _proc_key in _existing_proc_keys:
                            continue

                        _proc_concept = None
                        if _proc_code_value and 'snomed' in _proc_system.lower():
                            _proc_concept = _cc_by_vocab('SNOMED', _proc_code_value)
                        if _proc_concept is None and _proc_display:
                            _proc_concept = Concept.objects.filter(
                                concept_name__icontains=_proc_display,
                                domain__domain_id='Procedure',
                            ).first()
                        if _proc_concept is None:
                            # Never mint under SNOMED (or any licensed
                            # vocabulary), and never pick an arbitrary
                            # Procedure-domain concept — quarantine under
                            # HK-Procedure and record the gap (#236).
                            # _proc_display always falls back to a placeholder,
                            # so this never returns None here.
                            _proc_concept = get_or_create_quarantine_procedure(
                                source_vocabulary_id=_vocab_from_system(_proc_system),
                                concept_code=_proc_code_value,
                                concept_name=_proc_display,
                                source_system='fhir-upload',
                            )

                        _proc = ProcedureOccurrence(
                            procedure_occurrence_id=next_pk(ProcedureOccurrence, 'procedure_occurrence_id'),
                            person=person,
                            procedure_concept=_proc_concept,
                            procedure_date=_proc_date,
                            procedure_datetime=timezone.make_aware(_proc_dt) if _proc_dt.tzinfo is None else _proc_dt,
                            procedure_type_concept=_concept_ehr_type or _proc_concept,
                            procedure_source_value=_proc_source,
                            procedure_source_concept=_proc_concept,
                        )
                        _proc._skip_patient_record_refresh = True
                        _proc.save()
                        _pt_procedure_ids.append(_proc.procedure_occurrence_id)
                        _existing_proc_keys.add(_proc_key)
                        if prov_source:
                            _record_provenance(_proc, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))

                    logger.info(
                        "TIMING patient=%s phase=procedures elapsed=%.1fs count=%d",
                        _timing_hash, _time.monotonic() - _pt_start, len(_pt_procedure_ids),
                    )

                    # --- SCT Patient extensions -> dated OMOP Observation ---
                    # A Patient extension is not itself an event.  The explicit
                    # mm-sct-date is, however, a clinically asserted date for
                    # the accompanying SCT facts, so it is safe to persist all
                    # supplied SCT values as dated Observations and derive the
                    # PatientRecord fields from them.  Without that date we do
                    # not manufacture an import-time clinical event.
                    _sct_event_date = None
                    if sct_date_str:
                        try:
                            _candidate_sct_date = datetime.strptime(sct_date_str, '%Y-%m-%d').date()
                            if _candidate_sct_date <= localdate():
                                _sct_event_date = _candidate_sct_date
                            else:
                                logger.warning(
                                    "Ignoring future mm-sct-date for patient (id_hash=%s)",
                                    hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12],
                                )
                        except ValueError:
                            logger.warning(
                                "Ignoring invalid mm-sct-date for patient (id_hash=%s)",
                                hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12],
                            )

                    if _sct_event_date is not None:
                        _sct_values = [('mm-sct-date', _sct_event_date.isoformat())]
                        if sct_history_str:
                            _history = [
                                token.strip() for token in sct_history_str.split(',')
                                if token.strip() in _allowed_sct_titles
                            ]
                            if _history:
                                _sct_values.append(('mm-sct-history', ','.join(_history)))
                        if sct_eligibility_str:
                            _eligibility = [
                                token.strip() for token in sct_eligibility_str.split(',')
                                if token.strip() in _allowed_elig_titles
                            ]
                            if _eligibility:
                                _sct_values.append(('mm-sct-eligibility', ','.join(_eligibility)))

                        for _sct_source, _sct_value in _sct_values:
                            _sct_exists = Observation.objects.filter(
                                person=person,
                                observation_source_value=_sct_source,
                                observation_date=_sct_event_date,
                                value_as_string=_sct_value,
                            ).exists()
                            if _sct_exists:
                                continue
                            _sct_obs = Observation(
                                observation_id=next_pk(Observation, 'observation_id'),
                                person=person,
                                observation_concept=_concept_ehr_type or _concept_tx_regimen,
                                observation_date=_sct_event_date,
                                observation_type_concept=_concept_ehr_type or _concept_tx_regimen,
                                value_as_string=_sct_value,
                                observation_source_value=_sct_source,
                            )
                            _sct_obs._skip_patient_record_refresh = True
                            _sct_obs.save()
                            _record_provenance(
                                _sct_obs,
                                prov_source or 'EHR_SYNC',
                                prov_user_id,
                                target_patient_id=fhir_patient_id,
                                modification_reason=prov_reason,
                                organization=get_request_org(request),
                            )
                    elif sct_history_str or sct_eligibility_str:
                        logger.warning(
                            "Skipping undated SCT Patient extension values (id_hash=%s)",
                            hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12],
                        )

                    # --- OMOP-first: refresh PatientRecord from OMOP tables ---
                    # Release suppression so the single intentional refresh can run.
                    # We stay inside the atomic block so that a refresh failure rolls
                    # back all OMOP writes for this patient.
                    _suppress_cm.__exit__(None, None, None)
                    logger.info("TIMING patient=%s phase=drug_exposures elapsed=%.1fs", _timing_hash, _time.monotonic() - _pt_start)
                    if _skip_refresh:
                        # Bulk mode — just ensure the PatientRecord row exists so the
                        # patch block below has an object to write FHIR-specific fields
                        # into.  The full OMOP-derived refresh is deferred to the caller.
                        patient_info, _ = PatientRecord.objects.get_or_create(person=person)
                    else:
                        patient_info = refresh_patient_record(person)
                        infer_lot_for_person(person)

                    # --- Patch fields from FHIR that aren't yet in OMOP tables ---
                    # These fields come from FHIR parsing but are not (yet) stored in OMOP.
                    # Once full OMOP write coverage is achieved, this patch block can be removed.
                    _patch = {}
                    if birth_date:
                        _patch['date_of_birth'] = birth_date
                    if disease:
                        _patch['disease'] = disease
                        disease_slug = _disease_slug_from_name(disease)
                        if disease_slug:
                            _patch['disease_slug'] = disease_slug
                    if stage:
                        _patch['stage'] = stage
                    if histologic_type:
                        _patch['histologic_type'] = histologic_type
                    if country:
                        _patch['country'] = country
                    if region:
                        _patch['region'] = region
                    if city:
                        _patch['city'] = city
                    if postal_code:
                        _patch['postal_code'] = postal_code
                    if race:
                        _patch['race'] = race
                    if ethnicity:
                        _patch['ethnicity'] = ethnicity
                    if weight:
                        _patch.update({'weight': weight, 'weight_units': 'kg'})
                    if height:
                        _patch.update({'height': height, 'height_units': 'cm'})
                    if systolic_bp:
                        _patch['systolic_blood_pressure'] = systolic_bp
                    if diastolic_bp:
                        _patch['diastolic_blood_pressure'] = diastolic_bp
                    if heart_rate:
                        _patch['heartrate'] = heart_rate
                    if ecog is not None:
                        _patch['ecog_performance_status'] = ecog
                    if cytogenetics_str is not None:
                        _patch['cytogenic_markers'] = cytogenetics_str
                    if measurable_disease_imwg is not None:
                        _patch['measurable_disease_imwg'] = measurable_disease_imwg
                    if lymph_node_status:
                        _patch['lymph_node_status'] = lymph_node_status
                    if metastasis_status:
                        _patch['metastasis_status'] = metastasis_status
                    if tumor_stage:
                        _patch['tumor_stage'] = tumor_stage
                    if nodes_stage:
                        _patch['nodes_stage'] = nodes_stage
                    if distant_metastasis_stage:
                        _patch['distant_metastasis_stage'] = distant_metastasis_stage
                    if staging_modalities:
                        _patch['staging_modalities'] = staging_modalities
                    if measurable_disease_by_recist_status is not None:
                        _patch['measurable_disease_by_recist_status'] = measurable_disease_by_recist_status
                    if bone_only_metastasis_status is not None:
                        _patch['bone_only_metastasis_status'] = bone_only_metastasis_status
                    if clonal_bone_marrow_b_lymphocytes is not None:
                        _patch['clonal_bone_marrow_b_lymphocytes'] = clonal_bone_marrow_b_lymphocytes
                    if er_status:
                        _patch['estrogen_receptor_status'] = er_status
                    if pr_status:
                        _patch['progesterone_receptor_status'] = pr_status
                    if her2_status:
                        _patch['her2_status'] = her2_status
                    if ki67_index is not None:
                        _patch['ki67_proliferation_index'] = ki67_index
                    if pdl1_percentage is not None:
                        _patch['pd_l1_tumor_cells'] = pdl1_percentage
                    if genetic_mutations:
                        _patch['genetic_mutations'] = genetic_mutations
                    # Therapy lines (denormalized PatientRecord fields)
                    if first_line_therapy:
                        _patch.update({
                            'first_line_therapy': first_line_therapy,
                            'first_line_date': first_line_date,
                            'first_line_start_date': first_line_start_date,
                            'first_line_end_date': first_line_end_date,
                            'first_line_intent': first_line_intent,
                            'first_line_discontinuation_reason': first_line_discontinuation_reason,
                            'first_line_outcome': first_line_outcome,
                        })
                    if second_line_therapy:
                        _patch.update({
                            'second_line_therapy': second_line_therapy,
                            'second_line_date': second_line_date,
                            'second_line_start_date': second_line_start_date,
                            'second_line_end_date': second_line_end_date,
                            'second_line_intent': second_line_intent,
                            'second_line_discontinuation_reason': second_line_discontinuation_reason,
                            'second_line_outcome': second_line_outcome,
                        })
                    if later_therapy:
                        _patch.update({
                            'later_therapy': later_therapy,
                            'later_date': later_date,
                            'later_start_date': later_start_date,
                            'later_end_date': later_end_date,
                            'later_intent': later_intent,
                            'later_discontinuation_reason': later_discontinuation_reason,
                            'later_outcome': later_outcome,
                        })
                    # Labs are now written to the OMOP Measurement table (FHIR-06/07/08)
                    # and derived into PatientRecord via refresh_patient_record (FHIR-09).
                    # Only fields not yet modelled in OMOP are patched directly below.
                    _patch.update({k: v for k, v in {
                        'serum_bilirubin_level_direct': bilirubin_direct,
                        'calcium_mg_dl': calcium,
                        'inr': inr,
                        'pt_seconds': pt,
                        'ptt_seconds': ptt,
                        'cea_ng_ml': cea,
                        'ca19_9_u_ml': ca19_9,
                        'psa_ng_ml': psa,
                        'smoking_status': smoking_status,
                        'pack_years': pack_years,
                        'alcohol_use': alcohol_use,
                        'drinks_per_week': drinks_per_week,
                        'exercise_frequency': exercise_frequency,
                        'exercise_minutes_per_week': exercise_minutes_per_week,
                        'diet_type': diet_type,
                        'sleep_hours_per_night': sleep_hours_per_night,
                        'sleep_quality': sleep_quality,
                        'stress_level': stress_level,
                        'social_support': social_support,
                        'employment_status': employment_status,
                        'education_level': education_level,
                        'marital_status': marital_status,
                        'insurance_type': insurance_type,
                        'number_of_dependents': number_of_dependents,
                        'annual_household_income': annual_household_income,
                        'ecog_assessment_date': ecog_assessment_date,
                        'test_methodology': test_methodology,
                        'test_date': test_date,
                        'test_specimen_type': test_specimen_type,
                        'report_interpretation': report_interpretation,
                        'oncotype_dx_score': oncotype_dx_score,
                        'androgen_receptor_status': androgen_receptor_status,
                        'therapy_intent': therapy_intent,
                        'reason_for_discontinuation': reason_for_discontinuation,
                        'ldh': ldh_new if ldh_new is not None else ldh,
                        'alkaline_phosphatase': alkaline_phosphatase,
                        'magnesium': magnesium,
                        'phosphorus': phosphorus,
                        'pregnancy_test_date': pregnancy_test_date,
                        'pregnancy_test_result_value': pregnancy_test_result_value,
                        'contraceptive_use': contraceptive_use if contraceptive_use is not None else False,
                        'consent_capability': consent_capability if consent_capability is not None else True,
                        'caregiver_availability_status': caregiver_availability_status if caregiver_availability_status is not None else True,
                        'no_mental_health_disorder_status': no_mental_health_disorder_status if no_mental_health_disorder_status is not None else True,
                        'no_substance_use_status': no_substance_use_status if no_substance_use_status is not None else True,
                        'substance_use_details': substance_use_details,
                        'no_geographic_exposure_risk': no_geographic_exposure_risk if no_geographic_exposure_risk is not None else True,
                        'geographic_exposure_risk_details': geographic_exposure_risk_details,
                    }.items() if v is not None})
                    # Stamp the org derived from the OAuth2 token so this patient
                    # is scoped to the uploading service client's tenant.
                    upload_org = get_request_org(request)
                    if upload_org is not None and patient_info.organization_id is None:
                        _patch['organization'] = upload_org

                    # All clinical values above were written to OMOP rows before
                    # refresh_patient_record.  Do not copy the parser's legacy
                    # compatibility dictionary into PatientRecord: doing so
                    # creates a second clinical write authority.  Organization is
                    # tenant metadata and is the sole intentional exception.
                    if upload_org is not None and patient_info.organization_id is None:
                        patient_info.organization = upload_org
                        patient_info.save(update_fields=['organization', 'updated_at'])

                    # Commit all writes for this patient. Mark _atomic_entered=False
                    # so the finally block knows the transaction was cleanly committed.
                    _atomic_cm.__exit__(None, None, None)
                    _atomic_entered = False

                    patients_result.append({
                        'person_id': person.person_id,
                        'patient_info_id': patient_info.pk,  # legacy wire format — preserved for frontend/federation host compatibility
                        'measurement_ids': _pt_measurement_ids,
                        'condition_ids': _pt_condition_ids,
                        'drug_exposure_ids': _pt_drug_exposure_ids,
                        'procedure_ids': _pt_procedure_ids,
                        'episode_ids': _pt_episode_ids,
                        'episode_event_ids': _pt_episode_event_ids,
                    })

                    if person_is_new:
                        created_count += 1
                    else:
                        updated_count += 1
                    _fhir_id_hash = hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12]
                    _pt_total = _time.monotonic() - _pt_start
                    logger.info("Successfully %s patient (id_hash=%s) total=%.1fs",
                                'created' if person_is_new else 'updated',
                                _fhir_id_hash, _pt_total)
                    
                except Exception as e:
                    _last_exc = e
                    _err_hash = hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12]
                    logger.exception("FHIR upload error for patient id_hash=%s", _err_hash)
                    errors.append(f"Patient (id_hash={_err_hash}): processing failed")
                finally:
                    # Roll back if the transaction was opened but never committed
                    # (i.e. an exception occurred during OMOP writes).
                    if _atomic_entered:
                        try:
                            _atomic_cm.__exit__(
                                type(_last_exc) if _last_exc else None,
                                _last_exc,
                                _last_exc.__traceback__ if _last_exc else None,
                            )
                        except Exception:
                            pass
                    # _atomic_cm.__exit__ above already rolls back the savepoint/
                    # transaction via Django's ORM machinery.  A raw connection.rollback()
                    # would bypass savepoint tracking and break TestCase isolation, so
                    # it is intentionally omitted here.
                    # Guarantee suppression is cleared even on BaseException.
                    # Use bare except to handle NameError (assigned before entry) and
                    # any error from calling __exit__ a second time on success path.
                    try:
                        _suppress_cm.__exit__(None, None, None)
                    except Exception:
                        pass
            
            return Response({
                'success': True,
                'created_count': created_count,
                'updated_count': updated_count,
                'patients': patients_result,
                'errors': errors,
            })

        except Exception as e:
            logger.exception('FHIR upload failed')
            return Response({'error': 'Upload failed. Please check the bundle format and try again.'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='upload-wearable',
            permission_classes=[IsAuthenticated])
    def upload_wearable(self, request):
        """POST /api/v1/patient-records/upload-wearable/

        Accept a wearable data file (Garmin .fit or Apple Health .zip) and
        write parsed samples into OMOP Measurement/Observation rows, then
        refresh the PatientRecord 30-day summaries.

        Form fields:
          - file: the uploaded file
          - device_type: 'garmin' | 'apple'
        """
        from omop_core.services.wearable_parsers import parse_garmin_fit, parse_apple_health_export
        from omop_core.services.pk import next_pk_batch as _next_pk_batch
        from omop_core.services.mappings import (
            WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB, WEARABLE_ARTIFACT_BOUNDS,
            WEARABLE_TYPE_CONCEPT_ID,
        )
        from omop_core.services.concept_cache import concept_by_vocab as _cc_by_vocab

        if 'file' not in request.FILES:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        device_type = request.data.get('device_type', '').strip().lower()
        if device_type not in ('garmin', 'apple'):
            return Response(
                {'error': "device_type must be 'garmin' or 'apple'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = request.FILES['file']

        # Validate file extension before reading the file body
        name = (uploaded.name or '').lower()
        if device_type == 'garmin' and not name.endswith('.fit'):
            return Response({'error': 'Garmin uploads must be .fit files'}, status=status.HTTP_400_BAD_REQUEST)
        if device_type == 'apple' and not name.endswith('.zip'):
            return Response({'error': 'Apple Health uploads must be .zip files'}, status=status.HTTP_400_BAD_REQUEST)

        MAX_WEARABLE_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB
        if uploaded.size is not None and uploaded.size > MAX_WEARABLE_UPLOAD_BYTES:
            return Response(
                {'error': f'File too large. Maximum size is {MAX_WEARABLE_UPLOAD_BYTES // (1024 * 1024)} MB.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        file_bytes = uploaded.read()

        # Resolve the Person for the current user
        from patient_portal.models import PatientUser
        try:
            patient_user = PatientUser.objects.get(identity=request.user)
            person = patient_user.person
        except PatientUser.DoesNotExist:
            return Response(
                {'error': 'No patient record linked to your account'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Parse the file
        try:
            if device_type == 'garmin':
                samples = parse_garmin_fit(file_bytes)
            else:
                samples = parse_apple_health_export(file_bytes)
        except Exception as e:
            logger.exception('wearable_parse_error device=%s', device_type)
            return Response(
                {'error': 'Failed to parse file. Please ensure it is a valid '
                          f'{"Garmin .fit" if device_type == "garmin" else "Apple Health export.zip"} file.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not samples:
            return Response({'samples_created': 0, 'duplicates_skipped': 0})

        # Resolve each metric's concept, scoped by (vocabulary_id, concept_code).
        # A bare concept_code is ambiguous — 852 codes are reused across
        # vocabularies — and four wearable metrics live in HK-Wearable, not LOINC.
        metric_concepts: dict[str, Concept | None] = {}
        for metric_key, concept_code in WEARABLE_CONCEPT_CODE.items():
            metric_concepts[metric_key] = _cc_by_vocab(
                WEARABLE_CONCEPT_VOCAB[metric_key], concept_code)

        unresolved = sorted(k for k, c in metric_concepts.items() if c is None)
        if unresolved:
            logger.warning(
                'wearable_concepts_unresolved person_id=%s metrics=%s — samples for these '
                'metrics will be discarded. Run load_athena_vocabularies.',
                person.person_id, unresolved,
            )

        # Provenance type for every row written below. 32865 is
        # 'Patient self-report' — OMOP's Type Concept vocabulary has no
        # device or wearable type, so this is the closest faithful fit for
        # data the patient's own device produced.
        #
        # This previously used 32883 with a comment claiming it was
        # "Patient self-report"; 32883 is 'Survey'. It fell back to 32856,
        # which is 'Lab'. Both mislabelled the provenance of every wearable
        # row, and the fallback fired silently on any database without the
        # full vocabulary loaded (#441).
        #
        # There is deliberately no fallback now: refusing to write is better
        # than writing a row that misstates where the data came from. The
        # Migration 0143 installs this concept, so the deploy path guarantees it
        # rather than leaving it to whoever remembers to run a load.
        #
        # The vocabulary check is not redundant. lab_results.sync._ensure_concept
        # mints concept_id 32865 into HK-Labs as a fallback when Athena is absent
        # — a locally-authored row occupying a genuine Athena id. Accepting it
        # here would type every wearable row with a shadow concept, which is the
        # defect class #415 exists to eliminate, and would do so silently.
        type_concept = Concept.objects.filter(
            concept_id=WEARABLE_TYPE_CONCEPT_ID).first()
        if type_concept is None or type_concept.vocabulary_id != 'Type Concept':
            logger.error(
                'wearable_type_concept_unusable concept_id=%s person_id=%s found=%r '
                'vocabulary_id=%r — refusing to write rows with unknown provenance.',
                WEARABLE_TYPE_CONCEPT_ID, person.person_id,
                type_concept is not None,
                getattr(type_concept, 'vocabulary_id', None),
            )
            return Response(
                {'error': 'Wearable ingestion is not configured on this server '
                          '(measurement type concept missing or invalid). '
                          'Contact an administrator.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Filter out artifact values and deduplicate
        from django.db.models import Q

        # UCUM unit for each metric, written to *_source_value.
        unit_map = {
            'steps': '/d',
            'active_minutes': 'min',
            'resting_hr': '/min',
            'hrv_sdnn': 'ms',
            'hrv_rmssd': 'ms',
            'spo2': '%',
            'respiratory_rate': '/min',
            'sleep_duration': 'h',
            'vo2_max': 'mL/kg/min',
            'distance': 'km',
            'walking_speed': 'km/hr',
            'walking_step_length': 'cm',
            'walking_double_support_pct': '%',
            'walking_hr_avg': '/min',
            'flights_climbed': '{flights}',
            'active_energy': 'kcal',
            'basal_energy': 'kcal',
            'body_mass': 'kg',
        }

        # OMOP routes a row by its concept's domain, not by a hard-coded metric
        # list: steps, active_minutes, sleep_duration and flights_climbed are
        # Observation-domain concepts, the rest are Measurement.
        def _is_observation(concept):
            return concept.domain_id == 'Observation'

        existing_keys = set()
        # Build set of (metric_key, date, value) that already exist, reading
        # whichever table the concept's domain routes it to.
        for metric_key in set(s.metric_key for s in samples):
            concept = metric_concepts.get(metric_key)
            if concept is None:
                continue
            if _is_observation(concept):
                existing_rows = Observation.objects.filter(
                    person=person,
                    observation_concept=concept,
                ).values_list('observation_date', 'value_as_number')
            else:
                existing_rows = Measurement.objects.filter(
                    person=person,
                    measurement_concept=concept,
                ).values_list('measurement_date', 'value_as_number')
            for row_date, row_val in existing_rows:
                if row_val is not None:
                    existing_keys.add((metric_key, row_date, float(row_val)))

        pending_measurements = []
        pending_observations = []
        duplicates_skipped = 0
        unmapped_samples = 0
        unmapped_metrics = set()

        for sample in samples:
            concept = metric_concepts.get(sample.metric_key)
            if concept is None:
                unmapped_samples += 1
                unmapped_metrics.add(sample.metric_key)
                continue

            # Artifact filter
            bounds = WEARABLE_ARTIFACT_BOUNDS.get(sample.metric_key)
            if bounds and not (bounds[0] <= sample.value <= bounds[1]):
                continue

            # Dedup check
            dedup_key = (sample.metric_key, sample.date, round(sample.value, 2))
            if dedup_key in existing_keys:
                duplicates_skipped += 1
                continue
            existing_keys.add(dedup_key)

            unit = unit_map.get(sample.metric_key)
            source_code = WEARABLE_CONCEPT_CODE[sample.metric_key]

            if _is_observation(concept):
                obs = Observation(
                    observation_id=0,  # allocated below
                    person=person,
                    observation_concept=concept,
                    observation_date=sample.date,
                    observation_type_concept=type_concept,
                    value_as_number=sample.value,
                    observation_source_value=source_code,
                    unit_source_value=unit,
                )
                obs._skip_patient_record_refresh = True
                pending_observations.append(obs)
            else:
                m = Measurement(
                    measurement_id=0,  # allocated below
                    person=person,
                    measurement_concept=concept,
                    measurement_date=sample.date,
                    measurement_type_concept=type_concept,
                    value_as_number=sample.value,
                    measurement_source_value=source_code,
                    unit_source_value=unit,
                )
                m._skip_patient_record_refresh = True
                pending_measurements.append(m)

        created_count = 0
        with transaction.atomic():
            if pending_measurements:
                m_ids = _next_pk_batch(Measurement, 'measurement_id', len(pending_measurements))
                for m, mid in zip(pending_measurements, m_ids):
                    m.measurement_id = mid
                Measurement.objects.bulk_create(pending_measurements)
                created_count += len(pending_measurements)

            if pending_observations:
                obs_ids = _next_pk_batch(Observation, 'observation_id', len(pending_observations))
                for obs, oid in zip(pending_observations, obs_ids):
                    obs.observation_id = oid
                Observation.objects.bulk_create(pending_observations)
                created_count += len(pending_observations)

        # Refresh the PatientRecord to recompute 30-day summaries
        try:
            refresh_patient_record(person)
        except Exception:
            logger.exception('wearable_refresh_failed person_id=%s', person.person_id)

        # Record upload history
        sample_summary = [
            {'metric': s.metric_key, 'date': s.date.isoformat(), 'value': s.value}
            for s in samples
        ]
        try:
            WearableUpload.objects.create(
                person=person,
                device_type=device_type,
                filename=uploaded.name or 'unknown',
                samples_created=created_count,
                duplicates_skipped=duplicates_skipped,
                sample_summary=sample_summary,
                uploaded_by=request.user,
            )
        except Exception:
            logger.exception('wearable_upload_history_save_failed person_id=%s', person.person_id)

        if unmapped_samples:
            logger.warning(
                'wearable_upload_unmapped device=%s person_id=%s samples=%d metrics=%s',
                device_type, person.person_id, unmapped_samples, sorted(unmapped_metrics),
            )

        logger.info(
            'wearable_upload_complete device=%s person_id=%s created=%d duplicates=%d unmapped=%d',
            device_type, person.person_id, created_count, duplicates_skipped, unmapped_samples,
        )
        # unmapped_* is reported so a missing concept is distinguishable from
        # "the device exported no data for this metric" — previously identical.
        return Response({
            'samples_created': created_count,
            'duplicates_skipped': duplicates_skipped,
            'unmapped_samples': unmapped_samples,
            'unmapped_metrics': sorted(unmapped_metrics),
        })

    @action(detail=False, methods=['get'], url_path='wearable-uploads',
            permission_classes=[IsAuthenticated])
    def wearable_uploads(self, request):
        """List wearable upload history for the current patient."""
        patient_user = getattr(request.user, 'patient_user', None)
        if not patient_user:
            return Response([])
        person = patient_user.person
        uploads = WearableUpload.objects.filter(person=person)[:50]
        data = [
            {
                'id': u.id,
                'device_type': u.device_type,
                'filename': u.filename,
                'samples_created': u.samples_created,
                'duplicates_skipped': u.duplicates_skipped,
                'sample_summary': u.sample_summary,
                'uploaded_at': u.uploaded_at.isoformat(),
            }
            for u in uploads
        ]
        return Response(data)

    @action(detail=False, methods=['delete'], url_path='wearable-uploads/(?P<upload_id>[0-9]+)',
            permission_classes=[IsAuthenticated])
    def delete_wearable_upload(self, request, upload_id=None):
        """Delete a wearable upload and its associated Measurement/Observation rows."""
        from omop_core.services.mappings import WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB
        from omop_core.services.concept_cache import concept_by_vocab as _cc_by_vocab

        patient_user = getattr(request.user, 'patient_user', None)
        if not patient_user:
            return Response({'error': 'Not a patient'}, status=status.HTTP_403_FORBIDDEN)
        person = patient_user.person

        try:
            upload = WearableUpload.objects.get(id=upload_id, person=person)
        except WearableUpload.DoesNotExist:
            return Response({'error': 'Upload not found'}, status=status.HTTP_404_NOT_FOUND)

        # Delete associated Measurement/Observation rows using sample_summary
        deleted_count = 0
        for entry in upload.sample_summary or []:
            metric_key = entry.get('metric')
            date_str = entry.get('date')
            value = entry.get('value')
            if not metric_key or not date_str or value is None:
                continue

            concept_code = WEARABLE_CONCEPT_CODE.get(metric_key)
            if not concept_code:
                continue
            concept = _cc_by_vocab(WEARABLE_CONCEPT_VOCAB[metric_key], concept_code)
            if not concept:
                continue

            sample_date = _date.fromisoformat(date_str)

            # Route by the concept's domain, matching how upload_wearable wrote it.
            if concept.domain_id == 'Observation':
                count, _ = Observation.objects.filter(
                    person=person,
                    observation_concept=concept,
                    observation_date=sample_date,
                    value_as_number=value,
                ).delete()
            else:
                count, _ = Measurement.objects.filter(
                    person=person,
                    measurement_concept=concept,
                    measurement_date=sample_date,
                    value_as_number=value,
                ).delete()
            deleted_count += count

        upload.delete()

        # Refresh PatientRecord to recompute 30-day summaries
        try:
            refresh_patient_record(person)
        except Exception:
            logger.exception('wearable_refresh_after_delete_failed person_id=%s', person.person_id)

        logger.info(
            'wearable_upload_deleted upload_id=%s person_id=%s measurements_deleted=%d',
            upload_id, person.person_id, deleted_count,
        )
        return Response({'deleted_measurements': deleted_count})

    @action(detail=False, methods=['delete'], permission_classes=[ScopedTokenPermission])
    def bulk_delete(self, request):
        """Delete multiple patients by person_ids"""
        person_ids = request.data.get('person_ids', [])
        
        if not person_ids:
            return Response({'error': 'No person_ids provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            deleted_count = 0
            errors = []
            
            org = get_request_org(request)
            _is_privileged = request.user and getattr(request.user, 'is_staff', False)
            for person_id in person_ids:
                try:
                    person = Person.objects.get(person_id=person_id)
                    if org is not None and not PatientRecord.objects.filter(person=person, organization=org).exists():
                        errors.append("Person not found.")
                        continue
                    elif org is None and not _is_privileged:
                        from omop_core.authorization import can_access_patient
                        if not can_access_patient(request.user, person_id):
                            errors.append("Person not found.")
                            continue
                    with transaction.atomic():
                        # Delete OMOP clinical rows in FK dependency order.
                        _delete_omop_clinical_rows(person)
                        # PatientGroupMembership uses a plain BigIntegerField (no DB FK)
                        # so it is never cascade-deleted by the ORM.
                        PatientGroupMembership.objects.filter(person_id=person.person_id).delete()
                        # Delete PatientRecord
                        PatientRecord.objects.filter(person=person).delete()
                        # Delete associated Identity if exists (via PatientUser)
                        from patient_portal.models import PatientUser as PU
                        try:
                            pu = PU.objects.get(person=person)
                            pu.identity.delete()
                        except PU.DoesNotExist:
                            pass
                        # Delete Person last (other rows hold person FK)
                        person.delete()
                        deleted_count += 1
                except Person.DoesNotExist:
                    errors.append("Person not found.")
                except Exception:
                    id_hash = hashlib.sha256(str(person_id).encode()).hexdigest()[:12]
                    logger.warning("bulk_delete: delete failed (id_hash=%s)", id_hash)
                    errors.append("Delete failed.")
            
            return Response({
                'success': True,
                'deleted_count': deleted_count,
                'errors': errors
            })

        except Exception as e:
            logger.exception('Bulk delete failed')
            return Response({'error': 'Delete operation failed.'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['delete'], permission_classes=[ScopedTokenPermission])
    def bulk_delete_filtered(self, request):
        """Delete PatientRecord records matching all active filters (org + disease + stage + date).
        Only deletes the matched PatientRecord rows; Person/Identity are removed only if the
        person has no remaining PatientRecord in any org after the deletion.
        """
        try:
            base_queryset = self.get_queryset()
            filtered_queryset = self._apply_patient_list_filters(base_queryset)

            # Snapshot both the specific PatientRecord PKs and their person IDs in one query.
            snapshot = list(filtered_queryset.values_list('id', 'person__person_id'))
            if not snapshot:
                return Response({'success': True, 'deleted_count': 0, 'errors': []})

            patient_record_ids = [row[0] for row in snapshot]
            person_ids = [row[1] for row in snapshot]

            errors = []
            # Bulk-delete only the specifically filtered PatientRecord records — correctly
            # scoped to org + disease + stage + date via the queryset snapshot.
            # This transaction commits before per-person orphan cleanup so a failure
            # in the cleanup loop cannot roll back the PatientRecord deletions.
            with transaction.atomic():
                PatientRecord.objects.filter(id__in=patient_record_ids).delete()
            deleted_count = len(patient_record_ids)

            # Clean up Person/OMOP rows/Identity for persons that now have no PatientRecord
            # at all.  Each person gets its own savepoint so a single failure does not
            # abort cleanup for the remaining persons.
            persons = {p.person_id: p for p in Person.objects.filter(person_id__in=person_ids)}
            from patient_portal.models import PatientUser as PU
            for person_id in person_ids:
                person = persons.get(person_id)
                if person is None:
                    continue
                try:
                    if not PatientRecord.objects.filter(person=person).exists():
                        with transaction.atomic():
                            _delete_omop_clinical_rows(person)
                            # PatientGroupMembership uses a plain BigIntegerField (no DB FK).
                            PatientGroupMembership.objects.filter(person_id=person.person_id).delete()
                            try:
                                pu = PU.objects.get(person=person)
                                pu.identity.delete()
                            except PU.DoesNotExist:
                                pass
                            person.delete()
                except Exception:
                    id_hash = hashlib.sha256(str(person_id).encode()).hexdigest()[:12]
                    logger.warning("bulk_delete_filtered: person cleanup failed (id_hash=%s)", id_hash)
                    errors.append("Person cleanup failed.")

            return Response({
                'success': True,
                'deleted_count': deleted_count,
                'errors': errors
            })

        except Exception:
            logger.exception("bulk_delete_filtered: unexpected error")
            return Response({'error': 'Delete operation failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """Simple login with username and password"""
    try:
        username = request.data.get('username')
        password = request.data.get('password')

        if not username or not password:
            return Response({
                'error': 'Username and password required'
            }, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(request, username=username, password=password)

        # Fallback: if username lookup failed, try matching by email
        from patient_portal.models import Identity
        if user is None:
            identity = Identity.objects.filter(email__iexact=username).first()
            if identity:
                user = authenticate(request, username=identity.uid, password=password)

        if user is not None:
            login(request, user)
            user_serializer = UserSerializer(user)
            return Response({
                'message': 'Login successful',
                'user': user_serializer.data
            }, status=status.HTTP_200_OK)

        # Check if the account is locked so we can show a specific message
        identity = (
            Identity.objects.filter(uid=username).first()
            or Identity.objects.filter(email__iexact=username).first()
        )
        if identity and identity.is_locked:
            return Response({
                'error': 'Account temporarily locked due to too many failed attempts. Please try again later.'
            }, status=status.HTTP_403_FORBIDDEN)

        return Response({
            'error': 'Invalid credentials'
        }, status=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        import traceback
        logger.error('Login error: %s\n%s', str(e), traceback.format_exc())
        return Response({
            'error': 'Login failed',
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Change the authenticated local account's password (PHR-S FM TI.1.1).

    Enforces the password validators, the no-reuse policy, and clears the
    force-change flag. Used both for a routine change and to satisfy a
    must_change_password requirement after an admin reset.
    """
    from django.contrib.auth import update_session_auth_hash
    from patient_portal.services import (
        password_reuse_error, password_validation_errors, set_new_password,
    )

    identity = request.user
    current = request.data.get('current_password') or ''
    new = request.data.get('new_password') or ''

    if not new:
        return Response({'error': 'new_password is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # Verify the current password for accounts that have one (OIDC/service
    # identities carry an unusable password and set one for the first time here).
    if identity.has_usable_password() and not identity.check_password(current):
        return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

    pw_errors = password_validation_errors(new, email=getattr(identity, 'email', None))
    if pw_errors:
        return Response({'error': ' '.join(pw_errors)}, status=status.HTTP_400_BAD_REQUEST)

    reuse_error = password_reuse_error(identity, new)
    if reuse_error:
        return Response({'error': reuse_error}, status=status.HTTP_400_BAD_REQUEST)

    set_new_password(identity, new, must_change=False)
    update_session_auth_hash(request, identity)  # keep the session valid after the change
    return Response({'detail': 'Password updated.'}, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def logout_view(request):
    """Logout the user and clear session"""
    logout(request)
    return Response({'message': 'Logged out successfully'}, status=status.HTTP_200_OK)

@csrf_exempt
@require_http_methods(["GET"])
def health_check(request):
    """Health check endpoint for monitoring"""
    from django.db import connection
    try:
        connection.ensure_connection()
        db_status = 'connected'
    except Exception:
        db_status = 'error'

    http_status = 200 if db_status == 'connected' else 503
    return JsonResponse({
        'status': 'healthy' if db_status == 'connected' else 'unhealthy',
        'service': 'promop',
        'database': db_status,
    }, status=http_status)


# Disease label lookup — human-readable names for known disease slugs.
_DISEASE_LABELS = {
    'mm':                     'Multiple Myeloma',
    'MM':                     'Multiple Myeloma',
    'er-erbb2-breast-cancer': 'ER+/HER2+ Breast Cancer',
    'breast-cancer':           'Breast Cancer',
    'follicular-lymphoma':     'Follicular Lymphoma',
    'cll':                    'Chronic Lymphocytic Leukemia',
    'lung-cancer':             'Lung Cancer',
    'colon-cancer':            'Colon Cancer',
}


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def org_disease_stats(request):
    """GET /api/stats/org-disease/ — per-org disease patient counts for the requesting user."""
    from django.db.models import Count

    def _disease_counts(qs):
        rows = (
            qs.values('disease_slug')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        return [
            {
                'disease_slug': r['disease_slug'] or '',
                'label': _DISEASE_LABELS.get(r['disease_slug'] or '', r['disease_slug'] or 'Unknown'),
                'count': r['count'],
            }
            for r in rows
        ]

    from django.db.models import Count

    orgs = get_visible_orgs(request.user)
    org_list = list(orgs.order_by('name'))

    # Build {org_id: set of granting_org_ids} covering both org-to-org and domain trusts
    trusting_map = build_trusting_map(org_list)

    # Batch-compute patient counts for all granting orgs in one query (avoids N+1).
    # Disease breakdowns use this same accessible scope so direct access to an
    # umbrella org, such as HealthTree Trust, shows diseases from trusted orgs.
    all_granting_ids = {gid for gids in trusting_map.values() for gid in gids}
    granting_counts: dict[int, int] = {}
    if all_granting_ids:
        granting_counts = dict(
            PatientRecord.objects.filter(organization_id__in=all_granting_ids)
            .values('organization_id')
            .annotate(c=Count('id'))
            .values_list('organization_id', 'c')
        )

    result = []
    for org in org_list:
        owned_counts = _disease_counts(PatientRecord.objects.filter(organization=org))
        owned_count = sum(d['count'] for d in owned_counts)
        accessible_count = owned_count + sum(granting_counts.get(gid, 0) for gid in trusting_map[org.id])
        accessible_org_ids = {org.id} | trusting_map[org.id]
        counts = _disease_counts(PatientRecord.objects.filter(organization_id__in=accessible_org_ids))

        result.append({
            'org_slug': org.slug,
            'org_name': org.name,
            'total': accessible_count,
            'owned_count': owned_count,
            'accessible_count': accessible_count,
            'disease_counts': counts,
        })

    # For staff users: also surface patients not assigned to any org.
    if getattr(request.user, 'is_staff', False):
        counts = _disease_counts(PatientRecord.objects.filter(organization__isnull=True))
        if counts:
            unassigned_total = sum(d['count'] for d in counts)
            result.insert(0, {
                'org_slug': '__unassigned__',
                'org_name': 'All Patients (Unassigned)',
                'total': unassigned_total,
                'owned_count': unassigned_total,
                'accessible_count': unassigned_total,
                'disease_counts': counts,
            })

    return Response(result)


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def auth_test(request):
    """Test auth endpoint — DEBUG only."""
    if not settings.DEBUG:
        return Response({'detail': 'Not available'}, status=status.HTTP_404_NOT_FOUND)
    import traceback as tb
    try:
        step = 'start'
        username = request.data.get('username', 'test')
        step = 'got username'
        from django.contrib.auth import authenticate as do_auth
        step = 'imported authenticate'
        user = do_auth(request, username=username, password='badpassword_test_only')
        step = 'authenticate done'
        return Response({'status': 'ok', 'step': step, 'user': str(user)})
    except Exception as e:
        return Response({'status': 'error', 'step': step, 'error': str(e), 'traceback': tb.format_exc()}, status=500)

# =============================================================================
# Person ViewSet — identity resolution and demographic patch
# =============================================================================

# Fields considered "placeholder" values that a fill-if-empty PATCH may overwrite.
_PERSON_STR_PLACEHOLDERS = {'', 'unknown', 'Unknown'}
_PERSON_YEAR_PLACEHOLDER = PERSON_YEAR_PLACEHOLDERS
_PERSON_INT_PLACEHOLDER  = {None, 0}

_PERSON_PATCHABLE_FIELDS = {
    'given_name':            ('str',  _PERSON_STR_PLACEHOLDERS),
    'family_name':           ('str',  _PERSON_STR_PLACEHOLDERS),
    'year_of_birth':         ('int',  _PERSON_YEAR_PLACEHOLDER),
    'month_of_birth':        ('int',  _PERSON_INT_PLACEHOLDER),
    'day_of_birth':          ('int',  _PERSON_INT_PLACEHOLDER),
    'gender_source_value':   ('str',  _PERSON_STR_PLACEHOLDERS),
    'race_source_value':     ('str',  _PERSON_STR_PLACEHOLDERS),
    'ethnicity_source_value':('str',  _PERSON_STR_PLACEHOLDERS),
}

# PatientRecord field → (Person concept FK, Person source column). Both are
# written together: derivation reads the concept first and falls back to the
# source value, so writing text alone leaves a stale concept outranking it and the
# correction silently appears not to have taken.
_PERSON_DEMOGRAPHIC_FIELDS = {
    'gender': ('gender_concept', 'gender_source_value'),
    'race': ('race_concept', 'race_source_value'),
    'ethnicity': ('ethnicity_concept', 'ethnicity_source_value'),
}


# PatientRecord field → (Location column, kind). These live on the OMOP Location
# row that Person.location points at, not on Person, which is why the projection
# name and the column name differ for two of them.
#
# Replaceable rather than fill-if-empty: an address is corrected far more often
# than a birth date, and a patient who moves needs the new value to win.
_PERSON_LOCATION_FIELDS = {
    'city':        ('city', 'str', 50),
    'region':      ('state', 'str', 2),
    'postal_code': ('zip', 'str', 9),
    'country':     ('country', 'str', 100),
    'latitude':    ('latitude', 'decimal', None),
    'longitude':   ('longitude', 'decimal', None),
}

# Bounds are the CDM's, checked here so an over-long value is refused with a
# reason instead of being truncated by the database. `region` maps to `state`,
# which the CDM caps at two characters — a full region name is a 400, not a
# silent 'Ca'.
_LOCATION_DECIMAL_RANGE = {'latitude': (-90, 90), 'longitude': (-180, 180)}

_PERSON_REPLACEABLE_FIELDS = {
    'email': 'email',
    'phone_number': 'str',
    'facility_name': 'str',
    'validated': 'bool',
    'validated_by': 'str',
    'validation_date': 'date',
    'suppress_demographics_for_others': 'bool',
}


@method_decorator(csrf_exempt, name='dispatch')
class PatientRecordV1ViewSet(PatientRecordViewSet):
    """v1-only PatientRecord surface.

    The legacy /api/patient-info/ prefix registers PatientRecordViewSet itself,
    so anything added there widens a frozen API. New actions belong here.
    """

    @action(detail=False, methods=['get'], url_path='writable-fields')
    def writable_fields(self, request: Request) -> Response:
        """Which projection fields a client may edit, and the OMOP fact to write.

        PatientRecord has no writable clinical columns, so an editor must write the
        underlying fact instead. This tells it which table, concept and unit each
        field needs. Fields with no reviewed concept set are reported as unwritable
        with a reason rather than omitted, so a client can render them read-only and
        explain why instead of failing on save.

        Deployment metadata, not patient data: no person is involved and the result
        is identical for every caller.
        """
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        return Response(build_writable_field_descriptor())

    @action(detail=True, methods=['post'], url_path='refresh',
            permission_classes=[ScopedTokenPermission, PatientSelfScopePermission])
    def refresh(self, request: Request, pk: str | None = None) -> Response:
        """Re-derive this person's PatientRecord once, on demand."""
        person, patient_info, err = self._resolve_patient_with_auth(request, pk)
        if err:
            return err

        if not _is_admin_actor(request):
            return Response(
                {'detail': 'Only administrators can trigger a derivation.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Unguarded on purpose. A 2xx over a record that did not re-derive
        # would be a lie on an endpoint that exists only to derive.
        # Safety net: abort any single SQL statement that exceeds 25 s so a
        # pathologically large patient cannot hold a DB connection indefinitely.
        # SET LOCAL scopes to the enclosing transaction and auto-reverts on commit.
        from django.db import connection
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '25s'")
            record: PatientRecord = refresh_patient_record(person)
        return Response({
            'person_id': person.person_id,
            'refreshed': True,
            'derived_at': getattr(record, 'derived_at', None),
            'derivation_version': getattr(record, 'derivation_version', None),
        })



class PersonViewSet(viewsets.GenericViewSet):
    """
    Endpoints:
      POST /api/persons/find_or_create/  — resolve OIDC identity to a Person row
      PATCH /api/persons/{person_id}/    — fill-if-empty demographic patch
    """
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    queryset = Person.objects.all()
    lookup_field = 'person_id'

    @action(detail=False, methods=['post'], url_path='find_or_create')
    def find_or_create(self, request):
        """
        POST /api/persons/find_or_create/
        Body: { "actor_iss": "...", "actor_sub": "..." }
        Response 200/201: { "person_id": 1234, "created": true }
        """
        actor_iss = request.data.get('actor_iss', '').strip()
        actor_sub = request.data.get('actor_sub', '').strip()
        if not actor_iss or not actor_sub:
            return Response(
                {'detail': 'actor_iss and actor_sub are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from patient_portal.models import Identity, PatientUser
        identity = Identity.objects.filter(
            issuer=actor_iss, sub=actor_sub,
        ).first()
        if identity is not None:
            patient_user = (
                PatientUser.objects
                .select_related('person')
                .filter(identity=identity)
                .first()
            )
            if patient_user is not None:
                person = patient_user.person
                updates = []
                current_iss = person.actor_iss or ''
                current_sub = person.actor_sub or ''
                if (
                    current_iss in ('', actor_iss)
                    and current_sub in ('', actor_sub)
                ):
                    if not current_iss:
                        person.actor_iss = actor_iss
                        updates.append('actor_iss')
                    if not current_sub:
                        person.actor_sub = actor_sub
                        updates.append('actor_sub')
                if updates:
                    try:
                        person.save(update_fields=updates)
                    except IntegrityError:
                        # A prior buggy find_or_create may already have minted
                        # a duplicate with these actor columns. The account
                        # holder link is authoritative, so return it without
                        # clobbering either row.
                        pass
                return Response(
                    {'person_id': person.person_id, 'created': False},
                    status=status.HTTP_200_OK,
                )

        try:
            _new_person_id = next_pk(Person, 'person_id')
            person, created = Person.objects.get_or_create(
                actor_iss=actor_iss,
                actor_sub=actor_sub,
                defaults={'person_id': _new_person_id},
            )
        except IntegrityError:
            # Concurrent first-call race: another request won the INSERT
            person = Person.objects.get(actor_iss=actor_iss, actor_sub=actor_sub)
            created = False
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response({'person_id': person.person_id, 'created': created}, status=http_status)

    def partial_update(self, request, person_id=None):
        """
        PATCH /api/persons/{person_id}/
        Fill-if-empty: each field is only written when the current value is null or a placeholder.
        Never clobbers real data.
        """
        try:
            person = Person.objects.get(person_id=person_id)
        except (Person.DoesNotExist, ValueError):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Trusted backend (service-token): skip per-person row-level ACL.
        # ScopedTokenPermission confirmed the caller holds a valid HMAC-verified
        # service token. Service tokens have full cross-person write access by design.
        if not is_service_token(request):
            org = get_request_org(request)
            if org is not None:
                if not PatientRecord.objects.filter(person=person, organization=org).exists():
                    return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
            elif not getattr(request.user, 'is_staff', False):
                from omop_core.authorization import can_access_patient
                if not can_access_patient(request.user, person.person_id):
                    return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        changed = []
        for field, (kind, placeholders) in _PERSON_PATCHABLE_FIELDS.items():
            if field not in request.data:
                continue
            incoming = request.data[field]
            if kind == 'int' and incoming is not None:
                try:
                    incoming = int(incoming)
                except (TypeError, ValueError):
                    return Response(
                        {'detail': f"'{field}' must be an integer."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            current  = getattr(person, field)
            if current in placeholders or current is None:
                setattr(person, field, incoming)
                changed.append(field)

        for field, kind in _PERSON_REPLACEABLE_FIELDS.items():
            if field not in request.data:
                continue
            incoming = request.data[field]
            if kind in {'str', 'email'} and incoming is not None:
                incoming = str(incoming).strip() or None
                if kind == 'email' and incoming is not None:
                    try:
                        validate_email(incoming)
                    except DjangoValidationError:
                        return Response(
                            {'detail': f"'{field}' must be a valid email address."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
            elif kind == 'bool' and incoming is not None:
                if isinstance(incoming, bool):
                    pass
                elif incoming in {0, 1}:
                    incoming = bool(incoming)
                elif isinstance(incoming, str) and incoming.lower() in {'true', '1', 'yes'}:
                    incoming = True
                elif isinstance(incoming, str) and incoming.lower() in {'false', '0', 'no'}:
                    incoming = False
                else:
                    return Response(
                        {'detail': f"'{field}' must be a boolean."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif kind == 'date' and incoming is not None:
                incoming = parse_date(str(incoming))
                if incoming is None:
                    return Response(
                        {'detail': f"'{field}' must be an ISO date."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if getattr(person, field) != incoming:
                setattr(person, field, incoming)
                changed.append(field)

        # ---- Demographics -------------------------------------------------
        # Replaceable, unlike the *_source_value entries above: a wrong gender or
        # race must be correctable, not merely fillable when blank.
        for field, (concept_attr, source_attr) in _PERSON_DEMOGRAPHIC_FIELDS.items():
            if field not in request.data:
                continue
            incoming = request.data[field]
            incoming = str(incoming).strip() or None if incoming is not None else None
            concept = resolve_demographic_concept(field, incoming)
            # Clear the concept when the new value is not a curated answer. Leaving
            # the old one would let derivation keep reporting the value that was
            # just corrected, since it reads the concept before the source text.
            if getattr(person, f'{concept_attr}_id') != (concept.concept_id if concept else None):
                setattr(person, concept_attr, concept)
                changed.append(concept_attr)
            if getattr(person, source_attr) != incoming:
                setattr(person, source_attr, incoming)
                changed.append(source_attr)

        # ---- Location -----------------------------------------------------
        # Six projection fields resolve to the OMOP Location row rather than to
        # Person. The row is created on first write, because a patient whose
        # address arrives after registration has no location to update.
        location_updates = {}
        for field, (column, kind, max_len) in _PERSON_LOCATION_FIELDS.items():
            if field not in request.data:
                continue
            incoming = request.data[field]
            if incoming is not None and kind == 'str':
                incoming = str(incoming).strip() or None
                if incoming is not None and max_len and len(incoming) > max_len:
                    return Response(
                        {'detail': (
                            f"'{field}' must be at most {max_len} characters "
                            f'(OMOP Location.{column}).'
                        )},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif incoming is not None and kind == 'decimal':
                try:
                    incoming = Decimal(str(incoming))
                except (InvalidOperation, TypeError, ValueError):
                    return Response(
                        {'detail': f"'{field}' must be a number."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                lo, hi = _LOCATION_DECIMAL_RANGE[field]
                if not (lo <= incoming <= hi):
                    return Response(
                        {'detail': f"'{field}' must be between {lo} and {hi}."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            location_updates[column] = incoming

        if location_updates:
            location = None
            if person.location_id:
                location = Location.objects.filter(
                    location_id=person.location_id
                ).first()
            if location is None:
                location = Location(location_id=next_pk(Location, 'location_id'))
            location_changed = [
                c for c, v in location_updates.items() if getattr(location, c) != v
            ]
            for column, value in location_updates.items():
                setattr(location, column, value)
            if location._state.adding:
                location.save()
                # Person.location_id is a plain IntegerField, not a FK — the CDM
                # link is by id only, so assign the id rather than the instance.
                person.location_id = location.location_id
                changed.append('location_id')
            elif location_changed:
                location.save(update_fields=location_changed)

        if changed:
            person.save(update_fields=changed)
            refresh_patient_record(person)

        return Response({'person_id': person.person_id, 'updated_fields': changed})


# =============================================================================
# OMOP clinical event ViewSets
# =============================================================================

_MODEL_PK_MAP = {
    'ConditionOccurrence': ('condition_occurrence_id', ConditionOccurrence),
    'DrugExposure':        ('drug_exposure_id',        DrugExposure),
    'Measurement':         ('measurement_id',          Measurement),
    'Observation':         ('observation_id',          Observation),
    'ProcedureOccurrence': ('procedure_occurrence_id', ProcedureOccurrence),
}


class _OmopFilterMixin:
    """Filter by person_id query param and restrict to the requesting org's patients."""
    pagination_query_params = frozenset({'page', 'page_size', 'limit'})
    allowed_list_query_params = (
        frozenset({'person_id', 'include_erroneous', 'format'})
        | pagination_query_params
    )
    clinical_filter_fields = None
    pagination_class = ClinicalOmopPagination

    def get_allowed_list_query_params(self):
        allowed = set(self.allowed_list_query_params)
        config = self.clinical_filter_fields
        if config:
            allowed.update({
                config['concept_param'],
                config['source_concept_param'],
                'concept_code',
                f"{config['date_field']}__gte",
                f"{config['date_field']}__lte",
            })
            if config.get('visit_filter', True):
                allowed.add('visit_occurrence_id')
        return allowed

    def _pagination_requested(self):
        return bool(set(self.request.query_params) & self.pagination_query_params)

    def _ordered_for_pagination(self, queryset):
        if queryset.ordered:
            return queryset
        return queryset.order_by(queryset.model._meta.pk.name)

    def _unsupported_list_query_params(self):
        allowed = self.get_allowed_list_query_params()
        return sorted(set(self.request.query_params) - allowed)

    def list(self, request, *args, **kwargs):
        unsupported = self._unsupported_list_query_params()
        if unsupported:
            supported = sorted(self.get_allowed_list_query_params())
            return Response(
                {
                    'detail': (
                        'Unsupported query parameter(s): '
                        + ', '.join(unsupported)
                    ),
                    'unsupported_params': unsupported,
                    'supported_params': supported,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if self._pagination_requested():
            queryset = self._ordered_for_pagination(
                self.filter_queryset(self.get_queryset()))
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def get_queryset(self):
        qs = super().get_queryset()
        person_id = self.request.query_params.get('person_id')
        if person_id:
            qs = qs.filter(person_id=person_id)
        # PHR-S FM PH.1.1#06 — exclude entered-in-error rows from normal reads
        # by default. Applies only to models that carry the flag (clinical
        # event tables); the row is RETAINED, never deleted. Pass
        # ?include_erroneous=true to surface them (e.g. to review or un-flag).
        if any(f.name == 'is_erroneous' for f in qs.model._meta.get_fields()):
            include = str(
                self.request.query_params.get('include_erroneous', '')
            ).strip().lower() in ('1', 'true', 'yes')
            if not include:
                qs = qs.exclude(is_erroneous=True)
        # Trusted backend (service-token): full visibility. Already
        # validated at the permission layer (ScopedTokenPermission).
        if is_service_token(self.request):
            return self._apply_clinical_filters(qs)
        org = get_request_org(self.request)
        if org is not None:
            from omop_core.models import PatientRecord
            allowed = PatientRecord.objects.filter(organization=org).values('person_id')
            qs = qs.filter(person_id__in=allowed)
        elif not (self.request.user and
            getattr(self.request.user, 'is_staff', False)
        ):
            # Session / partner-auth (Firebase, SAML): no org token.
            # Enforce per-patient access using can_access_patient.
            from omop_core.authorization import can_access_patient
            from patient_portal.models import PatientUser
            if person_id:
                try:
                    pid = int(person_id)
                except (ValueError, TypeError):
                    return qs.none()
                if not can_access_patient(self.request.user, pid):
                    return qs.none()
            else:
                # No explicit person_id — restrict to the user's own records only.
                try:
                    own_pid = PatientUser.objects.get(identity=self.request.user).person_id
                    qs = qs.filter(person_id=own_pid)
                except PatientUser.DoesNotExist:
                    return qs.none()
        return self._apply_clinical_filters(qs)

    def _apply_clinical_filters(self, qs):
        config = self.clinical_filter_fields
        if not config:
            return qs

        concept_id = self.request.query_params.get(config['concept_param'])
        filter_requested = False
        if concept_id:
            filter_requested = True
            qs = qs.filter(**{config['concept_field']: concept_id})

        source_concept_id = self.request.query_params.get(
            config['source_concept_param'])
        if source_concept_id:
            filter_requested = True
            qs = qs.filter(**{config['source_concept_field']: source_concept_id})

        concept_code = self.request.query_params.get('concept_code')
        if concept_code:
            filter_requested = True
            from omop_core.models import Concept
            cids = list(
                Concept.objects.filter(concept_code=concept_code)
                .values_list('concept_id', flat=True)
            )
            qs = qs.filter(**{f"{config['concept_field']}__in": cids})

        date_gte = self.request.query_params.get(f"{config['date_field']}__gte")
        if date_gte:
            filter_requested = True
            qs = qs.filter(**{f"{config['date_field']}__gte": date_gte})
        date_lte = self.request.query_params.get(f"{config['date_field']}__lte")
        if date_lte:
            filter_requested = True
            qs = qs.filter(**{f"{config['date_field']}__lte": date_lte})

        visit_id = self.request.query_params.get('visit_occurrence_id')
        if visit_id and config.get('visit_filter', True):
            filter_requested = True
            qs = qs.filter(visit_occurrence_id=visit_id)

        ordering = config.get('ordering')
        if filter_requested and ordering:
            qs = qs.order_by(*ordering)
        return qs


#: Maximum rows accepted in a single bulk POST body. Exceeding it is a 413 naming
#: the limit rather than a timeout — callers chunk to this number.
OMOP_BULK_MAX_ROWS = int(os.environ.get('OMOP_BULK_MAX_ROWS', 1000))

#: Byte ceiling for a bulk body, checked against CONTENT_LENGTH *before* parsing.
#: The row cap cannot serve as a memory guard: DRF's JSONParser reads the WSGI
#: stream directly (Request._load_stream sets _stream to the raw HttpRequest),
#: so it never touches HttpRequest.body — the only place Django enforces
#: DATA_UPLOAD_MAX_MEMORY_SIZE. A 500 MB array would therefore be fully parsed
#: into memory before the row count could reject it. 1,000 measurement rows is
#: ~250 KB, so this leaves ample headroom.
OMOP_BULK_MAX_BYTES = int(os.environ.get('OMOP_BULK_MAX_BYTES', 8 * 1024 * 1024))


def _cached_pk_lookup(field, cache):
    """Wrap a PrimaryKeyRelatedField's to_internal_value with a prefetched cache.

    ``PrimaryKeyRelatedField.to_internal_value`` runs ``queryset.get(pk=...)`` per
    row, so a 1,000-row batch with five FK columns costs 5,000 queries — the same
    N+1 at the ORM layer that the endpoint exists to remove at the HTTP layer.
    A cache miss falls through to the original implementation so that unknown or
    malformed ids still raise DRF's own per-index validation error.
    """
    original = field.to_internal_value

    def to_internal_value(data):
        # bool subclasses int, so {1: obj}.get(True) is a HIT and would silently
        # link the row to pk 1. DRF's own to_internal_value raises TypeError for
        # bool before it reaches the queryset, yielding "Incorrect type. Expected
        # pk value, received bool." — so deferring to it here is what produces the
        # 400. Removing this guard turns a rejected row into a mis-linked one.
        # Locked in by test_boolean_fk_is_rejected_not_silently_mislinked.
        if isinstance(data, bool):
            return original(data)
        try:
            hit = cache.get(data)
        except TypeError:      # unhashable (dict/list) — let DRF report it
            hit = None
        return hit if hit is not None else original(data)

    return to_internal_value


def _prefetch_bulk_related(serializer, rows):
    """Resolve every FK referenced anywhere in the batch in one query per model."""
    from rest_framework.relations import PrimaryKeyRelatedField

    for name, field in serializer.child.fields.items():
        if not isinstance(field, PrimaryKeyRelatedField) or field.read_only:
            continue
        # Collect one value at a time rather than in a set comprehension: an
        # unhashable id ({"person": {"id": 5}}) would raise TypeError out of the
        # comprehension, escape as a 500, and lose the per-index 400 the
        # single-row path gives for the same input. Skipping the value here
        # leaves it out of the cache, so _cached_pk_lookup falls through to DRF
        # and the offending row gets its normal validation error.
        raw = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(name)
            if value is None:
                continue
            try:
                raw.add(value)
            except TypeError:
                continue
        if not raw:
            continue
        try:
            objs = {obj.pk: obj for obj in field.get_queryset().filter(pk__in=raw)}
        except (ValueError, TypeError, DjangoValidationError):
            # Non-integer ids in the batch — skip the optimisation and let DRF
            # produce the per-index error for the offending rows.
            continue
        if objs:
            field.to_internal_value = _cached_pk_lookup(field, objs)


#: Identity of a clinical event for the idempotent bulk write, mirroring
#: ``fhir/sync.py::_upsert_clinical``: ``(source_value, date)`` is "the stable
#: identity of a clinical event, independent of how its code resolves", so the
#: concept column stays *outside* the key and is upgraded in place when a
#: vocabulary load resolves a code that previously fell back to 'No matching
#: concept'.
#:
#: Measurement and Observation diverge on purpose. A patient legitimately has
#: several distinct results for one analyte on one day, so keying them on
#: (source_value, date) alone would delete real results instead of deduping.
#:
#: Only raw value columns may join a key. ``value_as_concept`` is re-resolved by
#: a vocabulary load just like the concept column, so keying on it would strand
#: a duplicate beside the row it should have upgraded. ``value_source_value`` is
#: the raw text behind that resolution, so it separates two coded answers safely.
#:
#: Format: model name -> (source_value field, date field, concept field, extra key fields)
_UPSERT_KEYS = {
    'ConditionOccurrence': ('condition_source_value', 'condition_start_date',
                            'condition_concept', ()),
    'DrugExposure':        ('drug_source_value', 'drug_exposure_start_date',
                            'drug_concept', ()),
    'Measurement':         ('measurement_source_value', 'measurement_date',
                            'measurement_concept',
                            ('measurement_datetime', 'value_as_number')),
    'Observation':         ('observation_source_value', 'observation_date',
                            'observation_concept',
                            ('observation_datetime', 'value_as_number',
                             'value_as_string', 'value_source_value')),
    'ProcedureOccurrence': ('procedure_source_value', 'procedure_date',
                            'procedure_concept', ()),
}


def _upsert_key(instance, sv_field, date_field, extra_fields):
    """Event identity of one unsaved row, or None when it has none.

    A row with no source_value or no date cannot be matched against anything, so
    it is always inserted rather than silently merged with every other keyless
    row in the batch. The numeric part needs no normalisation *because both sides
    are Decimal*: value_as_number is a DecimalField, so DRF hands back a Decimal
    and the DB column returns one, and Decimal compares and hashes across
    exponents — Decimal('5.00000') off the row and Decimal('5.0') off the request
    body land in the same bucket. That does not generalise to float: most
    non-integer floats compare unequal to the Decimal of the same literal
    (Decimal('0.1') != 0.1), so a FloatField joining a key here would need
    explicit normalisation to one type.
    """
    sv = getattr(instance, sv_field, None)
    event_date = getattr(instance, date_field, None)
    if not sv or event_date is None:
        return None
    extras = []
    for f in extra_fields:
        v = getattr(instance, f, None)
        # Naive datetimes from the request body will never match the
        # timezone-aware values PostgreSQL returns when USE_TZ=True.
        # Normalise to UTC so both sides of the key comparison hash equally.
        if isinstance(v, datetime) and is_naive(v):
            v = make_aware(v, _dt.timezone.utc)
        extras.append(v)
    return (sv, event_date) + tuple(extras)


class _UpsertPlan:
    """What one bulk batch resolves to once matched against the person's rows.

    Built with a single SELECT and executed with a constant number of statements
    regardless of batch size — the property ``BulkOmopUpsertTest`` pins with
    ``CaptureQueriesContext``. Per-row ``.get()``/``.save()`` here is what the
    endpoint exists to avoid; ``fhir/sync.py::_upsert_clinical`` can afford its
    per-key query because a FHIR compartment is small, a 1,000-row ETL chunk is
    not.
    """

    def __init__(self, to_insert, row_slots, collapse_ids, to_update, touched_ids):
        self.to_insert = to_insert        # unsaved rows needing a pk, in order
        self.row_slots = row_slots        # per input row: ('new', slot) | ('old', pk)
        self.collapse_ids = collapse_ids  # stacked duplicates to delete
        self.to_update = to_update        # [(pk, concept_id)] — concept changed
        self.touched_ids = touched_ids    # existing rows updated or de-stacked


def _plan_bulk_upsert(model_cls, pk_field, model_name, person, instances):
    """Match a batch of unsaved rows against what `person` already has.

    Same three outcomes as ``_upsert_clinical``: an event already on file is left
    in place (its concept updated when it changed, its stacked historical
    duplicates collapsed onto the earliest row), and anything else is inserted.
    Only the concept is rewritten on a matched row — every other column is left
    as stored, so a re-run cannot quietly overwrite a value someone corrected.
    Repeats of one event *within* the batch collapse to a single row, last
    occurrence winning, so a source bundle that reports an event twice does not
    write it twice.
    """
    sv_field, date_field, concept_field, extra_fields = _UPSERT_KEYS[model_name]
    cid_attr = f'{concept_field}_id'

    keys = [_upsert_key(inst, sv_field, date_field, extra_fields)
            for inst in instances]
    keyed = [k for k in keys if k is not None]

    # One SELECT for every row of this person that could match the batch. The
    # (source_value IN …, date IN …) pair is a superset of the real key set —
    # narrowing it to exact tuples would need a per-key OR term, i.e. the query
    # growth this endpoint exists to avoid. The superset is bounded by the
    # person's own rows for those source values.
    existing = {}   # key -> [pk, …] ascending
    existing_cid = {}
    if keyed:
        columns = (pk_field, cid_attr, sv_field, date_field) + tuple(extra_fields)
        rows = model_cls.objects.filter(**{
            'person': person,
            f'{sv_field}__in': {k[0] for k in keyed},
            f'{date_field}__in': {k[1] for k in keyed},
        }).order_by(pk_field).values_list(*columns)
        for row in rows:
            # Normalise DB-side datetimes the same way _upsert_key does for
            # in-memory instances: naive → UTC-aware. In practice PostgreSQL
            # with USE_TZ=True already returns aware values, but the explicit
            # normalisation keeps the two sides provably symmetric.
            tail = []
            for v in row[4:]:
                if isinstance(v, datetime) and is_naive(v):
                    v = make_aware(v, _dt.timezone.utc)
                tail.append(v)
            key = (row[2], row[3]) + tuple(tail)
            if key in existing:
                existing[key].append(row[0])
            else:
                existing[key] = [row[0]]
                existing_cid[key] = row[1]

    # Last occurrence of a repeated key wins, matching _upsert_clinical's
    # "desired" dict — so the batch converges on the row the producer emitted
    # most recently rather than the first one it happened to serialise.
    last_index = {}
    for i, key in enumerate(keys):
        if key is not None:
            last_index[key] = i

    to_insert, row_slots = [], [None] * len(instances)
    insert_slot, collapse_ids, to_update, touched_ids = {}, [], [], []

    for i, (inst, key) in enumerate(zip(instances, keys)):
        if key is None:                       # no identity — always insert
            row_slots[i] = ('new', len(to_insert))
            to_insert.append(inst)
            continue

        if key in existing:
            keep = existing[key][0]
            row_slots[i] = ('old', keep)
            if last_index[key] != i:
                continue                      # decide the key once, on its last row
            extras = existing[key][1:]
            collapse_ids.extend(extras)
            new_cid = getattr(inst, cid_attr, None)
            if existing_cid[key] != new_cid:
                to_update.append((keep, new_cid))
                touched_ids.append(keep)
            elif extras:
                touched_ids.append(keep)
            continue

        if key in insert_slot:                # repeated within this batch
            slot = insert_slot[key]
            if last_index[key] == i:
                to_insert[slot] = inst
        else:
            slot = insert_slot[key] = len(to_insert)
            to_insert.append(inst)
        row_slots[i] = ('new', slot)

    return _UpsertPlan(to_insert, row_slots, collapse_ids, to_update, touched_ids)


def _apply_upsert_plan(plan, model_cls, pk_field, model_name):
    """Execute a plan: collapse duplicates, update changed concepts, insert the
    rest. Returns (ids aligned with the input rows, newly inserted ids)."""
    concept_field = _UPSERT_KEYS[model_name][2]
    cid_attr = f'{concept_field}_id'

    if plan.collapse_ids:
        # Provenance first: it points at rows that are about to disappear, and a
        # GenericForeignKey has no FK cascade to clean it up.
        ProvenanceRecord.objects.filter(
            content_type=ContentType.objects.get_for_model(model_cls),
            object_id__in=plan.collapse_ids,
        ).delete()
        model_cls.objects.filter(**{f'{pk_field}__in': plan.collapse_ids}).delete()

    if plan.to_update:
        model_cls.objects.bulk_update(
            [model_cls(**{pk_field: pk, cid_attr: cid}) for pk, cid in plan.to_update],
            [concept_field],
        )

    new_ids = []
    if plan.to_insert:
        new_ids = list(next_pk_batch(model_cls, pk_field, len(plan.to_insert)))
        for row, pk in zip(plan.to_insert, new_ids):
            setattr(row, pk_field, pk)
        model_cls.objects.bulk_create(plan.to_insert)

    ids = [new_ids[ref] if kind == 'new' else ref for kind, ref in plan.row_slots]
    return ids, new_ids


def _is_admin_actor(request: Request) -> bool:
    """Whether this caller may act on a patient administratively."""
    actor = request.user
    return (
        is_service_token(request)
        or bool(getattr(actor, 'is_staff', False))
        or get_admin_orgs(actor).exists()
    )


def _skip_refresh_requested(request: Request) -> bool:
    """Whether the caller asked to defer, and is allowed to.

    Only actors who can call the refresh action may defer. Otherwise a patient
    could PATCH their own row with the flag and strand their PatientRecord
    stale with no way to rebuild it.
    """
    asked = str(
        request.query_params.get('skip_refresh', 'false')
    ).strip().lower() in ('1', 'true', 'yes')
    return asked and _is_admin_actor(request)


class _OmopDeferRefreshMixin:
    """Let row level writes defer the PatientRecord derivation.

    Derivation cost grows with the rows a person already holds, so on a
    bulk loaded patient one PATCH or DELETE costs 12-32s. A caller that defers
    has to call the refresh action afterwards.

    When *not* deferring, the mixin suppresses the signal-driven refresh and
    calls ``refresh_patient_record`` explicitly so that failures propagate as
    a 502 instead of being swallowed by the signal handler's ``try/except``.
    """

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        from omop_core.signals import suppress_patient_record_refresh

        if _skip_refresh_requested(request):
            with suppress_patient_record_refresh():
                return super().update(request, *args, **kwargs)

        # Suppress signal-driven refresh and call it explicitly so failures
        # surface as an HTTP error instead of being silently swallowed.
        instance = self.get_object()
        person = instance.person
        with suppress_patient_record_refresh():
            response = super().update(request, *args, **kwargs)
        try:
            refresh_patient_record(person)
        except Exception:
            logger.exception(
                'PatientRecord refresh failed after PATCH on %s pk=%s '
                'for person_id=%s',
                type(instance).__name__, instance.pk, person.person_id,
            )
            return Response(
                {'detail': 'The clinical row was updated but the '
                 'PatientRecord projection failed to refresh. '
                 'Retry POST /api/v1/patient-records/'
                 f'{person.person_id}/refresh/ to reconcile.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return response

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        from omop_core.signals import suppress_patient_record_refresh

        if _skip_refresh_requested(request):
            with suppress_patient_record_refresh():
                return super().destroy(request, *args, **kwargs)

        # Capture person before the row is deleted.
        instance = self.get_object()
        person = instance.person
        instance_type = type(instance).__name__
        instance_pk = instance.pk
        with suppress_patient_record_refresh():
            response = super().destroy(request, *args, **kwargs)
        try:
            refresh_patient_record(person)
        except Exception:
            logger.exception(
                'PatientRecord refresh failed after DELETE on %s pk=%s '
                'for person_id=%s',
                instance_type, instance_pk, person.person_id,
            )
            return Response(
                {'detail': 'The clinical row was deleted but the '
                 'PatientRecord projection failed to refresh. '
                 'Retry POST /api/v1/patient-records/'
                 f'{person.person_id}/refresh/ to reconcile.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return response


class _OmopBulkCreateMixin:
    """Accept a JSON *list* on POST and insert the rows in one batched transaction.

    Motivation: consumers that have already parsed FHIR into OMOP-shaped rows were
    writing one row per HTTP request (~1,900 requests for a single patient). This is
    the batch entrance to the same fast internals ``fhir/sync.py::_bulk_insert``
    uses — batched PK allocation plus ``bulk_create`` — without going through FHIR.

    Semantics (summarised in CLAUDE.md, "Bulk OMOP Row Writes"):

    * A single dict body with no client-supplied primary key uses the same
      natural-key upsert semantics, while preserving the single-row response
      shape. Pass ``?upsert=false`` to force the historical append-only create.
    * All-or-nothing: the whole batch shares one ``transaction.atomic()``.
    * One batch is one person. Mixed-person batches are rejected with 400.
    * Idempotent by default (issue #454): rows are upserted on the event identity
      ``_UPSERT_KEYS`` defines, the same identity ``fhir/sync.py::_upsert_clinical``
      uses, so re-posting a batch converges instead of duplicating. The response
      reports ``created`` vs ``updated``; ``ids`` stays positionally aligned with
      the request rows and names the row each one resolved to. ``?upsert=false``
      restores the append-only behaviour.
    * Query count is bounded by a constant, not by the number of rows.
    * ``bulk_create`` does not fire ``post_save``, so the ``omop_core.signals``
      receivers that rebuild ``PatientRecord`` never run. The refresh is therefore
      explicit here — once for the batch instead of once per row. ``?skip_refresh=true``
      defers it for backfills that will run ``populate_patient_record`` afterwards.
    """

    def create(self, request, *args, **kwargs):
        # Checked before request.data, which is what triggers parsing. Chunked
        # uploads send no CONTENT_LENGTH; there is nothing to check pre-parse in
        # that case, and the row cap still bounds what gets written.
        try:
            declared = int(request.META.get('CONTENT_LENGTH') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > OMOP_BULK_MAX_BYTES:
            return Response(
                {'detail': (
                    f'Request body too large: {declared} bytes exceeds the maximum '
                    f'of {OMOP_BULK_MAX_BYTES} bytes. Split the batch and retry.'
                ), 'max_bytes': OMOP_BULK_MAX_BYTES},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if not isinstance(request.data, list):
            if self._should_single_upsert(request):
                return self._single_upsert_create(request)
            return super().create(request, *args, **kwargs)
        return self._bulk_create(request)

    def _should_single_upsert(self, request):
        if not isinstance(request.data, dict):
            return False
        model_name = self.serializer_class.Meta.model.__name__
        if model_name not in _UPSERT_KEYS:
            return False
        pk_field, _model_cls = _MODEL_PK_MAP[model_name]
        if request.data.get(pk_field) is not None:
            return False
        return str(
            request.query_params.get('upsert', 'true')
        ).strip().lower() not in ('0', 'false', 'no')

    def _authorize_single_upsert_person(self, request, person):
        from rest_framework.exceptions import PermissionDenied

        org = get_request_org(request)
        if is_service_token(request):
            return org
        if org is not None:
            existing_pi = PatientRecord.objects.filter(person=person).first()
            if (existing_pi is not None
                    and existing_pi.organization is not None
                    and existing_pi.organization != org):
                raise PermissionDenied('Person does not belong to your organization.')
            return org
        if not getattr(request.user, 'is_staff', False):
            from omop_core.authorization import can_write_patient
            if not can_write_patient(request.user, person.person_id):
                raise PermissionDenied('Access denied.')
        return org

    def _single_upsert_create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        person = validated.get('person')
        if person is None:
            return Response(
                {'detail': 'person is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        org = self._authorize_single_upsert_person(request, person)
        source, source_user_id, reason = _extract_provenance(request)
        skip_refresh = str(
            request.query_params.get('skip_refresh', 'false')
        ).strip().lower() in ('1', 'true', 'yes')

        model_name = self.serializer_class.Meta.model.__name__
        pk_field, model_cls = _MODEL_PK_MAP[model_name]
        instance = model_cls(**dict(validated))

        from omop_core.signals import suppress_patient_record_refresh
        try:
            with transaction.atomic():
                with suppress_patient_record_refresh():
                    plan = _plan_bulk_upsert(
                        model_cls, pk_field, model_name, person, [instance])
                    ids, new_ids = _apply_upsert_plan(
                        plan, model_cls, pk_field, model_name)

                row_id = ids[0]
                if source and new_ids:
                    _record_provenance(
                        model_cls.objects.get(**{pk_field: row_id}),
                        source,
                        source_user_id,
                        target_patient_id=str(person.person_id),
                        modification_reason=reason,
                        organization=org,
                    )
                if not skip_refresh:
                    from omop_core.services.patient_record_service import refresh_patient_record
                    refresh_patient_record(person)
        except IntegrityError as exc:
            logger.exception(
                'single %s upsert for person %s failed on a database constraint',
                model_name, person.person_id)
            return Response(
                {'detail': (
                    'The row conflicted with a database constraint and was '
                    'rolled back; retrying is safe.'
                ), 'error': str(exc).strip().partition('\n')[0]},
                status=status.HTTP_409_CONFLICT,
            )

        obj = model_cls.objects.get(**{pk_field: row_id})
        response_serializer = self.get_serializer(obj)
        http_status = status.HTTP_201_CREATED if new_ids else status.HTTP_200_OK
        return Response(response_serializer.data, status=http_status)

    def _bulk_create(self, request):
        from rest_framework.exceptions import PermissionDenied

        rows = request.data
        if len(rows) > OMOP_BULK_MAX_ROWS:
            return Response(
                {'detail': (
                    f'Batch too large: {len(rows)} rows exceeds the maximum of '
                    f'{OMOP_BULK_MAX_ROWS} rows per request. Split the batch and retry.'
                ), 'max_rows': OMOP_BULK_MAX_ROWS},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        model_name = self.serializer_class.Meta.model.__name__
        pk_field, model_cls = _MODEL_PK_MAP[model_name]

        # Client-supplied PKs would desynchronise the next_pk_batch accounting
        # (some rows allocated, some not) — reject rather than half-honour them.
        pk_errors = [
            ({pk_field: ['Client-supplied primary keys are not allowed in a bulk '
                         'request; ids are assigned by the server.']}
             if isinstance(row, dict) and row.get(pk_field) is not None else {})
            for row in rows
        ]
        if any(pk_errors):
            return Response(pk_errors, status=status.HTTP_400_BAD_REQUEST)

        # many=True yields positionally-aligned per-index error dicts, so the
        # operator can tell which of N rows to fix.
        serializer = self.get_serializer(data=rows, many=True)
        _prefetch_bulk_related(serializer, rows)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        if not validated:
            return Response({'created': 0, 'updated': 0, 'ids': []},
                            status=status.HTTP_201_CREATED)

        # One batch is one person, by construction on the producer side.
        people = {attrs.get('person') for attrs in validated}
        if len(people) > 1:
            return Response(
                {'detail': (
                    'A bulk request must contain rows for exactly one person; '
                    f'found {len(people)} distinct person values. '
                    'Split the batch by person and retry.'
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )
        person = people.pop()
        if person is None:
            return Response(
                {'detail': 'person is required on every row of a bulk request.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Same authorization the single-row path applies in perform_create, run
        # once for the batch's single person.
        org = get_request_org(request)
        if not is_service_token(request):
            if org is not None:
                existing_pi = PatientRecord.objects.filter(person=person).first()
                if (existing_pi is not None
                        and existing_pi.organization is not None
                        and existing_pi.organization != org):
                    raise PermissionDenied('Person does not belong to your organization.')
            elif not getattr(request.user, 'is_staff', False):
                from omop_core.authorization import can_write_patient
                if not can_write_patient(request.user, person.person_id):
                    raise PermissionDenied('Access denied.')

        source, source_user_id, reason = _extract_provenance(request)
        skip_refresh = str(
            request.query_params.get('skip_refresh', 'false')
        ).strip().lower() in ('1', 'true', 'yes')
        # Idempotent by default: the endpoint carries no idempotency key of any
        # kind, so an ETL re-run, a retry after a read timeout, or a re-parse
        # would otherwise duplicate every row it had already written. Callers
        # that genuinely want append-only writes (an audit-style feed of rows
        # with no stable identity) pass ?upsert=false.
        upsert = str(
            request.query_params.get('upsert', 'true')
        ).strip().lower() not in ('0', 'false', 'no')

        from omop_core.signals import suppress_patient_record_refresh

        try:
            return self._bulk_write(
                request, person, org, model_cls, pk_field, model_name, validated,
                source, source_user_id, reason, skip_refresh, upsert,
                suppress_patient_record_refresh)
        except IntegrityError as exc:
            # A conflict over data, not a server fault. The distinction decides
            # whether the caller retries, and a 500 reads as "service is down".
            logger.exception(
                'bulk %s write for person %s failed on a database constraint',
                model_name, person.person_id)
            return Response(
                {'detail': (
                    'The batch conflicted with a database constraint and was '
                    'rolled back whole; no rows were written. Retrying is safe.'
                ), 'error': str(exc).strip().partition('\n')[0]},
                status=status.HTTP_409_CONFLICT,
            )

    def _bulk_write(
        self,
        request: Request,
        person: Person,
        org: Organization | None,
        model_cls: type[models.Model],
        pk_field: str,
        model_name: str,
        validated: list[dict[str, Any]],
        source: str | None,
        source_user_id: str | None,
        reason: str | None,
        skip_refresh: bool,
        upsert: bool,
        suppress_patient_record_refresh: Callable[[], ContextManager[None]],
    ) -> Response:
        """Write one validated batch and derive the read model once."""
        with transaction.atomic():
            # bulk_create does not fire post_save today, so the per-row refresh
            # receivers would not run anyway. Suppressing explicitly is the
            # documented house idiom for bulk writes (omop_core/signals.py) and
            # keeps "one refresh per batch, never per row" true even if a future
            # change introduces a per-row save() in here. The upsert path *does*
            # delete collapsed duplicates, and post_delete receivers are live —
            # so here the suppression is load-bearing, not just defensive.
            with suppress_patient_record_refresh():
                instances = [model_cls(**dict(attrs)) for attrs in validated]
                if upsert:
                    plan = _plan_bulk_upsert(
                        model_cls, pk_field, model_name, person, instances)
                    ids, new_ids = _apply_upsert_plan(
                        plan, model_cls, pk_field, model_name)
                    updated = len(plan.touched_ids)
                else:
                    new_ids = list(next_pk_batch(model_cls, pk_field, len(instances)))
                    for inst, pk in zip(instances, new_ids):
                        setattr(inst, pk_field, pk)
                    model_cls.objects.bulk_create(instances)
                    ids, updated = list(new_ids), 0

            # No source supplied means no ProvenanceRecord, matching the single-row
            # path — inventing a source would make provenance unfalsifiable.
            # Only inserted rows get one: an upsert that left a row untouched
            # wrote nothing to attribute, matching _upsert_clinical.
            if source and new_ids:
                ct = ContentType.objects.get_for_model(model_cls)
                ProvenanceRecord.objects.bulk_create([
                    ProvenanceRecord(
                        source=source,
                        source_user_id=source_user_id or '',
                        target_patient_id=str(person.person_id),
                        modification_reason=reason,
                        organization=org,
                        content_type=ct,
                        object_id=pk,
                    )
                    for pk in new_ids
                ])

            # Deliberately inside the transaction, and deliberately unguarded:
            # a failing derivation rolls the rows back and 500s rather than
            # leaving a landed batch with a stale read model, which is the
            # silent-corruption case this endpoint exists to avoid.
            #
            # This is a considered divergence from the single-row path, where
            # _refresh_for_instance swallows the same exception and logs a
            # warning (omop_core/signals.py). There, one stale row is a small
            # blast radius and failing the write would be disproportionate; here
            # the caller is a pipeline that retries whole batches, so a loud
            # rollback is both recoverable and the safer default. Callers who
            # want the rows regardless can pass ?skip_refresh=true and derive
            # separately.
            if not skip_refresh:
                refresh_patient_record(person)

        return Response(
            {'created': len(new_ids), 'updated': updated, 'ids': list(ids)},
            status=status.HTTP_201_CREATED,
        )


class _ProvenanceMixin:
    """Record provenance on create/update when source headers/body fields are present."""
    def _prov(self, obj):
        source, user_id, reason = _extract_provenance(self.request)
        if source:
            _record_provenance(obj, source, user_id, modification_reason=reason, organization=get_request_org(self.request))

    def perform_create(self, serializer):
        # Auto-generate PK if not supplied
        model_name = serializer.Meta.model.__name__
        if model_name in _MODEL_PK_MAP:
            pk_field, model_cls = _MODEL_PK_MAP[model_name]
            if pk_field not in serializer.validated_data:
                serializer.validated_data[pk_field] = next_pk(model_cls, pk_field)

        # Trusted backend (service-token): skip ACL — already validated at
        # the permission layer (ScopedTokenPermission).
        if is_service_token(self.request):
            obj = serializer.save()
            self._prov(obj)
            return

        # Org-scoping: reject cross-org persons; allow new/bootstrap patients
        org = get_request_org(self.request)
        if org is not None:
            person = serializer.validated_data.get('person')
            if person:
                from rest_framework.exceptions import PermissionDenied
                # Allow bootstrap (no PatientRecord yet) and unclaimed patients (org=NULL).
                # Block only when a PatientRecord exists and is already claimed by a different org.
                existing_pi = PatientRecord.objects.filter(person=person).first()
                if (existing_pi is not None
                        and existing_pi.organization is not None
                        and existing_pi.organization != org):
                    raise PermissionDenied('Person does not belong to your organization.')
        elif not getattr(self.request.user, 'is_staff', False):
            from omop_core.authorization import can_write_patient
            from rest_framework.exceptions import PermissionDenied
            person = serializer.validated_data.get('person')
            if not person:
                raise PermissionDenied('person is required.')
            if not can_write_patient(self.request.user, person.person_id):
                raise PermissionDenied('Access denied.')

        obj = serializer.save()
        self._prov(obj)

    def perform_update(self, serializer):
        # Trusted backend (service-token): skip ACL — already validated at
        # the permission layer (ScopedTokenPermission).
        if is_service_token(self.request):
            obj = serializer.save()
            self._prov(obj)
            return

        org = get_request_org(self.request)
        if org is not None:
            person = serializer.validated_data.get('person') or serializer.instance.person
            from rest_framework.exceptions import NotFound, PermissionDenied
            # On updates the patient must already have a PatientRecord; missing = not found.
            # Unclaimed patients (org=NULL) are allowed; only reject explicit cross-org.
            existing_pi = PatientRecord.objects.filter(person=person).first()
            if existing_pi is None:
                raise NotFound('Person not found.')
            if existing_pi.organization is not None and existing_pi.organization != org:
                raise PermissionDenied('Person does not belong to your organization.')
        elif not getattr(self.request.user, 'is_staff', False):
            from omop_core.authorization import can_write_patient
            from rest_framework.exceptions import PermissionDenied
            person = serializer.validated_data.get('person') or serializer.instance.person
            if not person:
                raise PermissionDenied('person is required.')
            if not can_write_patient(self.request.user, person.person_id):
                raise PermissionDenied('Access denied.')
        obj = serializer.save()
        self._prov(obj)


@method_decorator(csrf_exempt, name='dispatch')
class ConditionOccurrenceViewSet(_OmopDeferRefreshMixin, _OmopBulkCreateMixin, _ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ConditionOccurrenceSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = ConditionOccurrence.objects.all()
    clinical_filter_fields = {
        'concept_param': 'condition_concept_id',
        'concept_field': 'condition_concept_id',
        'source_concept_param': 'condition_source_concept_id',
        'source_concept_field': 'condition_source_concept_id',
        'date_field': 'condition_start_date',
        'ordering': ('-condition_start_date', '-condition_occurrence_id'),
    }
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'omop_write'


@method_decorator(csrf_exempt, name='dispatch')
class DrugExposureViewSet(_OmopDeferRefreshMixin, _OmopBulkCreateMixin, _ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = DrugExposureSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = DrugExposure.objects.all()
    clinical_filter_fields = {
        'concept_param': 'drug_concept_id',
        'concept_field': 'drug_concept_id',
        'source_concept_param': 'drug_source_concept_id',
        'source_concept_field': 'drug_source_concept_id',
        'date_field': 'drug_exposure_start_date',
        'ordering': ('-drug_exposure_start_date', '-drug_exposure_id'),
    }
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'omop_write'


@method_decorator(csrf_exempt, name='dispatch')
class MeasurementViewSet(_OmopDeferRefreshMixin, _OmopBulkCreateMixin, _ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = MeasurementSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = Measurement.objects.all()
    clinical_filter_fields = {
        'concept_param': 'measurement_concept_id',
        'concept_field': 'measurement_concept_id',
        'source_concept_param': 'measurement_source_concept_id',
        'source_concept_field': 'measurement_source_concept_id',
        'date_field': 'measurement_date',
        'ordering': ('-measurement_date', '-measurement_id'),
    }
    ordering_fields = ['measurement_date', 'measurement_id']
    ordering = ['-measurement_date']
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'omop_write'


@method_decorator(csrf_exempt, name='dispatch')
class ObservationViewSet(_OmopDeferRefreshMixin, _OmopBulkCreateMixin, _ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ObservationSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = Observation.objects.all()
    clinical_filter_fields = {
        'concept_param': 'observation_concept_id',
        'concept_field': 'observation_concept_id',
        'source_concept_param': 'observation_source_concept_id',
        'source_concept_field': 'observation_source_concept_id',
        'date_field': 'observation_date',
        'ordering': ('-observation_date', '-observation_id'),
    }
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'omop_write'


@method_decorator(csrf_exempt, name='dispatch')
class ProcedureOccurrenceViewSet(_OmopDeferRefreshMixin, _OmopBulkCreateMixin, _ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ProcedureOccurrenceSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = ProcedureOccurrence.objects.all()
    clinical_filter_fields = {
        'concept_param': 'procedure_concept_id',
        'concept_field': 'procedure_concept_id',
        'source_concept_param': 'procedure_source_concept_id',
        'source_concept_field': 'procedure_source_concept_id',
        'date_field': 'procedure_date',
        'ordering': ('-procedure_date', '-procedure_occurrence_id'),
    }
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'omop_write'


@method_decorator(csrf_exempt, name='dispatch')
class EpisodeViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = EpisodeSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = Episode.objects.all()
    allowed_list_query_params = (
        frozenset({'person_id', 'format'})
        | _OmopFilterMixin.pagination_query_params
    )
    clinical_filter_fields = {
        'concept_param': 'episode_concept_id',
        'concept_field': 'episode_concept_id',
        'source_concept_param': 'episode_source_concept_id',
        'source_concept_field': 'episode_source_concept_id',
        'date_field': 'episode_start_date',
        'ordering': ('-episode_start_date', '-episode_id'),
        'visit_filter': False,
    }


@method_decorator(csrf_exempt, name='dispatch')
class EpisodeEventViewSet(viewsets.ModelViewSet):
    serializer_class = EpisodeEventSerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    allowed_list_query_params = frozenset({'episode_id', 'person_id', 'format'})

    def _unsupported_list_query_params(self):
        return sorted(set(self.request.query_params) - self.allowed_list_query_params)

    def list(self, request, *args, **kwargs):
        unsupported = self._unsupported_list_query_params()
        if unsupported:
            return Response(
                {
                    'detail': (
                        'Unsupported query parameter(s): '
                        + ', '.join(unsupported)
                    ),
                    'unsupported_params': unsupported,
                    'supported_params': sorted(self.allowed_list_query_params),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not request.query_params.get('episode_id'):
            return Response(
                {'detail': 'episode_id query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        qs = EpisodeEvent.objects.all()
        episode_id = self.request.query_params.get('episode_id')
        if episode_id:
            qs = qs.filter(episode_id=episode_id)
        # Trusted backend (service-token): full visibility across all episode events.
        if is_service_token(self.request):
            return qs
        # Org / per-patient scoping: EpisodeEvent.episode_id is a bare integer FK to Episode.
        # Resolve allowed episode_ids via the Episode → person → org chain.
        # Org-scoped tokens see only their org's patients (not unassigned).
        org = get_request_org(self.request)
        if org is not None:
            allowed_pids = PatientRecord.objects.filter(
                organization=org
            ).values('person_id')
            allowed_episodes = Episode.objects.filter(person_id__in=allowed_pids).values('episode_id')
            qs = qs.filter(episode_id__in=allowed_episodes)
        elif self.request.user and not getattr(self.request.user, 'is_staff', False):
            from omop_core.authorization import can_access_patient
            from patient_portal.models import PatientUser
            person_id = self.request.query_params.get('person_id')
            if person_id:
                try:
                    pid = int(person_id)
                except (ValueError, TypeError):
                    return qs.none()
                if not can_access_patient(self.request.user, pid):
                    return qs.none()
                allowed_episodes = Episode.objects.filter(person_id=pid).values('episode_id')
                qs = qs.filter(episode_id__in=allowed_episodes)
            else:
                try:
                    own_pid = PatientUser.objects.get(identity=self.request.user).person_id
                    allowed_episodes = Episode.objects.filter(person_id=own_pid).values('episode_id')
                    qs = qs.filter(episode_id__in=allowed_episodes)
                except PatientUser.DoesNotExist:
                    return qs.none()
        return qs

    def perform_create(self, serializer):
        from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
        # Trusted backend (service-token): skip ACL — full cross-patient write access.
        if is_service_token(self.request):
            serializer.save()
            return
        episode_id = serializer.validated_data.get('episode_id')
        org = get_request_org(self.request)
        if org is not None:
            # Fail closed: if episode_id is absent the org check cannot be performed.
            if episode_id is None:
                raise ValidationError({'episode_id': 'This field is required.'})
            try:
                episode = Episode.objects.get(episode_id=episode_id)
            except Episode.DoesNotExist:
                raise NotFound('Episode not found.')
            pi = PatientRecord.objects.filter(person_id=episode.person_id).first()
            if pi is not None and pi.organization is not None and pi.organization != org:
                raise PermissionDenied('Episode does not belong to your organization.')
        elif self.request.user and not getattr(self.request.user, 'is_staff', False):
            # Non-org path (partner-auth / session patients): enforce per-patient ownership.
            from omop_core.authorization import can_access_patient
            if episode_id is not None:
                episode = Episode.objects.filter(episode_id=episode_id).first()
                if episode is None or not can_access_patient(self.request.user, episode.person_id):
                    raise PermissionDenied('Access denied.')
        serializer.save()


# =============================================================================
# OMOP concept lookup
# GET /api/concepts/lookup/?lookup=LOINC:2160-0&lookup=SNOMED:44054006
# =============================================================================

@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_lookup(request):
    """
    Batch translate (vocabulary_id, concept_code) pairs to concept_id.

    Query params (repeatable):
        lookup=VOCAB_ID:concept_code
        include_versions=1   (optional) — also return a top-level
                             `_vocabulary_versions` map {vocab_id: version}

    Response 200:
        { "LOINC": { "2160-0": 3013682, "2345-7": null }, "SNOMED": { ... } }

    Unknown codes return null; healthkey-etl substitutes concept_id=0 downstream.
    The default `{vocab: {code: id}}` shape is frozen; `include_versions=1` is
    additive so consumers can pin a vocabulary release / detect drift (promop#240).
    """
    from omop_core.models import Concept as OmopConcept

    raw_pairs = request.query_params.getlist('lookup')
    if not raw_pairs:
        return Response(
            {'detail': 'At least one ?lookup=VOCAB:code parameter is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Parse and group by vocabulary
    by_vocab: dict[str, set[str]] = {}
    for pair in raw_pairs:
        if ':' not in pair:
            return Response(
                {'detail': f"Malformed lookup value '{pair}'. Expected format: VOCAB_ID:concept_code"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        vocab_id, concept_code = pair.split(':', 1)
        by_vocab.setdefault(vocab_id, set()).add(concept_code)

    # Build result skeleton: all codes default to null
    result: dict[str, dict[str, int | None]] = {
        vocab: {code: None for code in codes}
        for vocab, codes in by_vocab.items()
    }

    # Single query across all requested (vocab, code) pairs
    all_vocab_ids = list(by_vocab.keys())
    all_codes = list({c for codes in by_vocab.values() for c in codes})
    hits = OmopConcept.objects.filter(
        vocabulary_id__in=all_vocab_ids,
        concept_code__in=all_codes,
    ).order_by('concept_id').values('vocabulary_id', 'concept_code', 'concept_id')

    for row in hits:
        v, c, cid = row['vocabulary_id'], row['concept_code'], row['concept_id']
        if v in result and c in result[v]:
            result[v][c] = cid

    # Opt-in: add a top-level `_vocabulary_versions` map so consumers can pin a
    # release / detect drift (promop#240). Off by default to keep the frozen
    # `{vocab: {code: id}}` shape that healthkey-etl reads.
    if request.query_params.get('include_versions') in ('1', 'true', 'True', 'yes') \
            and '_vocabulary_versions' not in result:
        # Guard: never clobber a user-requested vocabulary bucket that happens to
        # be literally named `_vocabulary_versions` (not a real OMOP vocab id).
        version_map = _vocab_version_map()
        result['_vocabulary_versions'] = {v: version_map.get(v) for v in by_vocab}

    return _set_release_etag(request, Response(result))


# Caps for concept graph traversal: bound any single source concept's result
# set and the batch fan-out so one request cannot blow up the worker.
CONCEPT_GRAPH_MAX_RESULTS_PER_SOURCE = 1000
CONCEPT_GRAPH_MAX_BATCH_IDS = 200


def _parse_positive_int(raw_value, field_name):
    if raw_value in (None, ''):
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"'{field_name}' must be an integer.")
    if value < 1:
        raise ValueError(f"'{field_name}' must be >= 1.")
    return value


def _concept_graph_filters(request):
    relationship_ids = request.query_params.getlist('relationship_id')
    vocabulary_ids = request.query_params.getlist('vocabulary_id')
    concept_class_ids = request.query_params.getlist('concept_class_id')
    max_levels = _parse_positive_int(request.query_params.get('max_levels'), 'max_levels')
    return relationship_ids, vocabulary_ids, concept_class_ids, max_levels


import threading

_vocab_cache_lock = threading.Lock()
_vocab_version_cache = {'release_pk': None, 'map': None}


def _vocab_version_map():
    """``{vocabulary_id: vocabulary_version}`` for every vocabulary (small table,
    one query). Lets concept responses carry the release/version each concept
    came from, so consumers can pin a release and detect drift (promop#240).

    Cached per-process, invalidated when a new VocabularyRelease is published."""
    from omop_core.models import Vocabulary
    from omop_core.services.vocab_release import get_latest_release

    release = get_latest_release()
    if release is None:
        # No published release — skip caching, just query directly.
        return dict(Vocabulary.objects.values_list('vocabulary_id', 'vocabulary_version'))
    release_pk = release.pk
    with _vocab_cache_lock:
        if _vocab_version_cache['release_pk'] == release_pk and _vocab_version_cache['map'] is not None:
            return _vocab_version_cache['map']
        result = dict(Vocabulary.objects.values_list('vocabulary_id', 'vocabulary_version'))
        _vocab_version_cache['release_pk'] = release_pk
        _vocab_version_cache['map'] = result
        return result


def _etag_matches(if_none_match, etag):
    """RFC 7232 §3.2 weak comparison for If-None-Match on GET/HEAD."""
    if not if_none_match or not etag:
        return False
    if if_none_match.strip() == '*':
        return True

    def _normalize(e):
        e = e.strip()
        if e.startswith('W/'):
            e = e[2:]
        return e
    etag_norm = _normalize(etag)
    for token in if_none_match.split(','):
        if _normalize(token) == etag_norm:
            return True
    return False


def _set_release_etag(request, response):
    """Set ETag and Cache-Control on a concept response based on the latest
    published VocabularyRelease. Returns a 304 if If-None-Match matches."""
    from django.http import HttpResponseNotModified
    from omop_core.services.vocab_release import get_latest_release, get_release_etag

    release = get_latest_release()
    etag = get_release_etag(release)
    if etag is None:
        return response

    if_none_match = request.META.get('HTTP_IF_NONE_MATCH', '')
    if _etag_matches(if_none_match, etag):
        return HttpResponseNotModified()

    response['ETag'] = etag
    response['Cache-Control'] = 'public, max-age=300'
    return response


def _serialize_concept_graph_node(concept, versions=None, **extra):
    payload = {
        'concept_id': concept.concept_id,
        'concept_name': concept.concept_name,
        'concept_code': concept.concept_code,
        'vocabulary_id': concept.vocabulary_id,
        'vocabulary_version': (versions or {}).get(concept.vocabulary_id),
        'concept_class_id': concept.concept_class_id,
        'domain_id': concept.domain_id,
        'standard_concept': concept.standard_concept,
    }
    payload.update(extra)
    return payload


def _query_concept_graph(source_ids, direction, relationship_ids, vocabulary_ids, concept_class_ids, max_levels):
    """Traverse the concept graph for the given source concepts.

    Returns (grouped, truncated) where grouped maps each source id to at most
    CONCEPT_GRAPH_MAX_RESULTS_PER_SOURCE serialized nodes, and truncated holds
    the source ids whose full result set exceeded that cap.

    Direction semantics follow the stored edge direction: in relationship mode,
    'ancestors' returns in-neighbors (concepts with an edge pointing AT the
    source) and 'descendants' returns out-neighbors (concepts the source points
    TO). For OMOP hierarchical relationships like 'Is a' (authored child ->
    parent), use the default concept_ancestor closure mode for true
    parent/ancestor traversal instead.
    """
    grouped = {source_id: [] for source_id in source_ids}
    truncated = set()
    versions = _vocab_version_map()

    def _append(source_id, node):
        bucket = grouped[source_id]
        if len(bucket) >= CONCEPT_GRAPH_MAX_RESULTS_PER_SOURCE:
            truncated.add(source_id)
            return False
        bucket.append(node)
        return True

    def _drain(edges, source_attr, node):
        for edge in edges.iterator():
            source_id = getattr(edge, source_attr)
            _append(source_id, node(edge))
            if len(truncated) == len(grouped):
                break

    if relationship_ids:
        qs = (
            ConceptRelationship.objects
            .select_related('concept_1', 'concept_2')
            .filter(relationship_id__in=relationship_ids, invalid_reason__isnull=True)
        )
        if direction == 'ancestors':
            qs = qs.filter(concept_2_id__in=source_ids)
            if vocabulary_ids:
                qs = qs.filter(concept_1__vocabulary_id__in=vocabulary_ids)
            if concept_class_ids:
                qs = qs.filter(concept_1__concept_class_id__in=concept_class_ids)
            _drain(
                qs.order_by('relationship_id', 'concept_1_id'),
                'concept_2_id',
                lambda edge: _serialize_concept_graph_node(
                    edge.concept_1,
                    versions=versions,
                    relationship_id=edge.relationship_id,
                    min_levels_of_separation=None,
                    max_levels_of_separation=None,
                ),
            )
        else:
            qs = qs.filter(concept_1_id__in=source_ids)
            if vocabulary_ids:
                qs = qs.filter(concept_2__vocabulary_id__in=vocabulary_ids)
            if concept_class_ids:
                qs = qs.filter(concept_2__concept_class_id__in=concept_class_ids)
            _drain(
                qs.order_by('relationship_id', 'concept_2_id'),
                'concept_1_id',
                lambda edge: _serialize_concept_graph_node(
                    edge.concept_2,
                    versions=versions,
                    relationship_id=edge.relationship_id,
                    min_levels_of_separation=None,
                    max_levels_of_separation=None,
                ),
            )
        return grouped, truncated

    qs = ConceptAncestor.objects.select_related('ancestor_concept', 'descendant_concept')
    if direction == 'ancestors':
        qs = qs.filter(descendant_concept_id__in=source_ids).exclude(
            ancestor_concept_id=F('descendant_concept_id')
        )
        if vocabulary_ids:
            qs = qs.filter(ancestor_concept__vocabulary_id__in=vocabulary_ids)
        if concept_class_ids:
            qs = qs.filter(ancestor_concept__concept_class_id__in=concept_class_ids)
        if max_levels is not None:
            qs = qs.filter(min_levels_of_separation__lte=max_levels)
        _drain(
            qs.order_by('min_levels_of_separation', 'ancestor_concept_id'),
            'descendant_concept_id',
            lambda edge: _serialize_concept_graph_node(
                edge.ancestor_concept,
                versions=versions,
                relationship_id=None,
                min_levels_of_separation=edge.min_levels_of_separation,
                max_levels_of_separation=edge.max_levels_of_separation,
            ),
        )
    else:
        qs = qs.filter(ancestor_concept_id__in=source_ids).exclude(
            ancestor_concept_id=F('descendant_concept_id')
        )
        if vocabulary_ids:
            qs = qs.filter(descendant_concept__vocabulary_id__in=vocabulary_ids)
        if concept_class_ids:
            qs = qs.filter(descendant_concept__concept_class_id__in=concept_class_ids)
        if max_levels is not None:
            qs = qs.filter(min_levels_of_separation__lte=max_levels)
        _drain(
            qs.order_by('min_levels_of_separation', 'descendant_concept_id'),
            'ancestor_concept_id',
            lambda edge: _serialize_concept_graph_node(
                edge.descendant_concept,
                versions=versions,
                relationship_id=None,
                min_levels_of_separation=edge.min_levels_of_separation,
                max_levels_of_separation=edge.max_levels_of_separation,
            ),
        )
    return grouped, truncated


def _concept_graph_single_response(request, concept_id, direction):
    try:
        relationship_ids, vocabulary_ids, concept_class_ids, max_levels = _concept_graph_filters(request)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if not Concept.objects.filter(concept_id=concept_id).exists():
        return Response({'detail': 'Concept not found.'}, status=status.HTTP_404_NOT_FOUND)

    grouped, truncated = _query_concept_graph(
        [concept_id],
        direction,
        relationship_ids,
        vocabulary_ids,
        concept_class_ids,
        max_levels,
    )
    results = grouped[concept_id]
    return _set_release_etag(request, Response({
        'concept_id': concept_id,
        'direction': direction,
        'count': len(results),
        'truncated': concept_id in truncated,
        'results': results,
    }))


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_ancestors(request, concept_id):
    return _concept_graph_single_response(request, concept_id, 'ancestors')


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_descendants(request, concept_id):
    return _concept_graph_single_response(request, concept_id, 'descendants')


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_graph_batch(request):
    raw_ids = request.query_params.getlist('concept_id')
    if not raw_ids:
        return Response(
            {'detail': 'At least one ?concept_id=<id> parameter is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    direction = request.query_params.get('direction', '').strip().lower()
    if direction not in {'ancestors', 'descendants'}:
        return Response(
            {'detail': "'direction' must be either 'ancestors' or 'descendants'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        concept_ids = [int(raw_id) for raw_id in raw_ids]
    except ValueError:
        return Response(
            {'detail': "'concept_id' values must be integers."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(concept_ids) > CONCEPT_GRAPH_MAX_BATCH_IDS:
        return Response(
            {'detail': f"At most {CONCEPT_GRAPH_MAX_BATCH_IDS} 'concept_id' values are allowed per request."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        relationship_ids, vocabulary_ids, concept_class_ids, max_levels = _concept_graph_filters(request)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    grouped, truncated = _query_concept_graph(
        concept_ids,
        direction,
        relationship_ids,
        vocabulary_ids,
        concept_class_ids,
        max_levels,
    )
    return _set_release_etag(request, Response({
        'direction': direction,
        'results': {
            str(concept_id): grouped.get(concept_id, [])
            for concept_id in concept_ids
        },
        'truncated': sorted(truncated),
    }))


# =============================================================================
# OMOP concept search / browse (issue #213)
# GET /api/v1/concepts/search/?q=creatinine&vocabulary_id=LOINC
# GET /api/v1/concepts/?domain_id=Measurement&concept_class_id=Lab+Test
# =============================================================================

class ConceptPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


# Query params accepted as exact-match filters by both concept endpoints.
_CONCEPT_FILTER_PARAMS = ('vocabulary_id', 'domain_id', 'concept_class_id', 'standard_concept')

# Filters selective enough to bound a listing on their own. standard_concept is
# deliberately excluded: it has ~3 distinct values and no index, so it cannot
# stand alone against a fully loaded (multi-million-row) concept table.
_CONCEPT_SELECTIVE_PARAMS = ('vocabulary_id', 'domain_id', 'concept_class_id')


def _apply_concept_filters(queryset, query_params):
    for param in _CONCEPT_FILTER_PARAMS:
        value = query_params.get(param)
        if value:
            queryset = queryset.filter(**{param: value})
    return queryset


def _serialize_concept(concept, versions=None):
    return {
        'concept_id': concept.concept_id,
        'concept_name': concept.concept_name,
        'vocabulary_id': concept.vocabulary_id,
        'vocabulary_version': (versions or {}).get(concept.vocabulary_id),
        'concept_code': concept.concept_code,
        'domain_id': concept.domain_id,
        'concept_class_id': concept.concept_class_id,
        'standard_concept': concept.standard_concept,
    }


def _paginated_concept_response(queryset, request):
    # Order by the pk: concept_name has only a GIN trigram index (usable for
    # icontains, not ORDER BY), so sorting by name would force a full sort of
    # the matched set on every page request.
    paginator = ConceptPagination()
    page = paginator.paginate_queryset(queryset.order_by('concept_id'), request)
    versions = _vocab_version_map()
    response = paginator.get_paginated_response([_serialize_concept(c, versions) for c in page])
    return _set_release_etag(request, response)


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_search(request):
    """
    Search OMOP concepts by name (case-insensitive substring).

    Query params:
        q                 required, minimum 3 characters
        vocabulary_id     optional exact-match filter (e.g. LOINC, SNOMED)
        domain_id         optional exact-match filter (e.g. Measurement)
        concept_class_id  optional exact-match filter (e.g. Lab Test)
        standard_concept  optional exact-match filter (S or C)
        page / page_size  pagination (page_size capped at 100)

    Response 200: paginated {count, next, previous, results: [concept, ...]}
    """
    # Minimum 3 chars: a pg_trgm trigram is 3 chars, so shorter queries can't use
    # the GIN trigram index on UPPER(concept_name) and would seq-scan the (large)
    # concept table (#262). Matches concepts/synonyms/ minimum.
    query = (request.query_params.get('q') or '').strip()
    if len(query) < 3:
        return Response(
            {'detail': "Query parameter 'q' is required and must be at least 3 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    queryset = _apply_concept_filters(
        Concept.objects.filter(concept_name__icontains=query),
        request.query_params,
    )
    return _paginated_concept_response(queryset, request)


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_list(request):
    """
    List OMOP concepts filtered by vocabulary, domain, concept class,
    or standard-concept flag.

    At least one of vocabulary_id, domain_id, or concept_class_id is
    required — the concept table can hold millions of rows, so a listing
    bounded only by standard_concept (or nothing) is rejected.

    Query params:
        vocabulary_id, domain_id, concept_class_id  (at least one required)
        standard_concept  optional additional filter (S or C)
        page / page_size  pagination (page_size capped at 100)

    Response 200: paginated {count, next, previous, results: [concept, ...]}
    """
    if not any(request.query_params.get(p) for p in _CONCEPT_SELECTIVE_PARAMS):
        return Response(
            {'detail': 'At least one of these filters is required: '
                       + ', '.join(_CONCEPT_SELECTIVE_PARAMS) + '.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    queryset = _apply_concept_filters(Concept.objects.all(), request.query_params)
    return _paginated_concept_response(queryset, request)


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_synonyms(request, concept_id):
    """
    List the synonyms (alternate names) for one OMOP concept, so a consumer
    mirroring promop's vocabulary can cache them (promop#239).

    Response 200: { "concept_id": N, "count": M, "results": [
        { "concept_synonym_name": "...", "language_concept_id": 4180186 }, ... ] }
    Response 404: concept_id not found.
    """
    if not Concept.objects.filter(concept_id=concept_id).exists():
        return Response({'detail': 'Concept not found.'}, status=status.HTTP_404_NOT_FOUND)
    rows = list(
        ConceptSynonym.objects
        .filter(concept_id=concept_id)
        .order_by('concept_synonym_name')
        .values('concept_synonym_name', 'language_concept_id')
    )
    return _set_release_etag(request, Response({'concept_id': concept_id, 'count': len(rows), 'results': rows}))


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_synonym_search(request):
    """
    Find concepts by a synonym (alternate name) substring — the reverse of
    `concepts/lookup/`, for alias resolution (e.g. regimen alias 'VRd' → the
    HemOnc concept). Backed by a GIN trigram index on `concept_synonym_name`.

    Query params:
        q                 required, minimum 3 characters
        vocabulary_id     optional exact-match filter on the concept
        concept_class_id  optional exact-match filter on the concept
        page / page_size  pagination (page_size capped at 100)

    Response 200: paginated {count, next, previous, results: [
        { concept_id, concept_name, vocabulary_id, concept_code,
          concept_class_id, standard_concept, concept_synonym_name }, ... ]}
    """
    # Minimum 3 chars: a pg_trgm trigram is 3 chars, so shorter queries could
    # not use the GIN trigram index and would force a full table scan.
    query = (request.query_params.get('q') or '').strip()
    if len(query) < 3:
        return Response(
            {'detail': "Query parameter 'q' is required and must be at least 3 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    qs = ConceptSynonym.objects.select_related('concept').filter(
        concept_synonym_name__icontains=query,
    )
    vocabulary_id = request.query_params.get('vocabulary_id')
    if vocabulary_id:
        qs = qs.filter(concept__vocabulary_id=vocabulary_id)
    concept_class_id = request.query_params.get('concept_class_id')
    if concept_class_id:
        qs = qs.filter(concept__concept_class_id=concept_class_id)

    paginator = ConceptPagination()
    page = paginator.paginate_queryset(qs.order_by('concept_id', 'concept_synonym_name'), request)
    results = [{
        'concept_id': s.concept_id,
        'concept_name': s.concept.concept_name,
        'vocabulary_id': s.concept.vocabulary_id,
        'concept_code': s.concept.concept_code,
        'concept_class_id': s.concept.concept_class_id,
        'standard_concept': s.concept.standard_concept,
        'concept_synonym_name': s.concept_synonym_name,
    } for s in page]
    return _set_release_etag(request, paginator.get_paginated_response(results))


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_replacement(request, concept_id):
    """
    Resolve a (possibly deprecated) concept to its active replacement.

    Embedded-term substitution (PHR-S FM TI.4.2#07) in an OMOP-based store
    reduces to a concept-replacement lookup: when a terminology release retires
    a concept, Athena loads a `'Concept replaced by'` edge to the successor.
    This endpoint walks that chain and returns the terminal active concept.

    Response 200: {
        "concept_id": N,                 # requested concept
        "replaced": true|false,          # whether a substitution was applied
        "resolved_concept": { concept_id, concept_name, concept_code,
                              vocabulary_id, concept_class_id, domain_id,
                              standard_concept, invalid_reason },
        "chain": [id0, id1, ...]         # traversal order (>=1 entry)
    }
    Response 404: concept_id not found.
    """
    from omop_core.models import resolve_concept_replacement

    resolved, chain = resolve_concept_replacement(concept_id)
    if resolved is None:
        return Response({'detail': 'Concept not found.'}, status=status.HTTP_404_NOT_FOUND)

    versions = _vocab_version_map()
    return Response({
        'concept_id': concept_id,
        'replaced': resolved.concept_id != concept_id,
        'resolved_concept': _serialize_concept_graph_node(
            resolved, versions, invalid_reason=resolved.invalid_reason,
        ),
        'chain': chain,
    })


# =============================================================================
# Controlled vocabulary endpoints
# GET /api/vocabularies/<model_name>/ → [{code, title}, ...]
# =============================================================================

_VOCABULARY_REGISTRY = {
    'ethnicity':                     Ethnicity,
    'stem-cell-transplant':          StemCellTransplant,
    'sct-eligibility':               SctEligibility,
    'histologic-type':               HistologicType,
    'estrogen-receptor-status':      EstrogenReceptorStatus,
    'progesterone-receptor-status':  ProgesteroneReceptorStatus,
    'her2-status':                   Her2Status,
    'hr-status':                     HrStatus,
    'hrd-status':                    HrdStatus,
    'mutation-origin':               MutationOrigin,
    'mutation-gene':                 MutationGene,
    'mutation-interpretation':       MutationInterpretation,
    'mutation-code':                 MutationCode,
    'tumor-stage':                   TumorStage,
    'nodes-stage':                   NodesStage,
    'distant-metastasis-stage':      DistantMetastasisStage,
    'staging-modality':              StagingModality,
    'toxicity-grade':                ToxicityGrade,
    'language':                      Language,
    'language-skill-level':          LanguageSkillLevel,
    'binet-stage':                   BinetStage,
    'protein-expression':            ProteinExpression,
    'richter-transformation':        RichterTransformation,
    'tumor-burden':                  TumorBurden,
    'morphologic-variant':           MorphologicVariant,
    'disease-activity':              DiseaseActivity,
    'pre-existing-condition-category': PreExistingConditionCategory,
    'disease':                         Disease,
    'cancer-stage':                    CancerStage,
    'karnofsky-score':                 KarnofskyScore,
    'ecog-status':                     EcogStatus,
    'peripheral-neuropathy-grade':     PeripheralNeuropathyGrade,
    'infection-status':                InfectionStatus,
    'disease-progression':             DiseaseProgression,
    'measurable-disease':              MeasurableDisease,
    'gelf-criteria':                   GelfCriteria,
    'flipi-score':                     FlipIScore,
    'follicular-lymphoma-grade':             FollicularLymphomaGrade,
    'post-transformation-outcome':           PostTransformationOutcome,
    'breast-cancer-first-line-therapy':      BreastCancerFirstLineTherapy,
    'breast-cancer-second-line-therapy':     BreastCancerSecondLineTherapy,
    'breast-cancer-later-line-therapy':      BreastCancerLaterLineTherapy,
    'myeloma-type':                          MyelomaType,
}


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vocabulary_list(request, model_name):
    """Return all entries for a controlled vocabulary model as [{code, title}]."""
    model = _VOCABULARY_REGISTRY.get(model_name)
    if model is None:
        return Response(
            {'error': f"Unknown vocabulary '{model_name}'. Valid options: {sorted(_VOCABULARY_REGISTRY.keys())}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    has_sort_key = any(f.name == 'sort_key' for f in model._meta.get_fields())
    order_field = 'sort_key' if has_sort_key else 'title'
    items = list(model.objects.values('code', 'title', 'source_name', 'source_url').order_by(order_field))
    return Response(items)


# =============================================================================
# PatientRecord supplementary ViewSets
# =============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class PatientDocumentViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = PatientDocumentSerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    queryset = PatientDocument.objects.all()
    allowed_list_query_params = _OmopFilterMixin.allowed_list_query_params | frozenset({
        'doc_type', 'status',
    })

    def get_queryset(self):
        qs = super().get_queryset()
        doc_type = self.request.query_params.get('doc_type')
        if doc_type:
            qs = qs.filter(doc_type=doc_type)
        # PHR-S FM PH.1.4#04 — filter advance directives (and other docs) by
        # effective status, e.g. ?status=active to list only in-effect documents.
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs


class ImmunizationListViewSet(_OmopFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only list of immunization records (PHR-S FM PH.2.5).

    Immunizations are stored as DrugExposure rows tagged with
    route_source_value='VACCINE'. Filter by person: GET /v1/immunizations/?person_id=42
    """
    serializer_class = ImmunizationSerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    pagination_class = PatientRecordPagination
    queryset = DrugExposure.objects.filter(
        route_source_value='VACCINE',
    ).select_related('drug_concept')


class AllergyListViewSet(_OmopFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only list of allergy records (PHR-S FM PH.2.5).

    Allergies are stored as Observation rows tagged with
    qualifier_source_value='ALLERGY'. Filter by person: GET /v1/allergies/?person_id=42
    """
    serializer_class = AllergySerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    pagination_class = PatientRecordPagination
    queryset = Observation.objects.filter(
        qualifier_source_value='ALLERGY',
    ).select_related('observation_concept')


class PatientTrialEnrollmentViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    """CRUD for a patient's clinical trial enrollment status.

    Trial metadata (title, phase, eligibility, etc.) is NOT stored here.
    Use ``trial_id`` to retrieve that data from the EXACT trial-matcher API.

    Filter by person: GET /api/trial-enrollments/?person_id=42
    """
    serializer_class = PatientTrialEnrollmentSerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    queryset = PatientTrialEnrollment.objects.all()


class SurveyViewSet(viewsets.ModelViewSet):
    """Survey definitions — create/read/update/archive surveys.

    Surveys are global templates (no org FK). Reads are available to any
    authenticated token. Writes (create/update/archive) require service-token
    or staff — arbitrary write-scope patient tokens must not mutate the shared
    template library.

    Filter by disease: GET /api/surveys/?disease=Multiple+Myeloma
    Filter by status:  GET /api/surveys/?status=ACTIVE
    Surveys are archived via PATCH {status: ARCHIVED}; DELETE is not allowed.
    """
    serializer_class = SurveySerializer
    permission_classes = [ScopedTokenPermission]
    queryset = Survey.objects.all()

    def _require_admin_for_writes(self, request):
        """Block non-service callers from mutating shared survey templates.

        Allowed:
          - service-token (trusted backend string)
          - OAuth2 tokens from internal service apps (no org_profile)
          - Staff / superuser session users

        Blocked:
          - OAuth2 tokens from partner/EHR org apps (have an org_profile)
          - Session / Firebase / SAML non-staff users (patients)
        """
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return
        token = getattr(request, 'auth', None)
        if is_service_token(request):
            return
        if token is not None and not isinstance(token, TokenClaims):
            # OAuth2: allow only internal service apps (no org).
            # Partner org apps have an org_profile and must not touch shared templates.
            if get_request_org(request) is None:
                return
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Survey templates can only be modified by staff or service tokens.')
        # Session / Firebase / SAML: require staff.
        user = request.user
        if not (user and getattr(user, 'is_staff', False)):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Survey templates can only be modified by staff or service tokens.')

    def create(self, request, *args, **kwargs):
        self._require_admin_for_writes(request)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._require_admin_for_writes(request)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self._require_admin_for_writes(request)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Surveys cannot be deleted. Set status to ARCHIVED instead.'},
            status=405,
        )

    def get_queryset(self):
        qs = Survey.objects.all()
        disease = self.request.query_params.get('disease')
        if disease is not None:
            qs = qs.filter(disease=disease)
        status_filter = self.request.query_params.get('status')
        if status_filter is not None:
            qs = qs.filter(status=status_filter)
        return qs


class PatientSurveyResponseViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    """Patient survey responses — one record per (person, survey) pair.

    Filter by person: GET /api/survey-responses/?person_id=42
    Filter by survey: GET /api/survey-responses/?survey=3
    Supports partial update (PATCH) for incremental autosave of individual answers.
    PUT is disabled: values/values_dates are append-only dicts; use PATCH.
    """
    serializer_class = PatientSurveyResponseSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    queryset = PatientSurveyResponse.objects.select_related('survey').all()
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    allowed_list_query_params = _OmopFilterMixin.allowed_list_query_params | frozenset({
        'survey',
    })

    def get_queryset(self):
        qs = super().get_queryset()
        survey_id = self.request.query_params.get('survey')
        if survey_id:
            qs = qs.filter(survey_id=survey_id)
        # Guard: unfiltered list leaks all responses when no org context.
        # Require ?person_id= or staff/superuser for list actions.
        if self.action == 'list':
            org = get_request_org(self.request)
            person_id = self.request.query_params.get('person_id')
            user = self.request.user
            is_privileged = user and getattr(user, 'is_staff', False)
            if org is None and not person_id and not is_privileged:
                return qs.none()
        return qs

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Patient Consent management (PHR-S FM Phase 3)
# ---------------------------------------------------------------------------

class PatientConsentViewSet(viewsets.ModelViewSet):
    """Patient consent grants — auto-created for each consent type.

    Patients toggle ``consent_granted`` via PATCH; consents are never
    created or deleted by patients directly.

    GET  /api/v1/consents/         → list (auto-creates missing types)
    PATCH /api/v1/consents/{id}/   → toggle consent_granted
    """
    serializer_class = PatientConsentSerializer
    permission_classes = [ScopedTokenPermission, PatientSelfScopePermission]
    http_method_names = ['get', 'patch', 'head', 'options']

    CONSENT_TYPES = ['data_sharing', 'clinical_trial', 'research']

    def get_queryset(self):
        from patient_portal.models import PatientConsent
        from patient_portal.services import patient_person_for

        person = patient_person_for(self.request.user)
        if person is not None:
            return PatientConsent.objects.filter(patient_user__person=person)
        # Staff sees all; providers see only their org's patients' consents.
        if getattr(self.request.user, 'is_staff', False):
            return PatientConsent.objects.all()
        from omop_core.services.access import get_admin_orgs
        admin_org_ids = get_admin_orgs(self.request.user).values_list('id', flat=True)
        return PatientConsent.objects.filter(
            patient_user__person__patientrecord__organization_id__in=admin_org_ids
        )

    def list(self, request, *args, **kwargs):
        from patient_portal.models import PatientConsent, PatientUser
        from patient_portal.services import patient_person_for

        person = patient_person_for(request.user)
        if person is not None:
            try:
                patient_user = PatientUser.objects.get(person=person)
            except PatientUser.DoesNotExist:
                return Response([], status=status.HTTP_200_OK)
            # Auto-create missing consent types
            for consent_type in self.CONSENT_TYPES:
                PatientConsent.objects.get_or_create(
                    patient_user=patient_user,
                    consent_type=consent_type,
                    defaults={'consent_granted': False},
                )
        return super().list(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Patient messages — bidirectional messaging (PHR-S FM Phase 4b)
# ---------------------------------------------------------------------------

class MessagePagination(PageNumberPagination):
    page_size = 50


class PatientMessageViewSet(viewsets.ModelViewSet):
    """Bidirectional patient–provider messaging with threading.

    GET   /api/v1/messages/               → list (patients see only their own)
    POST  /api/v1/messages/               → create a new message or reply
    PATCH /api/v1/messages/{id}/           → edit own message text
    PATCH /api/v1/messages/{id}/mark-read/ → mark a message as read

    Query params:
      - parent=null       → top-level threads only
      - parent={id}       → replies to a specific message
      - is_read=false     → unread messages only
      - is_read=true      → read messages only
    """
    serializer_class = PatientMessageSerializer
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]
    pagination_class = MessagePagination
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        from patient_portal.models import PatientMessage
        from patient_portal.services import patient_person_for

        from django.db.models import Count
        qs = PatientMessage.objects.select_related(
            'sender', 'parent', 'patient_user',
        ).annotate(_reply_count=Count('replies')).order_by('-created_at')
        person = patient_person_for(self.request.user)
        if person is not None:
            # The account holder sees their own thread at every confidentiality level.
            qs = qs.filter(patient_user__person=person)
        else:
            # Staff/providers: restricted & very-restricted messages are visible only
            # to their sender — sensitive content is not broadly visible to
            # staff (PHR-S FM PH.6.3#08). Only service tokens are unrestricted.
            from django.db.models import Q
            if not is_service_token(self.request):
                qs = qs.filter(
                    Q(confidentiality=PatientMessage.CONFIDENTIALITY_NORMAL)
                    | Q(sender=self.request.user)
                )

        # Optional filters
        parent = self.request.query_params.get('parent')
        if parent == 'null':
            qs = qs.filter(parent__isnull=True)  # Top-level threads only
        elif parent:
            try:
                qs = qs.filter(parent_id=int(parent))
            except (ValueError, TypeError):
                qs = qs.none()

        is_read = self.request.query_params.get('is_read')
        if is_read is not None:
            qs = qs.filter(read_at__isnull=(is_read.lower() == 'false'))

        return qs

    def perform_create(self, serializer):
        from patient_portal.models import PatientUser
        from patient_portal.services import patient_person_for
        from rest_framework.exceptions import PermissionDenied, ValidationError

        user = self.request.user
        person = patient_person_for(user)

        # Auto-set sender and sender_is_patient
        kwargs = {
            'sender': user,
            'sender_is_patient': person is not None,
        }

        if person is not None:
            # Patient: force patient_user to their own record
            try:
                pu = PatientUser.objects.get(identity=user, person=person)
            except PatientUser.DoesNotExist:
                raise PermissionDenied('No patient record linked to this account.')
            kwargs['patient_user'] = pu
        else:
            # Staff: inherit patient_user from parent on reply, require on new thread
            parent = serializer.validated_data.get('parent')
            if parent:
                kwargs['patient_user'] = parent.patient_user
            elif not serializer.validated_data.get('patient_user'):
                raise ValidationError({'patient_user': 'Staff must specify patient_user when starting a new thread.'})

            # Verify staff has access to the target patient's org
            resolved_pu = kwargs.get('patient_user') or serializer.validated_data.get('patient_user')
            if resolved_pu and not getattr(user, 'is_staff', False):
                from omop_core.services.access import get_admin_orgs
                pr = PatientRecord.objects.filter(person=resolved_pu.person).first()
                if pr and pr.organization_id:
                    admin_org_ids = set(get_admin_orgs(user).values_list('id', flat=True))
                    if pr.organization_id not in admin_org_ids:
                        raise PermissionDenied('You do not have access to this patient.')

        # Validate parent belongs to the same patient thread
        parent = serializer.validated_data.get('parent')
        resolved_pu = kwargs.get('patient_user') or serializer.validated_data.get('patient_user')
        if parent and resolved_pu:
            if parent.patient_user_id != resolved_pu.pk:
                raise PermissionDenied('Cannot reply to another patient\'s message.')

        serializer.save(**kwargs)

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        # Only the sender can edit message text; prevent patients from altering
        # provider messages.
        obj = serializer.instance
        if obj.sender_id != self.request.user.pk:
            raise PermissionDenied('You can only edit your own messages.')
        # Strip immutable fields on update
        for field in ('patient_user', 'parent', 'subject'):
            serializer.validated_data.pop(field, None)
        serializer.save()

    @action(detail=True, methods=['patch'], url_path='mark-read')
    def mark_read(self, request, pk=None):
        msg = self.get_object()
        if msg.read_at is None:
            msg.read_at = timezone.now()
            msg.is_read = True  # Keep legacy field in sync
            msg.save(update_fields=['read_at', 'is_read'])
        serializer = self.get_serializer(msg)
        return Response(serializer.data)


class InterchangeAgreementViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only list/detail of documented data-interchange agreements (TI.5.4#01).

    A formal, described agreement artifact governing electronic exchange with
    external partners. Exposed under /api/v1/interchange-agreements/.
    """
    permission_classes = [IsAuthenticated]
    pagination_class = PatientRecordPagination

    def get_queryset(self):
        from omop_core.models import InterchangeAgreement
        return InterchangeAgreement.objects.all().select_related('partner_organization')

    def get_serializer_class(self):
        from .serializers import InterchangeAgreementSerializer
        return InterchangeAgreementSerializer


# ---------------------------------------------------------------------------
# Vocabulary Release — versioned release manifest API
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([VocabReadPermission])
def vocab_release_list(request):
    """Paginated list of published vocabulary releases (newest first)."""
    from omop_core.models import VocabularyRelease

    qs = VocabularyRelease.objects.filter(status='published').order_by('-published_at')
    paginator = ConceptPagination()
    page = paginator.paginate_queryset(qs, request)
    results = [_serialize_vocab_release(r) for r in page]
    return paginator.get_paginated_response(results)


@api_view(['GET'])
@permission_classes([VocabReadPermission])
def vocab_release_detail(request, release_id):
    """Full manifest for a specific published vocabulary release, including checksums."""
    from omop_core.models import VocabularyRelease

    try:
        release = VocabularyRelease.objects.get(pk=release_id, status='published')
    except VocabularyRelease.DoesNotExist:
        return Response({'detail': 'Release not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(_serialize_vocab_release(release, include_checksums=True))


@api_view(['GET'])
@permission_classes([VocabReadPermission])
def vocab_release_latest(request):
    """Latest published vocabulary release. Supports If-None-Match → 304."""
    from omop_core.services.vocab_release import get_latest_release

    release = get_latest_release()
    if release is None:
        return Response({'detail': 'No published releases.'}, status=status.HTTP_404_NOT_FOUND)

    resp = Response(_serialize_vocab_release(release, include_checksums=True))
    return _set_release_etag(request, resp)


def _serialize_vocab_release(release, include_checksums=False):
    data = {
        'id': release.pk,
        'schema_version': release.schema_version,
        'scope': release.scope,
        'build_timestamp': release.build_timestamp.isoformat() if release.build_timestamp else None,
        'athena_version': release.athena_version,
        'vocab_versions': release.vocab_versions,
        'row_counts': release.row_counts,
        'status': release.status,
        'published_at': release.published_at.isoformat() if release.published_at else None,
        'notes': release.notes,
    }
    if include_checksums:
        data['checksums'] = release.checksums
    return data


# ---------------------------------------------------------------------------
# Vocabulary Snapshot — streaming NDJSON bulk download
# ---------------------------------------------------------------------------

class VocabSnapshotView(APIView):
    """Stream all rows from a vocabulary table as newline-delimited JSON.

    Uses raw SQL ``row_to_json()`` to avoid ORM overhead on large tables.
    The table name is validated against a whitelist before interpolation.
    """
    permission_classes = [VocabReadPermission]

    # SECURITY: db_table values are hardcoded; never interpolate user input.
    ALLOWED_TABLES = {
        'concept': 'concept',
        'concept_ancestor': 'concept_ancestor',
        'concept_class': 'concept_class',
        'concept_relationship': 'concept_relationship',
        'concept_synonym': 'concept_synonym',
        'domain': 'domain',
        'drug_strength': 'drug_strength',
        'relationship': 'relationship',
        'source_to_concept_map': 'source_to_concept_map',
        'vocabulary': 'vocabulary',
    }

    def get(self, request, table, release_id=None):
        from django.http import HttpResponseNotModified, StreamingHttpResponse
        from omop_core.models import VocabularyRelease
        from omop_core.services.vocab_release import get_latest_release, get_release_etag

        # 1. Validate table name
        if table not in self.ALLOWED_TABLES:
            return Response(
                {'detail': f'Unknown table. Valid tables: {", ".join(sorted(self.ALLOWED_TABLES))}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 2. Resolve release. Resolve `latest` ONCE and reuse it for both the
        # release_id=None branch and the non-latest guard below — two independent
        # get_latest_release() reads could straddle a publish (or a published_at
        # tie) and make the /latest/ URL 409 against itself.
        latest = get_latest_release()
        if release_id is None:
            release = latest
        else:
            release = VocabularyRelease.objects.filter(
                pk=release_id, status='published',
            ).first()
        if release is None:
            return Response({'detail': 'Release not found.'}, status=status.HTTP_404_NOT_FOUND)

        # 2b. Refuse a non-latest published release. The vocab tables are reloaded
        # wholesale per release (current-only), and the stream below filters only by
        # `source`, not release_id — so serving an OLDER release's URL would stream
        # the CURRENT rows under a stale label. Only the latest published release is
        # truthful; the manifest API (vocab_release_detail) still serves historical
        # metadata. See issue #371 / exact#286.
        if latest is not None and release.pk != latest.pk:
            return Response(
                {'detail': (
                    f'Snapshot for release {release.pk} is unavailable: vocabulary '
                    f'tables are current-only, so only the latest published release '
                    f'({latest.pk}) can be streamed. Use '
                    f'/api/v1/vocab-releases/latest/snapshot/{table}/.'
                )},
                status=status.HTTP_409_CONFLICT,
            )

        # 3. ETag / conditional request
        etag = get_release_etag(release)
        if_none_match = request.META.get('HTTP_IF_NONE_MATCH', '')
        if _etag_matches(if_none_match, etag):
            resp = HttpResponseNotModified()
            if etag:
                resp['ETag'] = etag
            resp['X-Vocab-Release-Id'] = str(release.pk)
            return resp

        # 4. Build WHERE clause (source filter for concept table only)
        db_table = self.ALLOWED_TABLES[table]
        where = ''
        params = []
        source_param = None
        if table == 'concept':
            source_param = request.query_params.get('source')
            if source_param == 'HealthKey':
                where = 'WHERE source = %s'
                params = ['HealthKey']
            elif source_param == 'external':
                where = 'WHERE source IS NULL'

        # Vary ETag by source filter so different queries don't share ETags
        if source_param and etag:
            etag = etag.rstrip('"') + f'-{source_param}"'

        # 5. Stream NDJSON
        sql = f'SELECT row_to_json(t) FROM {db_table} t {where}'
        response = StreamingHttpResponse(
            self._stream_ndjson(sql, params),
            content_type='application/x-ndjson',
        )
        response['Content-Disposition'] = (
            f'attachment; filename="{table}_{release.pk}.ndjson"'
        )
        # Name the release these rows reflect, so consumers capture it robustly
        # (not by parsing the ETag/filename). Always the latest published release —
        # non-latest is refused above. See issue #371 / cancerbot#4646.
        response['X-Vocab-Release-Id'] = str(release.pk)
        if etag:
            response['ETag'] = etag
            response['Cache-Control'] = 'private, max-age=86400'
        return response

    @staticmethod
    def _stream_ndjson(sql, params=None):
        import json as _json
        from django.db import connection, transaction
        count = 0
        # A server-side (named) cursor issues DECLARE CURSOR, which Postgres only
        # allows inside a transaction block. The streaming generator runs after the
        # view returns, in Django's default autocommit — so wrap it in an explicit
        # transaction spanning the whole stream, or the first fetch raises
        # NoActiveSqlTransaction.
        with transaction.atomic():
            with connection.connection.cursor(name='vocab_snapshot') as cursor:
                cursor.itersize = 1000
                cursor.execute(sql, params or [])
                for (row_json,) in cursor:
                    if isinstance(row_json, dict):
                        yield _json.dumps(row_json) + '\n'
                    else:
                        yield str(row_json) + '\n'
                    count += 1
        yield _json.dumps({'__done': True, 'rows': count}) + '\n'


# =============================================================================
# Field Concept Mapping (staff-only concept assignment interface)
# =============================================================================

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def field_mapping_list(request):
    """GET: list all field descriptors.  POST: create a new mapping."""
    if not getattr(request.user, 'is_staff', False):
        return Response({'detail': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        from omop_core.services.field_descriptor import get_all_field_descriptors
        descriptors = get_all_field_descriptors()
        # Optional filters.
        category = request.query_params.get('category')
        if category:
            descriptors = [d for d in descriptors if d['category'] == category]
        search = request.query_params.get('search')
        if search:
            q = search.lower()
            descriptors = [d for d in descriptors if q in d['field_name'].lower()]
        return Response(descriptors)

    # POST — create a mapping.
    from .serializers import FieldConceptMappingSerializer
    serializer = FieldConceptMappingSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def field_mapping_detail(request, pk):
    """GET/PATCH/DELETE a single FieldConceptMapping."""
    if not getattr(request.user, 'is_staff', False):
        return Response({'detail': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)

    from omop_core.models import FieldConceptMapping
    try:
        mapping = FieldConceptMapping.objects.get(pk=pk)
    except FieldConceptMapping.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        from .serializers import FieldConceptMappingSerializer
        return Response(FieldConceptMappingSerializer(mapping).data)

    if request.method == 'DELETE':
        mapping.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    # PATCH
    from .serializers import FieldConceptMappingSerializer
    serializer = FieldConceptMappingSerializer(
        mapping, data=request.data, partial=True, context={'request': request},
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)
