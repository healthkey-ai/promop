from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from patient_portal.models import Identity
from django.contrib.auth import logout, login, authenticate
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from omop_core.models import (
    Person, PatientRecord, Concept, ConceptClass, Domain, ProvenanceRecord, Vocabulary,
    ConditionOccurrence, DrugExposure, Measurement, MeasurementOwnership,
    Observation, ProcedureOccurrence, VisitOccurrence, VisitDetail, Location, Death,
    PatientDocument, PatientTrialEnrollment, PatientGroupMembership, Survey, PatientSurveyResponse,
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
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.patient_cleanup import delete_omop_clinical_rows
from omop_core.services.lot_inference_service import infer_lot_for_person
from omop_core.services.omop_write_service import sync_to_omop
from omop_core.services.episode_service import upsert_therapy_line_episode
from omop_core.services.mappings import get_gender_concept, LAB_FIELD_TO_LOINC
from omop_core.services.pk import next_pk
from omop_core.services.rxnav_service import resolve_drug as _rxnav_resolve_drug
from omop_core.services.concept_cache import concept_by_id as _cc_by_id, concept_by_loinc as _cc_by_loinc, concept_by_name_ilike as _cc_by_name, concept_by_vocab as _cc_by_vocab
from omop_core.services.access import get_visible_orgs, build_trusting_map
from datetime import datetime, timedelta
from django.utils.timezone import localdate
import csv
import hashlib
import json
import logging
import re
from io import StringIO
from .permissions import ScopedTokenPermission, get_request_org, is_service_token
from .providers.base import TokenClaims
from .serializers import (
    UserSerializer, PatientRecordSerializer, PatientListSerializer, ProvenanceRecordSerializer,
    ConditionOccurrenceSerializer, DrugExposureSerializer, MeasurementSerializer,
    ObservationSerializer, ProcedureOccurrenceSerializer,
    EpisodeSerializer, EpisodeEventSerializer,
    PatientDocumentSerializer, PatientTrialEnrollmentSerializer,
    SurveySerializer, PatientSurveyResponseSerializer,
)
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


class PatientRecordPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


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
    base = request.build_absolute_uri('/').rstrip('/')
    oidc_issuer = getattr(settings, 'OAUTH2_PROVIDER', {}).get('OIDC_ISS_ENDPOINT', '') or base
    return Response({
        'issuer': oidc_issuer,
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
    
    def list(self, request):
        """Just return the logged-in user info - they don't need to be a patient"""
        if not request.user.is_authenticated:
            return Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
        
        user_serializer = UserSerializer(request.user)
        return Response({
            'user': user_serializer.data
        })

def _extract_provenance(request):
    """Return (source, source_user_id, modification_reason) from headers or POST body."""
    source = (
        request.data.get('source')
        or request.META.get('HTTP_X_PROVENANCE_SOURCE')
    )
    source_user_id = (
        request.data.get('source_user_id')
        or request.META.get('HTTP_X_PROVENANCE_USER_ID', '')
    )
    modification_reason = request.data.get('modification_reason')
    return source, source_user_id, modification_reason


def _record_provenance(record, source, source_user_id, target_patient_id=None, modification_reason=None, organization=None):
    """Create a ProvenanceRecord pointing at any model instance."""
    ProvenanceRecord.objects.create(
        source=source,
        source_user_id=source_user_id or '',
        target_patient_id=target_patient_id,
        modification_reason=modification_reason,
        organization=organization,
        content_type=ContentType.objects.get_for_model(record),
        object_id=record.pk,
    )


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
    permission_classes = [ScopedTokenPermission]
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
        elif not (self.request.user and (
            getattr(self.request.user, 'is_superuser', False) or
            getattr(self.request.user, 'is_staff', False)
        )):
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

            # Org-admin grants: see all patients belonging to those orgs
            admin_org_ids = list(
                active_grants.filter(role='org_admin').values_list('org_id', flat=True)
            )

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

    def create(self, request):
        """Create a new patient record, creating a Person if needed"""
        data = request.data

        # Resolve or create Person
        person_id = data.get('person_id') or data.get('person')
        if person_id:
            try:
                person = Person.objects.get(person_id=int(person_id))
            except Person.DoesNotExist:
                person = Person.objects.create(
                    person_id=int(person_id),
                    year_of_birth=datetime.now().year - 50,
                    gender_source_value='unknown',
                    race_source_value='unknown',
                    ethnicity_source_value='unknown',
                )
        else:
            last_person = Person.objects.order_by('-person_id').first()
            new_person_id = last_person.person_id + 1 if last_person else 1000
            person = Person.objects.create(
                person_id=new_person_id,
                year_of_birth=datetime.now().year - 50,
                gender_source_value='unknown',
                race_source_value='unknown',
                ethnicity_source_value='unknown',
            )

        serializer = PatientRecordSerializer(data=data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save(person=person)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        """Get detailed patient info for a specific person"""
        try:
            person = Person.objects.get(person_id=pk)
            patient_info = PatientRecord.objects.get(person=person)
        except Person.DoesNotExist:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientRecord.DoesNotExist:
            return Response({'error': 'Patient information not found'}, status=status.HTTP_404_NOT_FOUND)

        # AUTH-04: enforce per-patient row-level access
        org = get_request_org(request)
        if org is not None:
            if patient_info.organization != org:
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        elif not request.user.is_superuser and not getattr(request.user, 'is_staff', False):
            from omop_core.authorization import can_access_patient
            if not can_access_patient(request.user, person.person_id):
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)

        # Get the Identity associated with this person (not the logged-in user)
        from patient_portal.models import PatientUser
        try:
            patient_user = PatientUser.objects.get(person=person)
            user_serializer = UserSerializer(patient_user.identity)
            user_data = user_serializer.data
        except PatientUser.DoesNotExist:
            user_data = None

        patient_serializer = PatientRecordSerializer(patient_info)

        return Response({
            'patient_info': patient_serializer.data,  # legacy wire format — preserved for frontend/federation host compatibility
            'user': user_data
        })

    def partial_update(self, request, pk=None):
        """PATCH /api/patient-info/{person_id}/ — update PatientRecord and write through to OMOP."""
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
        elif not request.user.is_superuser and not getattr(request.user, 'is_staff', False):
            from omop_core.authorization import can_access_patient, can_write_patient
            if not can_access_patient(request.user, person.person_id):
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
            if not can_write_patient(request.user, person.person_id):
                return Response(
                    {'error': 'Analysts have read-only access. Contact a doctor or org admin to update patient data.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        prov_source, prov_user_id, prov_reason = _extract_provenance(request)
        if prov_source == 'ADMIN_CORRECTION' and not prov_reason:
            return Response(
                {'error': 'modification_reason is required when source is ADMIN_CORRECTION'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Capture previous values for fields being changed (exclude provenance meta-fields).
        # Use {field}_id for FK fields so we get a serializable PK, not a model object.
        _prov_meta = {'source', 'source_user_id', 'modification_reason'}
        _read_only = set(PatientRecordSerializer.Meta.read_only_fields)
        def _prev_val(obj, field):
            fk_id = f'{field}_id'
            if hasattr(obj, fk_id):
                return getattr(obj, fk_id, None)
            return getattr(obj, field, None)
        previous_values = {
            field: _prev_val(patient_info, field)
            for field in request.data
            if field not in _prov_meta and field not in _read_only and hasattr(patient_info, field)
        }

        serializer = PatientRecordSerializer(patient_info, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        changed_fields = {f for f in request.data if f not in _prov_meta}
        try:
            with transaction.atomic():
                serializer.save()
                if prov_source:
                    _record_provenance(patient_info, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
                sync_to_omop(patient_info, changed_fields, changed_data=dict(request.data))
                if prov_source:
                    for field in changed_fields:
                        if field in LAB_FIELD_TO_LOINC:
                            loinc_code = LAB_FIELD_TO_LOINC[field][0]
                            m = Measurement.objects.filter(
                                person=patient_info.person,
                                measurement_source_value=loinc_code,
                            ).order_by('-measurement_id').first()
                            if m:
                                _record_provenance(m, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
        except Exception as _sync_exc:
            logger.error(
                'omop_write_through_failed patient=%s error=%s',
                patient_info.pk, type(_sync_exc).__name__,
            )
            return Response(
                {'error': 'Failed to persist changes to OMOP. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({**serializer.data, 'previous_values': previous_values})

    @action(detail=True, methods=['get'], permission_classes=[ScopedTokenPermission])
    def provenance(self, request, pk=None):
        """GET /api/patient-info/{person_id}/provenance/ — full provenance history for a patient."""
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
        elif not request.user.is_superuser and not getattr(request.user, 'is_staff', False):
            from omop_core.authorization import can_access_patient
            if not can_access_patient(request.user, person.person_id):
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)

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

    @action(detail=False, methods=['get', 'patch'], permission_classes=[ScopedTokenPermission])
    def me(self, request):
        """GET/PATCH /api/patient-info/me/ — current user's own PatientRecord."""
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
                or getattr(request.user, 'is_superuser', False)
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

        # PATCH
        patient_name = request.data.pop('patient_name', None) if hasattr(request.data, 'pop') else request.data.get('patient_name')
        patch_data = {k: v for k, v in request.data.items() if k != 'patient_name'}

        if patient_name is not None:
            parts = str(patient_name).strip().split(None, 1)
            person.given_name = parts[0] if parts else ''
            person.family_name = parts[1] if len(parts) > 1 else ''
            person.save(update_fields=['given_name', 'family_name'])

        serializer = PatientRecordSerializer(patient_info, data=patch_data, partial=True)
        serializer.is_valid(raise_exception=True)

        changed_fields = set(patch_data.keys())
        try:
            with transaction.atomic():
                serializer.save()
                sync_to_omop(patient_info, changed_fields, changed_data=dict(patch_data))
        except Exception as _sync_exc:
            logger.error(
                'omop_write_through_failed patient=%s error=%s',
                patient_info.pk, type(_sync_exc).__name__,
            )
            return Response(
                {'error': 'Failed to persist changes to OMOP. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(serializer.data)

    @action(detail=False, methods=['post'], permission_classes=[ScopedTokenPermission])
    def upload_csv(self, request):
        """Upload patients from CSV file"""
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
            
            for row_num, row in enumerate(reader, start=2):
                try:
                    person_id = int(row.get('person_id', 0))
                    if person_id == 0:
                        last_person = Person.objects.all().order_by('-person_id').first()
                        person_id = last_person.person_id + 1 if last_person else 1000
                    
                    # Get gender concept
                    gender_concept = get_gender_concept(row.get('gender', ''))
                    gender_source = row.get('gender', 'unknown')
                    
                    person, created = Person.objects.get_or_create(
                        person_id=person_id,
                        defaults={
                            'year_of_birth': int(row.get('year_of_birth', datetime.now().year - 50)),
                            'gender_concept': gender_concept,
                            'gender_source_value': gender_source,
                            'race_concept': None,
                            'race_source_value': 'unknown',
                            'ethnicity_concept': None,
                            'ethnicity_source_value': 'unknown',
                            'person_source_value': f"CSV-{person_id}",
                        }
                    )
                    
                    date_of_birth = None
                    if row.get('date_of_birth'):
                        try:
                            date_of_birth = datetime.strptime(row['date_of_birth'], '%Y-%m-%d').date()
                        except ValueError:
                            try:
                                date_of_birth = datetime.strptime(row['date_of_birth'], '%m/%d/%Y').date()
                            except ValueError:
                                pass
                    
                    patient_info, pi_created = PatientRecord.objects.update_or_create(
                        person=person,
                        defaults={
                            'date_of_birth': date_of_birth,
                            'disease': row.get('disease', ''),
                        }
                    )
                    
                    if pi_created:
                        created_count += 1
                        
                except Exception as e:
                    errors.append(f"Row {row_num}: {str(e)}")
            
            return Response({
                'success': True,
                'created_count': created_count,
                'errors': errors
            })
            
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'], permission_classes=[ScopedTokenPermission])
    def upload_fhir(self, request):
        """Upload patients from FHIR JSON file"""
        if 'file' not in request.FILES:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        file = request.FILES['file']
        if not file.name.endswith('.json'):
            return Response({'error': 'File must be a JSON file'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            fhir_data = json.load(file)

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
            _concept_generic_lab   = _cc_by_id(3000963)  # Generic lab

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
                    standard_concept='S',
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
                    if not person_is_new and not request.user.is_superuser and not getattr(request.user, 'is_staff', False):
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
                        
                        # Check for tumor size
                        if 'tumor size' in obs_text or 'size tumor' in obs_text:
                            if observation.get('valueQuantity'):
                                tumor_size = observation['valueQuantity'].get('value')
                        
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
                        
                        # Check if this is a regimen (parent) or individual drug (partOf)
                        if not medication.get('partOf'):
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
                                    'outcome': therapy_outcome,
                                    'hemonc_concept_id': hemonc_concept_id,
                                }
                            else:
                                therapy_lines[therapy_line]['regimen'] = regimen_name
                                if start_date:
                                    therapy_lines[therapy_line]['start_date'] = start_date
                                if end_date:
                                    therapy_lines[therapy_line]['end_date'] = end_date
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
                            except:
                                pass
                        if therapy_lines[1].get('end_date'):
                            try:
                                first_line_end_date = datetime.strptime(therapy_lines[1]['end_date'][:10], '%Y-%m-%d').date()
                            except:
                                pass
                        first_line_outcome = therapy_lines[1]['outcome']
                    
                    if 2 in therapy_lines:
                        second_line_therapy = therapy_lines[2]['regimen']
                        if therapy_lines[2].get('start_date'):
                            try:
                                second_line_start_date = datetime.strptime(therapy_lines[2]['start_date'][:10], '%Y-%m-%d').date()
                                second_line_date = second_line_start_date  # Keep for backwards compatibility
                            except:
                                pass
                        if therapy_lines[2].get('end_date'):
                            try:
                                second_line_end_date = datetime.strptime(therapy_lines[2]['end_date'][:10], '%Y-%m-%d').date()
                            except:
                                pass
                        second_line_outcome = therapy_lines[2]['outcome']
                    
                    # Map line 3 and 4 to "later" field (prioritize most recent)
                    if 4 in therapy_lines:
                        later_therapy = therapy_lines[4]['regimen']
                        if therapy_lines[4].get('start_date'):
                            try:
                                later_start_date = datetime.strptime(therapy_lines[4]['start_date'][:10], '%Y-%m-%d').date()
                                later_date = later_start_date  # Keep for backwards compatibility
                            except:
                                pass
                        if therapy_lines[4].get('end_date'):
                            try:
                                later_end_date = datetime.strptime(therapy_lines[4]['end_date'][:10], '%Y-%m-%d').date()
                            except:
                                pass
                        later_outcome = therapy_lines[4]['outcome']
                    elif 3 in therapy_lines:
                        later_therapy = therapy_lines[3]['regimen']
                        if therapy_lines[3].get('start_date'):
                            try:
                                later_start_date = datetime.strptime(therapy_lines[3]['start_date'][:10], '%Y-%m-%d').date()
                                later_date = later_start_date  # Keep for backwards compatibility
                            except:
                                pass
                        if therapy_lines[3].get('end_date'):
                            try:
                                later_end_date = datetime.strptime(therapy_lines[3]['end_date'][:10], '%Y-%m-%d').date()
                            except:
                                pass
                        later_outcome = therapy_lines[3]['outcome']
                    
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
                        return len(name) <= 8 and name.upper() == name and any(ch.isalpha() for ch in name)

                    def _get_or_create_local_regimen_concept(name):
                        name = (name or '').strip()
                        if not name:
                            return None
                        code = 'FHIR-' + ''.join(ch.upper() if ch.isalnum() else '-' for ch in name)
                        while '--' in code:
                            code = code.replace('--', '-')
                        code = code.strip('-')[:50]

                        concept = Concept.objects.filter(
                            vocabulary_id='HemOnc',
                            concept_code=code,
                        ).first()
                        if concept:
                            return concept

                        domain, _ = Domain.objects.get_or_create(
                            domain_id='Drug',
                            defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
                        )
                        vocabulary, _ = Vocabulary.objects.get_or_create(
                            vocabulary_id='HemOnc',
                            defaults={
                                'vocabulary_name': 'HemOnc',
                                'vocabulary_reference': 'FHIR import',
                                'vocabulary_version': 'local',
                                'vocabulary_concept_id': 0,
                            },
                        )
                        concept_class, _ = ConceptClass.objects.get_or_create(
                            concept_class_id='Regimen',
                            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
                        )
                        return Concept.objects.create(
                            concept_id=next_pk(Concept, 'concept_id'),
                            concept_name=name[:255],
                            domain=domain,
                            vocabulary=vocabulary,
                            concept_class=concept_class,
                            standard_concept='S',
                            concept_code=code,
                            valid_start_date=datetime(1970, 1, 1).date(),
                            valid_end_date=datetime(2099, 12, 31).date(),
                        )

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

                    def _get_or_create_local_concept(domain_id, vocabulary_id, concept_class_id, concept_code, concept_name):
                        concept_code = (concept_code or concept_name or 'unknown')[:50]
                        concept_name = (concept_name or f'{vocabulary_id} {concept_code}')[:255]

                        concept = Concept.objects.filter(
                            vocabulary_id=vocabulary_id,
                            concept_code=concept_code,
                        ).first()
                        if concept:
                            return concept

                        domain, _ = Domain.objects.get_or_create(
                            domain_id=domain_id,
                            defaults={'domain_name': domain_id, 'domain_concept_id': 0},
                        )
                        vocabulary, _ = Vocabulary.objects.get_or_create(
                            vocabulary_id=vocabulary_id,
                            defaults={
                                'vocabulary_name': vocabulary_id,
                                'vocabulary_reference': 'FHIR import',
                                'vocabulary_version': 'local',
                                'vocabulary_concept_id': 0,
                            },
                        )
                        concept_class, _ = ConceptClass.objects.get_or_create(
                            concept_class_id=concept_class_id,
                            defaults={'concept_class_name': concept_class_id, 'concept_class_concept_id': 0},
                        )
                        return Concept.objects.create(
                            concept_id=next_pk(Concept, 'concept_id'),
                            concept_name=concept_name,
                            domain=domain,
                            vocabulary=vocabulary,
                            concept_class=concept_class,
                            standard_concept='S',
                            concept_code=concept_code,
                            valid_start_date=datetime(1970, 1, 1).date(),
                            valid_end_date=datetime(2099, 12, 31).date(),
                        )

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
                                return _rxnav_resolve_drug(display)
                            except Exception as rxnav_exc:
                                logger.warning(
                                    '{"event": "rxnav_resolve_failed", "drug": "%s", "error": "%s"}',
                                    display, rxnav_exc,
                                )
                        return _get_or_create_local_concept(
                            'Drug',
                            vocabulary_id,
                            'Clinical Drug' if vocabulary_id == 'RxNorm' else 'Vaccine',
                            code or display,
                            display,
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
                                # Prefer HemOnc concept_id already embedded in the FHIR bundle;
                                # only fall back to ILIKE + RxNav when it is absent.
                                _hemonc_cid = lot_data.get('hemonc_concept_id')
                                if _hemonc_cid:
                                    regimen_concept = _cc_by_id(_hemonc_cid)
                                else:
                                    if _looks_like_regimen_name(regimen_name):
                                        regimen_concept = _get_or_create_local_regimen_concept(regimen_name)
                                    else:
                                        regimen_concept = Concept.objects.filter(
                                            concept_name__icontains=regimen_name,
                                            domain__domain_id='Drug',
                                        ).first() if regimen_name else None
                                    # RxNav fallback only when no HemOnc concept_id, no local
                                    # match, and the source looks like a plain drug name.
                                    if regimen_concept is None and regimen_name:
                                        try:
                                            regimen_concept = _rxnav_resolve_drug(regimen_name)
                                        except Exception as rxnav_exc:
                                            logger.warning(
                                                '{"event": "rxnav_resolve_failed", "drug": "%s", "error": "%s"}',
                                                regimen_name, rxnav_exc,
                                            )
                                # Final fallback to any Drug domain concept
                                if regimen_concept is None:
                                    regimen_concept = Concept.objects.filter(
                                        domain__domain_id='Drug'
                                    ).first()
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
                                    regimen_source_concept=regimen_concept if _hemonc_cid else None,
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

                    def _write_drug_exposure(codeable, start_str, end_str=None, sig=None):
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
                        )

                    logger.info(
                        "TIMING patient=%s phase=supplemental_drugs elapsed=%.1fs count=%d",
                        _timing_hash, _time.monotonic() - _pt_start, len(_pt_drug_exposure_ids),
                    )

                    # --- Write DiagnosticReport rows into OMOP Observation ---
                    _existing_report_keys = {
                        (o.observation_source_value, o.observation_date, o.value_as_string)
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
                        _report_concept = _cc_by_loinc(_code) if _vocab == 'LOINC' and _code else None
                        if _report_concept is None:
                            _report_concept = _get_or_create_local_concept(
                                'Observation',
                                _vocab,
                                'Clinical Observation',
                                _code or _display,
                                _display or 'FHIR DiagnosticReport',
                            )
                        _value = (_report.get('conclusion') or _display or 'Diagnostic report')[:60]
                        _source = (_code or _display or 'DiagnosticReport')[:50]
                        _report_key = (_source, _report_date, _value)
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

                    # --- Write ProcedureOccurrence records ---
                    def _get_or_create_fhir_procedure_concept(vocabulary_id, concept_code, concept_name):
                        if not concept_code:
                            return None
                        concept = Concept.objects.filter(
                            vocabulary_id=vocabulary_id,
                            concept_code=concept_code,
                        ).first()
                        if concept:
                            return concept

                        domain, _ = Domain.objects.get_or_create(
                            domain_id='Procedure',
                            defaults={'domain_name': 'Procedure', 'domain_concept_id': 10},
                        )
                        vocabulary, _ = Vocabulary.objects.get_or_create(
                            vocabulary_id=vocabulary_id,
                            defaults={
                                'vocabulary_name': vocabulary_id,
                                'vocabulary_reference': 'FHIR import',
                                'vocabulary_version': 'local',
                                'vocabulary_concept_id': 0,
                            },
                        )
                        concept_class, _ = ConceptClass.objects.get_or_create(
                            concept_class_id='Procedure',
                            defaults={'concept_class_name': 'Procedure', 'concept_class_concept_id': 0},
                        )
                        return Concept.objects.create(
                            concept_id=next_pk(Concept, 'concept_id'),
                            concept_name=(concept_name or f'{vocabulary_id} {concept_code}')[:255],
                            domain=domain,
                            vocabulary=vocabulary,
                            concept_class=concept_class,
                            standard_concept='S',
                            concept_code=concept_code[:50],
                            valid_start_date=datetime(1970, 1, 1).date(),
                            valid_end_date=datetime(2099, 12, 31).date(),
                        )

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
                            if _proc_concept is None:
                                _proc_concept = _get_or_create_fhir_procedure_concept(
                                    'SNOMED',
                                    _proc_code_value,
                                    _proc_display,
                                )
                        if _proc_concept is None and _proc_display:
                            _proc_concept = Concept.objects.filter(
                                concept_name__icontains=_proc_display,
                                domain__domain_id='Procedure',
                            ).first()
                        if _proc_concept is None:
                            _proc_concept = Concept.objects.filter(domain__domain_id='Procedure').first()
                        if _proc_concept is None:
                            logger.warning(
                                '{"event": "procedure_write_skipped", "reason": "no_procedure_concept", "patient": "%s", "procedure": "%s"}',
                                _timing_hash, _proc_display,
                            )
                            continue

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
                    if sct_date_str:
                        try:
                            parsed_sct_date = datetime.strptime(sct_date_str, '%Y-%m-%d').date()
                            if parsed_sct_date <= localdate():
                                _patch['sct_date'] = parsed_sct_date
                        except ValueError:
                            _id_hash = hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12]
                            logger.warning(
                                "Ignoring invalid mm-sct-date for patient (id_hash=%s)",
                                _id_hash,
                            )
                    if sct_history_str:
                        _patch['stem_cell_transplant_history'] = [
                            t.strip() for t in sct_history_str.split(',')
                            if t.strip() and t.strip() in _allowed_sct_titles
                        ]
                    if sct_eligibility_str:
                        _patch['sct_eligibility'] = [
                            t.strip() for t in sct_eligibility_str.split(',')
                            if t.strip() and t.strip() in _allowed_elig_titles
                        ]
                    if tumor_size:
                        _patch['tumor_size'] = tumor_size
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

                    # Apply patch to PatientRecord (suppress signal-triggering save)
                    for _field, _val in _patch.items():
                        setattr(patient_info, _field, _val)
                    patient_info.save()
                    if prov_source:
                        _record_provenance(patient_info, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))

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
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

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
            _is_privileged = request.user and (
                getattr(request.user, 'is_superuser', False) or
                getattr(request.user, 'is_staff', False)
            )
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
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

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

        if user is not None:
            login(request, user)
            user_serializer = UserSerializer(user)
            return Response({
                'message': 'Login successful',
                'user': user_serializer.data
            }, status=status.HTTP_200_OK)
        else:
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

    # For staff/superusers: also surface patients not assigned to any org.
    if getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
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
_PERSON_YEAR_PLACEHOLDER = {None, 0, 1900}
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


@method_decorator(csrf_exempt, name='dispatch')
class PersonViewSet(viewsets.GenericViewSet):
    """
    Endpoints:
      POST /api/persons/find_or_create/  — resolve OIDC identity to a Person row
      PATCH /api/persons/{person_id}/    — fill-if-empty demographic patch
    """
    permission_classes = [ScopedTokenPermission]
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
            elif not (getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)):
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

        if changed:
            person.save(update_fields=changed)

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
    def get_queryset(self):
        qs = super().get_queryset()
        person_id = self.request.query_params.get('person_id')
        if person_id:
            qs = qs.filter(person_id=person_id)
        # Trusted backend (service-token): full visibility. Already
        # validated at the permission layer (ScopedTokenPermission).
        if is_service_token(self.request):
            return qs
        org = get_request_org(self.request)
        if org is not None:
            from omop_core.models import PatientRecord
            allowed = PatientRecord.objects.filter(organization=org).values('person_id')
            qs = qs.filter(person_id__in=allowed)
        elif not (self.request.user and (
            getattr(self.request.user, 'is_superuser', False) or
            getattr(self.request.user, 'is_staff', False)
        )):
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
        return qs


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
        elif not (getattr(self.request.user, 'is_superuser', False) or getattr(self.request.user, 'is_staff', False)):
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
        elif not (getattr(self.request.user, 'is_superuser', False) or getattr(self.request.user, 'is_staff', False)):
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
class ConditionOccurrenceViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ConditionOccurrenceSerializer
    permission_classes = [ScopedTokenPermission]
    queryset = ConditionOccurrence.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class DrugExposureViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = DrugExposureSerializer
    permission_classes = [ScopedTokenPermission]
    queryset = DrugExposure.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class MeasurementViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = MeasurementSerializer
    permission_classes = [ScopedTokenPermission]
    queryset = Measurement.objects.all()
    ordering_fields = ['measurement_date', 'measurement_id']
    ordering = ['-measurement_date']

    def get_queryset(self):
        qs = super().get_queryset()
        concept_id = self.request.query_params.get('measurement_concept_id')
        if concept_id:
            qs = qs.filter(measurement_concept_id=concept_id)
        source_concept_id = self.request.query_params.get('measurement_source_concept_id')
        if source_concept_id:
            qs = qs.filter(measurement_source_concept_id=source_concept_id)
        concept_code = self.request.query_params.get('concept_code')
        if concept_code:
            from omop_core.models import Concept
            cids = list(
                Concept.objects.filter(concept_code=concept_code)
                .values_list('concept_id', flat=True)
            )
            qs = qs.filter(measurement_concept_id__in=cids)
        date_gte = self.request.query_params.get('measurement_date__gte')
        if date_gte:
            qs = qs.filter(measurement_date__gte=date_gte)
        date_lte = self.request.query_params.get('measurement_date__lte')
        if date_lte:
            qs = qs.filter(measurement_date__lte=date_lte)
        visit_id = self.request.query_params.get('visit_occurrence_id')
        if visit_id:
            qs = qs.filter(visit_occurrence_id=visit_id)
        return qs


@method_decorator(csrf_exempt, name='dispatch')
class ObservationViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ObservationSerializer
    permission_classes = [ScopedTokenPermission]
    queryset = Observation.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class ProcedureOccurrenceViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ProcedureOccurrenceSerializer
    permission_classes = [ScopedTokenPermission]
    queryset = ProcedureOccurrence.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class EpisodeViewSet(_ProvenanceMixin, _OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = EpisodeSerializer
    permission_classes = [ScopedTokenPermission]
    queryset = Episode.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class EpisodeEventViewSet(viewsets.ModelViewSet):
    serializer_class = EpisodeEventSerializer
    permission_classes = [ScopedTokenPermission]

    def list(self, request, *args, **kwargs):
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
        # Bootstrap patients (organization=NULL) are included so that create-path
        # and read-path are symmetric.
        org = get_request_org(self.request)
        if org is not None:
            from django.db.models import Q
            allowed_pids = PatientRecord.objects.filter(
                Q(organization=org) | Q(organization__isnull=True)
            ).values('person_id')
            allowed_episodes = Episode.objects.filter(person_id__in=allowed_pids).values('episode_id')
            qs = qs.filter(episode_id__in=allowed_episodes)
        elif self.request.user and not (
            getattr(self.request.user, 'is_superuser', False) or
            getattr(self.request.user, 'is_staff', False)
        ):
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
        elif self.request.user and not (
            getattr(self.request.user, 'is_superuser', False) or
            getattr(self.request.user, 'is_staff', False)
        ):
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

    Response 200:
        { "LOINC": { "2160-0": 3013682, "2345-7": null }, "SNOMED": { ... } }

    Unknown codes return null; healthkey-etl substitutes concept_id=0 downstream.
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

    return Response(result)


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


def _serialize_concept(concept):
    return {
        'concept_id': concept.concept_id,
        'concept_name': concept.concept_name,
        'vocabulary_id': concept.vocabulary_id,
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
    return paginator.get_paginated_response([_serialize_concept(c) for c in page])


@api_view(['GET'])
@permission_classes([ScopedTokenPermission])
def concept_search(request):
    """
    Search OMOP concepts by name (case-insensitive substring).

    Query params:
        q                 required, minimum 2 characters
        vocabulary_id     optional exact-match filter (e.g. LOINC, SNOMED)
        domain_id         optional exact-match filter (e.g. Measurement)
        concept_class_id  optional exact-match filter (e.g. Lab Test)
        standard_concept  optional exact-match filter (S or C)
        page / page_size  pagination (page_size capped at 100)

    Response 200: paginated {count, next, previous, results: [concept, ...]}
    """
    query = (request.query_params.get('q') or '').strip()
    if len(query) < 2:
        return Response(
            {'detail': "Query parameter 'q' is required and must be at least 2 characters."},
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
    permission_classes = [ScopedTokenPermission]
    queryset = PatientDocument.objects.all()


class PatientTrialEnrollmentViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    """CRUD for a patient's clinical trial enrollment status.

    Trial metadata (title, phase, eligibility, etc.) is NOT stored here.
    Use ``trial_id`` to retrieve that data from the EXACT trial-matcher API.

    Filter by person: GET /api/trial-enrollments/?person_id=42
    """
    serializer_class = PatientTrialEnrollmentSerializer
    permission_classes = [ScopedTokenPermission]
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
        if not (user and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))):
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
    permission_classes = [ScopedTokenPermission]
    queryset = PatientSurveyResponse.objects.select_related('survey').all()
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

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
            is_privileged = user and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
            if org is None and not person_id and not is_privileged:
                return qs.none()
        return qs

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)
