from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from oauth2_provider.contrib.rest_framework import (
    OAuth2Authentication,
    IsAuthenticatedOrTokenHasScope,
    TokenHasReadWriteScope,
    TokenMatchesOASRequirements,
)
from django.contrib.auth.models import User
from django.contrib.auth import logout, login, authenticate
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.conf import settings
from omop_core.models import (
    Person, PatientInfo, Concept,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    PatientDocument,
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_info_service import refresh_patient_info
from datetime import datetime
import csv
import json
import logging
from io import StringIO
from .serializers import (
    UserSerializer, PatientInfoSerializer, PatientListSerializer,
    ConditionOccurrenceSerializer, DrugExposureSerializer, MeasurementSerializer,
    ObservationSerializer, ProcedureOccurrenceSerializer,
    EpisodeSerializer, EpisodeEventSerializer,
    PatientDocumentSerializer,
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
        'grant_types_supported': ['authorization_code', 'refresh_token'],
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


def get_gender_concept(gender_str):
    """Map gender string to OMOP gender concept"""
    if not gender_str:
        return None
    
    gender_map = {
        'male': 8507,
        'm': 8507,
        'female': 8532,
        'f': 8532,
        'unknown': 8551,
        'other': 8551,
        'ambiguous': 8570,
    }
    
    gender_lower = gender_str.lower().strip()
    concept_id = gender_map.get(gender_lower)
    
    if concept_id:
        try:
            return Concept.objects.get(concept_id=concept_id)
        except Concept.DoesNotExist:
            return None
    return None

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

@method_decorator(csrf_exempt, name='dispatch')
class PatientInfoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PatientInfoSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return PatientInfo.objects.all().select_related('person')
    
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
            
            # Get the User associated with this person (not the logged-in user)
            try:
                patient_user = User.objects.get(id=person.person_id)
                user_serializer = UserSerializer(patient_user)
                user_data = user_serializer.data
            except User.DoesNotExist:
                user_data = None
            
            patient_serializer = PatientInfoSerializer(patient_info)
            
            return Response({
                'patient_info': patient_serializer.data,
                'user': user_data
            })
        except Person.DoesNotExist:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        except PatientInfo.DoesNotExist:
            return Response({'error': 'Patient information not found'}, status=status.HTTP_404_NOT_FOUND)
    
    @action(detail=False, methods=['post'])
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
    
    @action(detail=False, methods=['post'], permission_classes=[AllowAny], authentication_classes=[])
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
            
            created_count = 0
            errors = []
            
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
            
            # Process each patient
            for fhir_patient_id, data in patients_data.items():
                try:
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
                    
                    # Extract ethnicity and vital signs from extensions
                    ethnicity = None
                    weight = None
                    height = None
                    systolic_bp = None
                    diastolic_bp = None
                    heart_rate = None
                    ecog = None
                    
                    if patient_resource.get('extension'):
                        for ext in patient_resource['extension']:
                            url = ext.get('url', '')
                            if 'ethnicity' in url:
                                ethnicity = ext.get('valueString')
                            elif 'bodyWeight' in url:
                                weight = ext.get('valueQuantity', {}).get('value')
                            elif 'bodyHeight' in url:
                                height = ext.get('valueQuantity', {}).get('value')
                            elif 'systolic-bp' in url:
                                systolic_bp = ext.get('valueQuantity', {}).get('value')
                            elif 'diastolic-bp' in url:
                                diastolic_bp = ext.get('valueQuantity', {}).get('value')
                            elif 'heartRate' in url:
                                heart_rate = ext.get('valueQuantity', {}).get('value')
                            elif 'ecog-performance-status' in url:
                                ecog = ext.get('valueInteger')
                    
                    # Get gender concept from FHIR
                    gender_concept = get_gender_concept(patient_resource.get('gender', ''))
                    
                    # Extract name from FHIR
                    name = patient_resource.get('name', [{}])[0] if patient_resource.get('name') else {}
                    given_name = ' '.join(name.get('given', [])) if name.get('given') else ''
                    family_name = name.get('family', '')
                    
                    # Create Person with OMOP-compliant birth date fields and names
                    person = Person.objects.create(
                        person_id=person_id,
                        gender_concept=gender_concept,
                        year_of_birth=year_of_birth or datetime.now().year - 50,
                        month_of_birth=month_of_birth,
                        day_of_birth=day_of_birth,
                        ethnicity_concept=None,
                        given_name=given_name,
                        family_name=family_name,
                    )
                    
                    # Create User for authentication (optional, not used for display)
                    User.objects.create(
                        id=person.person_id,
                        username=f'patient{person.person_id}',
                        first_name=given_name,
                        last_name=family_name,
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
                    
                    # Create ConditionOccurrence for the diagnosis
                    if condition_date:
                        from omop_core.models import ConditionOccurrence
                        last_condition = ConditionOccurrence.objects.all().order_by('-condition_occurrence_id').first()
                        condition_id = last_condition.condition_occurrence_id + 1 if last_condition else 1
                        
                        # Get breast cancer concept (using a standard concept ID)
                        breast_cancer_concept = None
                        try:
                            breast_cancer_concept = Concept.objects.filter(
                                concept_name__icontains='breast cancer'
                            ).first()
                        except:
                            pass
                        
                        if breast_cancer_concept:
                            # Get EHR type concept (32817 = EHR)
                            type_concept = Concept.objects.filter(concept_id=32817).first()
                            if not type_concept:
                                type_concept = breast_cancer_concept
                            
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
                            _co.save()
                    
                    # Process observations and create Measurement records
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
                        
                        # Find or create measurement concept
                        measurement_concept = None
                        try:
                            measurement_concept = Concept.objects.filter(
                                concept_name__icontains=obs_name[:50]
                            ).first()
                        except:
                            pass
                        
                        if not measurement_concept:
                            # Use a generic lab test concept if not found
                            measurement_concept = Concept.objects.filter(concept_id=3000963).first()
                        
                        if measurement_concept:
                            # Get Lab type concept (32856 = Lab)
                            type_concept = Concept.objects.filter(concept_id=32856).first()
                            if not type_concept:
                                type_concept = measurement_concept
                            
                            _m = Measurement(
                                measurement_id=measurement_id,
                                person=person,
                                measurement_concept=measurement_concept,
                                measurement_date=obs_date.date(),
                                measurement_datetime=obs_date,
                                measurement_type_concept=type_concept,
                                value_as_number=value_number,
                                value_as_string=value_string,
                                measurement_source_value=obs_name[:50],
                                unit_source_value=unit[:50] if unit else None,
                            )
                            _m._skip_patient_info_refresh = True
                            _m.save()
                            measurement_id += 1
                    
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
                            
                            if therapy_line not in therapy_lines:
                                therapy_lines[therapy_line] = {
                                    'regimen': regimen_name,
                                    'start_date': start_date,
                                    'end_date': end_date,
                                    'outcome': therapy_outcome
                                }
                            else:
                                therapy_lines[therapy_line]['regimen'] = regimen_name
                                if start_date:
                                    therapy_lines[therapy_line]['start_date'] = start_date
                                if end_date:
                                    therapy_lines[therapy_line]['end_date'] = end_date
                                therapy_lines[therapy_line]['outcome'] = therapy_outcome
                    
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
                    last_drug = DrugExposure.objects.all().order_by('-drug_exposure_id').first()
                    drug_exposure_id = last_drug.drug_exposure_id + 1 if last_drug else 1
                    last_episode = Episode.objects.all().order_by('-episode_id').first()
                    episode_id_counter = last_episode.episode_id + 1 if last_episode else 1

                    for lot_num, lot_data in sorted(therapy_lines.items()):
                        try:
                            lot_start = None
                            lot_end = None
                            if lot_data.get('start_date'):
                                lot_start = datetime.strptime(lot_data['start_date'][:10], '%Y-%m-%d').date()
                            if lot_data.get('end_date'):
                                lot_end = datetime.strptime(lot_data['end_date'][:10], '%Y-%m-%d').date()

                            regimen_concept = Concept.objects.filter(
                                concept_name__icontains=lot_data.get('regimen', ''),
                                domain__domain_id='Drug',
                            ).first() if lot_data.get('regimen') else None
                            # Fall back to any Drug domain concept when named one not found
                            if regimen_concept is None:
                                regimen_concept = Concept.objects.filter(
                                    domain__domain_id='Drug'
                                ).first()
                            drug_type_concept = Concept.objects.filter(concept_id=32869).first()  # EHR prescription
                            # Fall back to regimen_concept if type concept not found
                            if drug_type_concept is None and regimen_concept is not None:
                                drug_type_concept = regimen_concept

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

                            # Create Episode for this LOT
                            # OMOP standard concepts for LOT episodes
                            ep_concept = Concept.objects.filter(concept_id=32531).first()  # Treatment Regimen
                            if ep_concept is None:
                                ep_concept = regimen_concept
                            ep_obj_concept = regimen_concept  # drug that is the object of this episode
                            ep_type_concept = Concept.objects.filter(concept_id=32817).first()  # EHR
                            if ep_type_concept is None:
                                ep_type_concept = regimen_concept

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
                            )
                            _ep.save()

                            # Link drug exposure to episode
                            # OMOP concept 1147094 = drug_exposure.drug_exposure_id field
                            ee_field_concept = Concept.objects.filter(concept_id=1147094).first()
                            if ee_field_concept is None:
                                ee_field_concept = regimen_concept
                            EpisodeEvent.objects.create(
                                episode_id=_ep.episode_id,
                                event_id=drug_exposure_id,
                                episode_event_field_concept=ee_field_concept,
                            )

                            drug_exposure_id += 1
                            episode_id_counter += 1
                        except Exception as _e:
                            logger.warning(f"Could not write DrugExposure/Episode for LOT {lot_num}: {_e}")

                    # --- OMOP-first: refresh PatientInfo from OMOP tables ---
                    patient_info = refresh_patient_info(person)

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
                        _patch['pd_l1_tumor_cels'] = pdl1_percentage
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
                    # Labs & other FHIR-derived fields not yet written to OMOP
                    _patch.update({k: v for k, v in {
                        'hemoglobin_g_dl': hemoglobin_g_dl,
                        'hematocrit_percent': hematocrit_percent,
                        'wbc_count_thousand_per_ul': wbc_count,
                        'rbc_million_per_ul': rbc_count,
                        'platelet_count_thousand_per_ul': platelet_count,
                        'anc_thousand_per_ul': anc_count,
                        'alc_thousand_per_ul': alc_count,
                        'amc_thousand_per_ul': amc_count,
                        'serum_calcium_mg_dl': serum_calcium,
                        'serum_creatinine_mg_dl': serum_creatinine,
                        'creatinine_clearance_ml_min': creatinine_clearance,
                        'egfr_ml_min_173m2': egfr,
                        'bun_mg_dl': bun,
                        'sodium_meq_l': sodium,
                        'potassium_meq_l': potassium,
                        'calcium_mg_dl': calcium,
                        'magnesium_mg_dl': magnesium,
                        'bilirubin_total_mg_dl': bilirubin_total,
                        'serum_bilirubin_level_direct': bilirubin_direct,
                        'alt_u_l': alt,
                        'ast_u_l': ast,
                        'alkaline_phosphatase_u_l': alkaline_phosphatase,
                        'albumin_g_dl': albumin,
                        'total_protein': total_protein,
                        'troponin_ng_ml': troponin,
                        'bnp_pg_ml': bnp,
                        'glucose_mg_dl': glucose,
                        'hba1c_percent': hba1c,
                        'ldh_u_l': ldh,
                        'inr': inr,
                        'pt_seconds': pt,
                        'ptt_seconds': ptt,
                        'cea_ng_ml': cea,
                        'ca19_9_u_ml': ca19_9,
                        'psa_ng_ml': psa,
                        'beta2_microglobulin': beta2_microglobulin,
                        'c_reactive_protein': c_reactive_protein,
                        'esr': esr,
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
                    # Apply patch to PatientInfo (suppress signal-triggering save)
                    for _field, _val in _patch.items():
                        setattr(patient_info, _field, _val)
                    patient_info.save()
                    
                    created_count += 1
                    logger.info(f"Successfully imported patient {fhir_patient_id} ({person.person_id}) with {measurement_id - (last_measurement.measurement_id + 1 if last_measurement else 1)} measurements (dates converted to timezone-aware UTC)")
                    
                except Exception as e:
                    errors.append(f"Patient {fhir_patient_id}: {str(e)}")
            
            return Response({
                'success': True,
                'created_count': created_count,
                'errors': errors
            })
            
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['delete'])
    def bulk_delete(self, request):
        """Delete multiple patients by person_ids"""
        person_ids = request.data.get('person_ids', [])
        
        if not person_ids:
            return Response({'error': 'No person_ids provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            deleted_count = 0
            errors = []
            
            for person_id in person_ids:
                try:
                    person = Person.objects.get(person_id=person_id)
                    # Delete PatientInfo
                    PatientInfo.objects.filter(person=person).delete()
                    # Delete associated User if exists
                    try:
                        User.objects.filter(id=person_id).delete()
                    except User.DoesNotExist:
                        pass
                    # Delete Person
                    person.delete()
                    deleted_count += 1
                except Person.DoesNotExist:
                    errors.append(f"Person {person_id} not found")
                except Exception as e:
                    errors.append(f"Person {person_id}: {str(e)}")
            
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
            'detail': str(e)
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
    """Test auth endpoint to diagnose login 500"""
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
# OMOP clinical event ViewSets
# =============================================================================

class _OmopFilterMixin:
    """Shared queryset filtering by person_id query param."""
    def get_queryset(self):
        qs = super().get_queryset()
        person_id = self.request.query_params.get('person_id')
        if person_id:
            qs = qs.filter(person_id=person_id)
        return qs


@method_decorator(csrf_exempt, name='dispatch')
class ConditionOccurrenceViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ConditionOccurrenceSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = ConditionOccurrence.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class DrugExposureViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = DrugExposureSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = DrugExposure.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class MeasurementViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = MeasurementSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = Measurement.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class ObservationViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ObservationSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = Observation.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class ProcedureOccurrenceViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = ProcedureOccurrenceSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = ProcedureOccurrence.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class EpisodeViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = EpisodeSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = Episode.objects.all()


@method_decorator(csrf_exempt, name='dispatch')
class EpisodeEventViewSet(viewsets.ModelViewSet):
    serializer_class = EpisodeEventSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']

    def get_queryset(self):
        episode_id = self.request.query_params.get('episode_id')
        qs = EpisodeEvent.objects.all()
        if episode_id:
            qs = qs.filter(episode_id=episode_id)
        return qs


# =============================================================================
# HealthTree parity ViewSets
# =============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class PatientDocumentViewSet(_OmopFilterMixin, viewsets.ModelViewSet):
    serializer_class = PatientDocumentSerializer
    permission_classes = [IsAuthenticatedOrTokenHasScope]
    required_scopes = ['patient/*.read']
    queryset = PatientDocument.objects.all()
