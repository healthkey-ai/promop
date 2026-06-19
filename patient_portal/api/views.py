from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from patient_portal.models import Identity
from django.contrib.auth import logout, login, authenticate
from django.db import IntegrityError, transaction
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from omop_core.models import (
    Person, PatientInfo, Concept, ProvenanceRecord,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    PatientDocument, PatientTrialEnrollment, Survey, PatientSurveyResponse,
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
    FlipIScore, FollicularLymphomaGrade,
    BreastCancerFirstLineTherapy, BreastCancerSecondLineTherapy, BreastCancerLaterLineTherapy,
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_info_service import refresh_patient_info
from omop_core.services.lot_inference_service import infer_lot_for_person
from omop_core.services.omop_write_service import sync_to_omop
from omop_core.services.mappings import get_gender_concept, LAB_FIELD_TO_LOINC
from omop_core.services.pk import next_pk
from omop_core.services.rxnav_service import resolve_drug as _rxnav_resolve_drug
from omop_core.services.concept_cache import concept_by_id as _cc_by_id, concept_by_loinc as _cc_by_loinc
from datetime import datetime
from django.utils.timezone import localdate
import csv
import hashlib
import json
import logging
from io import StringIO
from .permissions import ScopedTokenPermission, get_request_org
from .providers.base import TokenClaims
from .serializers import (
    UserSerializer, PatientInfoSerializer, PatientListSerializer, ProvenanceRecordSerializer,
    ConditionOccurrenceSerializer, DrugExposureSerializer, MeasurementSerializer,
    ObservationSerializer, ProcedureOccurrenceSerializer,
    EpisodeSerializer, EpisodeEventSerializer,
    PatientDocumentSerializer, PatientTrialEnrollmentSerializer,
    SurveySerializer, PatientSurveyResponseSerializer,
)
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


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


@method_decorator(csrf_exempt, name='dispatch')
class PatientInfoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PatientInfoSerializer
    permission_classes = [ScopedTokenPermission]
    
    def get_queryset(self):
        qs = PatientInfo.objects.all().select_related('person')
        org = get_request_org(self.request)
        if org is not None:
            qs = qs.filter(organization=org)
        elif not (self.request.user and (
            getattr(self.request.user, 'is_superuser', False) or
            getattr(self.request.user, 'is_staff', False)
        )):
            # Session / partner-auth users: scope to only the patients they can
            # access — their own record (PatientUser) and any patients in their
            # professional groups (ProfessionalGroupAccess). Doctors/admins with
            # group access see their whole panel; is_staff bypasses this entirely.
            from patient_portal.models import PatientUser
            from omop_core.models import PatientGroupMembership, ProfessionalGroupAccess
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

            # Professional group access (non-expired grants)
            now = timezone.now()
            actor_group_ids = ProfessionalGroupAccess.objects.filter(
                identity=self.request.user,
            ).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now),
            ).values_list('group_id', flat=True)

            if actor_group_ids:
                group_pids = PatientGroupMembership.objects.filter(
                    group_id__in=actor_group_ids
                ).values_list('person_id', flat=True)
                accessible_pids.update(group_pids)

            if not accessible_pids:
                return qs.none()

            qs = qs.filter(person_id__in=accessible_pids)
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return PatientListSerializer
        return PatientInfoSerializer
    
    def list(self, request):
        """List all patients - accessible to authenticated users"""
        queryset = self.get_queryset().order_by('-created_at')
        serializer = PatientListSerializer(queryset, many=True)
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

        serializer = PatientInfoSerializer(data=data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save(person=person)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        """Get detailed patient info for a specific person"""
        try:
            person = Person.objects.get(person_id=pk)
            patient_info = PatientInfo.objects.get(person=person)
        except Person.DoesNotExist:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientInfo.DoesNotExist:
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

        patient_serializer = PatientInfoSerializer(patient_info)

        return Response({
            'patient_info': patient_serializer.data,
            'user': user_data
        })

    def partial_update(self, request, pk=None):
        """PATCH /api/patient-info/{person_id}/ — update PatientInfo and write through to OMOP."""
        try:
            person = Person.objects.get(person_id=pk)
            patient_info = PatientInfo.objects.get(person=person)
        except Person.DoesNotExist:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientInfo.DoesNotExist:
            return Response({'error': 'Patient information not found'}, status=status.HTTP_404_NOT_FOUND)

        org = get_request_org(request)
        if org is not None:
            if patient_info.organization != org:
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        elif not request.user.is_superuser and not getattr(request.user, 'is_staff', False):
            from omop_core.authorization import can_access_patient
            if not can_access_patient(request.user, person.person_id):
                return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)

        prov_source, prov_user_id, prov_reason = _extract_provenance(request)
        if prov_source == 'ADMIN_CORRECTION' and not prov_reason:
            return Response(
                {'error': 'modification_reason is required when source is ADMIN_CORRECTION'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Capture previous values for fields being changed (exclude provenance meta-fields).
        # Use {field}_id for FK fields so we get a serializable PK, not a model object.
        _prov_meta = {'source', 'source_user_id', 'modification_reason'}
        _read_only = set(PatientInfoSerializer.Meta.read_only_fields)
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

        serializer = PatientInfoSerializer(patient_info, data=request.data, partial=True)
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
            patient_info = PatientInfo.objects.get(person=person)
        except Person.DoesNotExist:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientInfo.DoesNotExist:
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
        # Build a single query for all provenance records across PatientInfo + OMOP tables
        q = Q(
            content_type=ContentType.objects.get_for_model(PatientInfo),
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
        """GET/PATCH /api/patient-info/me/ — current user's own PatientInfo."""
        from patient_portal.services import resolve_or_create_person

        person = resolve_or_create_person(request.user)
        patient_info, _ = PatientInfo.objects.get_or_create(person=person)

        if request.method == 'GET':
            user_serializer = UserSerializer(request.user)
            patient_serializer = PatientInfoSerializer(patient_info)
            full_name = f"{person.given_name or ''} {person.family_name or ''}".strip()
            return Response({
                'patient_info': patient_serializer.data,
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

        serializer = PatientInfoSerializer(patient_info, data=patch_data, partial=True)
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
                    
                    patient_info, pi_created = PatientInfo.objects.update_or_create(
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

            # Group resources by patient
            patients_data = {}
            
            for entry in fhir_data.get('entry', []):
                resource = entry.get('resource', {})
                resource_type = resource.get('resourceType')
                
                if resource_type == 'Patient':
                    patient_id = resource.get('id', '')
                    patients_data[patient_id] = {
                        'patient': resource,
                        'conditions': [],
                        'observations': [],
                        'medications': []
                    }
                elif resource_type == 'Condition':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = patient_ref.split('/')[-1] if '/' in patient_ref else ''
                    if patient_id in patients_data:
                        patients_data[patient_id]['conditions'].append(resource)
                elif resource_type == 'Observation':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = patient_ref.split('/')[-1] if '/' in patient_ref else ''
                    if patient_id in patients_data:
                        patients_data[patient_id]['observations'].append(resource)
                elif resource_type == 'MedicationStatement':
                    patient_ref = resource.get('subject', {}).get('reference', '')
                    patient_id = patient_ref.split('/')[-1] if '/' in patient_ref else ''
                    if patient_id in patients_data:
                        patients_data[patient_id]['medications'].append(resource)
            
            # Hoist SCT vocabulary sets for FHIR upload validation (avoids N+1 per patient).
            _allowed_sct_titles = set(StemCellTransplant.objects.values_list('title', flat=True))
            _allowed_elig_titles = set(SctEligibility.objects.values_list('title', flat=True))

            # Hoist constant Concept lookups — these are the same for every patient and every
            # observation. Using the process-level concept_cache means each of these is a
            # zero-cost memory hit on all subsequent calls (across batches and requests).
            _concept_breast_cancer = Concept.objects.filter(
                concept_name__icontains='breast cancer'
            ).first()
            _concept_ehr_type      = _cc_by_id(32817)    # EHR
            _concept_lab_type      = _cc_by_id(32856)    # Lab
            _concept_drug_type     = _cc_by_id(32869)    # EHR prescription
            _concept_tx_regimen    = _cc_by_id(32531)    # Treatment Regimen
            _concept_de_field      = _cc_by_id(1147094)  # DrugExposure field
            _concept_generic_lab   = _cc_by_id(3000963)  # Generic lab

            # When skip_refresh=true the caller (e.g. load_fhir_bundle) will run
            # refresh_patient_info for all patients after the upload completes.
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
                        'http://ctomop.io/fhir/StructureDefinition/race':
                            ('valueString', lambda e: e.get('valueString')),
                        'http://ctomop.io/fhir/StructureDefinition/ethnicity':
                            ('valueString', lambda e: e.get('valueString')),
                        'http://ctomop.io/fhir/StructureDefinition/bodyWeight':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'http://ctomop.io/fhir/StructureDefinition/bodyHeight':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'http://ctomop.io/fhir/StructureDefinition/systolic-bp':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'http://ctomop.io/fhir/StructureDefinition/diastolic-bp':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'http://ctomop.io/fhir/StructureDefinition/heartRate':
                            ('valueQuantity', lambda e: e.get('valueQuantity', {}).get('value')),
                        'http://ctomop.io/fhir/StructureDefinition/ecog-performance-status':
                            ('valueInteger', lambda e: e.get('valueInteger')),
                        'http://ctomop.io/fhir/StructureDefinition/mm-cytogenetic-markers':
                            ('valueString', lambda e: e.get('valueString')),
                        'http://ctomop.io/fhir/StructureDefinition/mm-measurable-disease-imwg':
                            ('valueBoolean', lambda e: e.get('valueBoolean')),
                        'http://ctomop.io/fhir/StructureDefinition/mm-sct-date':
                            ('valueString', lambda e: e.get('valueString')),
                        'http://ctomop.io/fhir/StructureDefinition/mm-sct-history':
                            ('valueString', lambda e: e.get('valueString')),
                        'http://ctomop.io/fhir/StructureDefinition/mm-sct-eligibility':
                            ('valueString', lambda e: e.get('valueString')),
                    }
                    ext_results = {}
                    for ext in patient_resource.get('extension', []):
                        url = ext.get('url', '')
                        if url in _PATIENT_EXTENSIONS:
                            _, parser = _PATIENT_EXTENSIONS[url]
                            ext_results[url] = parser(ext)

                    base = 'http://ctomop.io/fhir/StructureDefinition/'
                    race            = ext_results.get(f'{base}race')
                    ethnicity       = ext_results.get(f'{base}ethnicity')
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
                    
                    # Suppress signal-triggered PatientInfo refreshes for all OMOP
                    # writes below. Use __enter__/__exit__ explicitly so the finally
                    # block guarantees cleanup even on BaseException (e.g. KeyboardInterrupt),
                    # without requiring 1000 lines of re-indentation.
                    from omop_core.signals import suppress_patient_info_refresh as _suppress_cm_fn
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

                    # Upsert Person: match on name + birth year to avoid duplicates on re-upload
                    person = None
                    person_is_new = False
                    if (given_name or family_name) and year_of_birth:
                        person = Person.objects.filter(
                            given_name=given_name,
                            family_name=family_name,
                            year_of_birth=year_of_birth,
                        ).first()
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

                    # Extract disease, stage, and histologic type from Condition
                    disease = 'Breast Cancer'
                    stage = ''
                    histologic_type = ''
                    condition_date = None

                    for condition in data['conditions']:
                        # Get histologic type from code
                        code = condition.get('code', {})
                        if code.get('text'):
                            histologic_type = code['text']
                        elif code.get('coding') and len(code['coding']) > 0:
                            histologic_type = code['coding'][0].get('display', '')
                        
                        # Get stage
                        stages = condition.get('stage', [])
                        if stages and len(stages) > 0:
                            stage_summary = stages[0].get('summary', {})
                            if stage_summary.get('text'):
                                stage_text = stage_summary['text']
                                if 'Stage' in stage_text:
                                    stage = stage_text.split('Stage')[-1].strip()
                            elif stage_summary.get('coding') and len(stage_summary['coding']) > 0:
                                stage = stage_summary['coding'][0].get('code', '')
                        
                        # Get condition onset date
                        if condition.get('onsetDateTime'):
                            try:
                                naive_dt = datetime.strptime(condition['onsetDateTime'], '%Y-%m-%d')
                                condition_date = timezone.make_aware(naive_dt)
                            except ValueError:
                                pass
                    
                    # Upsert ConditionOccurrence for the diagnosis
                    if condition_date:
                        from omop_core.models import ConditionOccurrence

                        # Use pre-hoisted concept lookups (computed once before the patient loop)
                        breast_cancer_concept = _concept_breast_cancer

                        if breast_cancer_concept:
                            type_concept = _concept_ehr_type or breast_cancer_concept

                            if not ConditionOccurrence.objects.filter(
                                person=person,
                                condition_concept=breast_cancer_concept,
                                condition_start_date=condition_date.date(),
                            ).exists():
                                last_condition = ConditionOccurrence.objects.all().order_by('-condition_occurrence_id').first()
                                condition_id = last_condition.condition_occurrence_id + 1 if last_condition else 1
                                _co = ConditionOccurrence(
                                    condition_occurrence_id=condition_id,
                                    person=person,
                                    condition_concept=breast_cancer_concept,
                                    condition_start_date=condition_date.date(),
                                    condition_start_datetime=condition_date,
                                    condition_type_concept=type_concept,
                                    condition_source_value=disease,
                                )
                                _co._skip_patient_info_refresh = True
                                try:
                                    with transaction.atomic():
                                        _co.save()
                                        _pt_condition_ids.append(_co.condition_occurrence_id)
                                        if prov_source:
                                            _record_provenance(_co, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
                                except Exception as _coex:
                                    logger.warning(
                                        '{"event": "condition_occurrence_save_failed", "person_id": %s, "error": "%s"}',
                                        person.person_id, _coex,
                                    )

                    # Process observations and create Measurement records
                    _timing_hash = hashlib.sha256(str(fhir_patient_id).encode()).hexdigest()[:12]
                    logger.info("TIMING patient=%s phase=person_setup elapsed=%.1fs", _timing_hash, _time.monotonic() - _pt_start)
                    from omop_core.models import Measurement
                    last_measurement = Measurement.objects.all().order_by('-measurement_id').first()
                    measurement_id = last_measurement.measurement_id + 1 if last_measurement else 1
                    
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
                            smoking_status = value_codeable
                        elif loinc_code == '63640-7':  # Pack Years
                            pack_years = value_number
                        elif loinc_code == '74013-4':  # Alcohol Use
                            alcohol_use = value_codeable
                        elif loinc_code == '11286-7':  # Drinks per Week
                            drinks_per_week = value_number
                        elif loinc_code == '68516-4':  # Exercise Frequency
                            exercise_frequency = value_codeable
                        elif loinc_code == '89555-7':  # Exercise Minutes per Week
                            exercise_minutes_per_week = value_number
                        elif loinc_code == '88365-2':  # Diet Type
                            diet_type = value_codeable
                        # Behavior - Sleep & Wellbeing
                        elif loinc_code == '93832-4':  # Sleep Hours per Night
                            sleep_hours_per_night = value_number
                        elif loinc_code == '93831-6':  # Sleep Quality
                            sleep_quality = value_codeable
                        elif loinc_code == '73985-4':  # Stress Level
                            stress_level = value_codeable
                        elif loinc_code == '93033-9':  # Social Support
                            social_support = value_codeable
                        # Behavior - Socioeconomic
                        elif loinc_code == '74165-2':  # Employment Status
                            employment_status = value_codeable
                        elif loinc_code == '82589-3':  # Education Level
                            education_level = value_codeable
                        elif loinc_code == '45404-1':  # Marital Status
                            marital_status = value_codeable
                        elif loinc_code == '76513-1':  # Insurance Type
                            insurance_type = value_codeable
                        elif loinc_code == '63512-8':  # Number of Dependents
                            number_of_dependents = value_number
                        elif loinc_code == '77243-3':  # Annual Household Income
                            annual_household_income = value_number
                        # Cancer Assessment Fields
                        elif loinc_code == '89247-1':  # ECOG Performance Status
                            # Store the date from effectiveDateTime
                            if observation.get('effectiveDateTime'):
                                ecog_assessment_date = observation['effectiveDateTime'][:10]
                        elif loinc_code == '85337-4':  # Test Methodology
                            test_methodology = value_codeable
                            # Also check if this is Oncotype DX score
                            if value_number is not None:
                                oncotype_dx_score = value_number
                        elif loinc_code == '31208-2':  # Specimen Source
                            test_specimen_type = value_codeable
                            if observation.get('effectiveDateTime'):
                                test_date = observation['effectiveDateTime'][:10]
                        elif loinc_code == '69548-6':  # Test Interpretation
                            report_interpretation = value_codeable
                        elif loinc_code == '16112-5':  # Androgen Receptor
                            androgen_receptor_status = value_codeable
                        elif loinc_code == '42804-5':  # Therapy Intent
                            obs_date = observation.get('effectiveDateTime', '')[:10] if observation.get('effectiveDateTime') else None
                            therapy_intent_observations.append({'date': obs_date, 'value': value_codeable})
                            if not therapy_intent:  # Keep first for backwards compatibility
                                therapy_intent = value_codeable
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
                            pregnancy_test_result_value = value_codeable
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
                        
                        # Check for lymph node status
                        elif 'lymph node' in obs_text or 'lymph nodes' in obs_text:
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    lymph_node_status = value_concept['text']
                                elif value_concept.get('coding'):
                                    lymph_node_status = value_concept['coding'][0].get('display')
                        
                        # Check for metastasis status
                        elif 'metastasis' in obs_text or 'metastases' in obs_text:
                            if observation.get('valueCodeableConcept'):
                                value_concept = observation['valueCodeableConcept']
                                if value_concept.get('text'):
                                    metastasis_status = value_concept['text']
                                elif value_concept.get('coding'):
                                    metastasis_status = value_concept['coding'][0].get('display')

                        # TNM staging fields
                        if obs_text == 'tumor stage' or loinc_code == '21905-5':
                            tumor_stage = (observation.get('valueCodeableConcept') or {}).get('text')
                        elif obs_text == 'nodes stage' or loinc_code == '21906-3':
                            nodes_stage = (observation.get('valueCodeableConcept') or {}).get('text')
                        elif obs_text == 'distant metastasis stage' or loinc_code == '21901-4':
                            distant_metastasis_stage = (observation.get('valueCodeableConcept') or {}).get('text')
                        elif obs_text == 'staging modality' or loinc_code == '85319-2':
                            staging_modalities = observation.get('valueString')
                        elif 'recist' in obs_text or loinc_code == '21908-9':
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
                    _existing_measurements: dict[tuple, object] = {
                        (m.measurement_concept_id, m.measurement_date, m.measurement_source_value): m
                        for m in Measurement.objects.filter(person=person)
                    }

                    for observation in data['observations']:
                        obs_date = None
                        if observation.get('effectiveDateTime'):
                            try:
                                naive_dt = datetime.strptime(observation['effectiveDateTime'], '%Y-%m-%d')
                                obs_date = timezone.make_aware(naive_dt)
                            except ValueError:
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
                        elif observation.get('valueCodeableConcept'):
                            value_concept = observation['valueCodeableConcept']
                            if value_concept.get('text'):
                                value_string = value_concept['text']
                            elif value_concept.get('coding'):
                                value_string = value_concept['coding'][0].get('display')
                        
                        # Find measurement concept — LOINC lookup first (FHIR-06/07/08),
                        # fall back to name-based, then generic lab concept.
                        measurement_concept = None
                        obs_loinc = None
                        for _c in obs_code.get('coding', []):
                            if _c.get('system') == 'http://loinc.org':
                                obs_loinc = _c.get('code')
                                break
                        if obs_loinc:
                            measurement_concept = _cc_by_loinc(obs_loinc)
                        if not measurement_concept:
                            try:
                                measurement_concept = Concept.objects.filter(
                                    concept_name__icontains=obs_name[:50]
                                ).first()
                            except Exception:
                                pass
                        if not measurement_concept:
                            # Use pre-hoisted generic lab test concept if not found
                            measurement_concept = _concept_generic_lab

                        if measurement_concept:
                            # Use pre-hoisted Lab type concept (32856 = Lab)
                            type_concept = _concept_lab_type or measurement_concept

                            # Use LOINC code as source_value when available — it's short,
                            # unique, and avoids collisions from truncating long display names.
                            source_value = obs_loinc if obs_loinc else obs_name[:50]
                            try:
                                with transaction.atomic():
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
                                            existing_m._skip_patient_info_refresh = True
                                            existing_m.save()
                                    else:
                                        _m = Measurement(
                                            measurement_id=measurement_id,
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
                                        _m._skip_patient_info_refresh = True
                                        _m.save()
                                        _pt_measurement_ids.append(_m.measurement_id)
                                        # Keep the dict current so duplicate observations
                                        # in the same patient don't re-insert the same row.
                                        _existing_measurements[_mkey] = _m
                                        if prov_source:
                                            _record_provenance(_m, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
                                        measurement_id += 1
                            except Exception as _mex:
                                logger.warning(
                                    '{"event": "measurement_save_failed", "obs": "%s", "error": "%s"}',
                                    obs_name[:50], _mex,
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
                    last_drug = DrugExposure.objects.all().order_by('-drug_exposure_id').first()
                    drug_exposure_id = last_drug.drug_exposure_id + 1 if last_drug else 1
                    last_episode = Episode.objects.all().order_by('-episode_id').first()
                    episode_id_counter = last_episode.episode_id + 1 if last_episode else 1

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
                                    regimen_concept = Concept.objects.filter(
                                        concept_name__icontains=regimen_name,
                                        domain__domain_id='Drug',
                                    ).first() if regimen_name else None
                                    # RxNav fallback only when no HemOnc concept_id and ILIKE found nothing
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
                                        drug_exposure_id=drug_exposure_id,
                                        person=person,
                                        drug_concept=regimen_concept,
                                        drug_exposure_start_date=lot_start,
                                        drug_exposure_end_date=lot_end,
                                        drug_type_concept=drug_type_concept,
                                        drug_source_value=(lot_data.get('regimen') or '')[:50],
                                    )
                                    _de._skip_patient_info_refresh = True
                                    _de.save()
                                    _pt_drug_exposure_ids.append(_de.drug_exposure_id)
                                    if prov_source:
                                        _record_provenance(_de, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
                                    drug_exposure_id += 1

                                # ep_source_concept reuses the same HemOnc lookup already resolved above
                                ep_source_concept = regimen_concept if _hemonc_cid else None

                                # Upsert Episode for this LOT
                                ep_concept = _concept_tx_regimen or regimen_concept
                                ep_obj_concept = regimen_concept
                                ep_type_concept = _concept_ehr_type or regimen_concept

                                _ep = Episode.objects.filter(
                                    person=person,
                                    episode_source_value=f'LOT-{lot_num}',
                                ).first()
                                if _ep is None:
                                    _ep = Episode(
                                        episode_id=episode_id_counter,
                                        person=person,
                                        episode_concept=ep_concept,
                                        episode_object_concept=ep_obj_concept,
                                        episode_type_concept=ep_type_concept,
                                        episode_start_date=lot_start or datetime.now().date(),
                                        episode_end_date=lot_end,
                                        episode_number=lot_num,
                                        episode_source_value=f'LOT-{lot_num}',
                                        episode_source_concept=ep_source_concept,
                                    )
                                    _ep.save()
                                    _pt_episode_ids.append(_ep.episode_id)
                                    episode_id_counter += 1

                                # Link drug exposure to episode (idempotent)
                                ee_field_concept = _concept_de_field or regimen_concept
                                _ee, _ = EpisodeEvent.objects.get_or_create(
                                    episode_id=_ep.episode_id,
                                    event_id=_de.drug_exposure_id,
                                    defaults={'episode_event_field_concept': ee_field_concept},
                                )
                                _pt_episode_event_ids.append(_ee.pk)
                        except Exception as _e:
                            logger.warning(f"Could not write DrugExposure/Episode for LOT {lot_num}: {_e}")

                    # --- OMOP-first: refresh PatientInfo from OMOP tables ---
                    # Release suppression so the single intentional refresh can run.
                    # We stay inside the atomic block so that a refresh failure rolls
                    # back all OMOP writes for this patient.
                    _suppress_cm.__exit__(None, None, None)
                    logger.info("TIMING patient=%s phase=drug_exposures elapsed=%.1fs", _timing_hash, _time.monotonic() - _pt_start)
                    if _skip_refresh:
                        # Bulk mode — just ensure the PatientInfo row exists so the
                        # patch block below has an object to write FHIR-specific fields
                        # into.  The full OMOP-derived refresh is deferred to the caller.
                        patient_info, _ = PatientInfo.objects.get_or_create(person=person)
                    else:
                        patient_info = refresh_patient_info(person)
                        infer_lot_for_person(person)

                    # --- Patch fields from FHIR that aren't yet in OMOP tables ---
                    # These fields come from FHIR parsing but are not (yet) stored in OMOP.
                    # Once full OMOP write coverage is achieved, this patch block can be removed.
                    _patch = {}
                    if birth_date:
                        _patch['date_of_birth'] = birth_date
                    if disease:
                        _patch['disease'] = disease
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
                    # Therapy lines (denormalized PatientInfo fields)
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
                    # and derived into PatientInfo via refresh_patient_info (FHIR-09).
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
                    if upload_org is not None:
                        _patch['organization'] = upload_org

                    # Apply patch to PatientInfo (suppress signal-triggering save)
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
                        'patient_info_id': patient_info.pk,
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
                    # If this patient failed, force-rollback at the connection level
                    # so a poisoned transaction doesn't cascade to subsequent patients
                    # in the same batch.
                    if _last_exc is not None:
                        try:
                            from django.db import connection as _db_conn
                            _db_conn.rollback()
                        except Exception:
                            pass
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
                    if org is not None and not PatientInfo.objects.filter(person=person, organization=org).exists():
                        errors.append("Person not found.")
                        continue
                    elif org is None and not _is_privileged:
                        from omop_core.authorization import can_access_patient
                        if not can_access_patient(request.user, person_id):
                            errors.append("Person not found.")
                            continue
                    # Delete PatientInfo
                    PatientInfo.objects.filter(person=person).delete()
                    # Delete associated Identity if exists (via PatientUser)
                    from patient_portal.models import PatientUser as PU
                    try:
                        pu = PU.objects.get(person=person)
                        pu.identity.delete()
                    except PU.DoesNotExist:
                        pass
                    # Delete Person
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
        'service': 'ctomop',
        'database': db_status,
    }, status=http_status)


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
            person, created = Person.objects.get_or_create(
                actor_iss=actor_iss,
                actor_sub=actor_sub,
                defaults={'person_id': lambda: next_pk(Person, 'person_id')},
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

        org = get_request_org(request)
        if org is not None:
            if not PatientInfo.objects.filter(person=person, organization=org).exists():
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
        org = get_request_org(self.request)
        if org is not None:
            from omop_core.models import PatientInfo
            allowed = PatientInfo.objects.filter(organization=org).values('person_id')
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

        # Org-scoping: reject cross-org persons; allow new/bootstrap patients
        org = get_request_org(self.request)
        if org is not None:
            person = serializer.validated_data.get('person')
            if person:
                from rest_framework.exceptions import PermissionDenied
                # Allow bootstrap (no PatientInfo yet) and unclaimed patients (org=NULL).
                # Block only when a PatientInfo exists and is already claimed by a different org.
                existing_pi = PatientInfo.objects.filter(person=person).first()
                if (existing_pi is not None
                        and existing_pi.organization is not None
                        and existing_pi.organization != org):
                    raise PermissionDenied('Person does not belong to your organization.')
        elif not (getattr(self.request.user, 'is_superuser', False) or getattr(self.request.user, 'is_staff', False)):
            from omop_core.authorization import can_access_patient
            from rest_framework.exceptions import PermissionDenied
            person = serializer.validated_data.get('person')
            if not person:
                raise PermissionDenied('person is required.')
            if not can_access_patient(self.request.user, person.person_id):
                raise PermissionDenied('Access denied.')

        obj = serializer.save()
        self._prov(obj)

    def perform_update(self, serializer):
        org = get_request_org(self.request)
        if org is not None:
            person = serializer.validated_data.get('person') or serializer.instance.person
            from rest_framework.exceptions import NotFound, PermissionDenied
            # On updates the patient must already have a PatientInfo; missing = not found.
            # Unclaimed patients (org=NULL) are allowed; only reject explicit cross-org.
            existing_pi = PatientInfo.objects.filter(person=person).first()
            if existing_pi is None:
                raise NotFound('Person not found.')
            if existing_pi.organization is not None and existing_pi.organization != org:
                raise PermissionDenied('Person does not belong to your organization.')
        elif not (getattr(self.request.user, 'is_superuser', False) or getattr(self.request.user, 'is_staff', False)):
            from omop_core.authorization import can_access_patient
            from rest_framework.exceptions import PermissionDenied
            person = serializer.validated_data.get('person') or serializer.instance.person
            if not person:
                raise PermissionDenied('person is required.')
            if not can_access_patient(self.request.user, person.person_id):
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
        # Org / per-patient scoping: EpisodeEvent.episode_id is a bare integer FK to Episode.
        # Resolve allowed episode_ids via the Episode → person → org chain.
        # Bootstrap patients (organization=NULL) are included so that create-path
        # and read-path are symmetric.
        org = get_request_org(self.request)
        if org is not None:
            from django.db.models import Q
            allowed_pids = PatientInfo.objects.filter(
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
            pi = PatientInfo.objects.filter(person_id=episode.person_id).first()
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
# HealthTree parity ViewSets
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
        if token == 'service-token':
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
