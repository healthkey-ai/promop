from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.utils import timezone
from .models import PatientUser, PatientMessage, PatientConsent
from omop_core.models import PatientInfo, Measurement, ConditionOccurrence

def index(request):
    """Root view - redirect to portal or show login info"""
    if request.user.is_authenticated:
        return redirect('/portal/')
    else:
        # Show a simple page with login link
        return render(request, 'patient_portal/index.html')

@login_required
def dashboard(request):
    """Patient dashboard showing health summary"""
    try:
        patient_user = PatientUser.objects.get(identity=request.user)
        patient_info = PatientInfo.objects.filter(person=patient_user.person).first()
        
        # Get recent measurements
        recent_measurements = Measurement.objects.filter(
            person=patient_user.person
        ).select_related('measurement_concept', 'unit_concept').order_by('-measurement_date')[:10]
        
        # Get conditions
        conditions = ConditionOccurrence.objects.filter(
            person=patient_user.person
        ).select_related('condition_concept').order_by('-condition_start_date')[:5]
        
        # Get unread messages
        unread_messages = PatientMessage.objects.filter(
            patient_user=patient_user,
            sender_is_patient=False,
            is_read=False
        ).count()
        
        context = {
            'patient_info': patient_info,
            'recent_measurements': recent_measurements,
            'conditions': conditions,
            'unread_messages': unread_messages,
        }
        return render(request, 'patient_portal/dashboard.html', context)
    except PatientUser.DoesNotExist:
        messages.error(request, 'Patient profile not found. Please contact administrator.')
        return redirect('patient_portal:login')

@login_required
def health_records(request):
    """View detailed health records"""
    try:
        patient_user = get_object_or_404(PatientUser, identity=request.user)
        patient_info = PatientInfo.objects.filter(person=patient_user.person).first()

        # Get all measurements
        measurements = Measurement.objects.filter(
            person=patient_user.person
        ).select_related('measurement_concept', 'unit_concept').order_by('-measurement_date')
        
        # Get all conditions
        conditions = ConditionOccurrence.objects.filter(
            person=patient_user.person
        ).select_related('condition_concept').order_by('-condition_start_date')
        
        context = {
            'patient_info': patient_info,
            'measurements': measurements,
            'conditions': conditions,
        }
        return render(request, 'patient_portal/health_records.html', context)
    except PatientUser.DoesNotExist:
        messages.error(request, 'Patient profile not found.')
        return redirect('patient_portal:dashboard')

@login_required
def messages_list(request):
    """View and send messages"""
    try:
        patient_user = get_object_or_404(PatientUser, identity=request.user)

        if request.method == 'POST':
            subject = request.POST.get('subject')
            message_text = request.POST.get('message')
            
            if subject and message_text:
                PatientMessage.objects.create(
                    patient_user=patient_user,
                    subject=subject,
                    message=message_text,
                    sender_is_patient=True
                )
                messages.success(request, 'Message sent successfully')
                return redirect('patient_portal:messages')
            else:
                messages.error(request, 'Please provide both subject and message')
        
        patient_messages = PatientMessage.objects.filter(
            patient_user=patient_user
        ).order_by('-created_at')
        
        context = {
            'patient_messages': patient_messages,
        }
        return render(request, 'patient_portal/messages.html', context)
    except PatientUser.DoesNotExist:
        messages.error(request, 'Patient profile not found.')
        return redirect('patient_portal:dashboard')

@login_required
def consent_management(request):
    """Manage consents for data sharing and clinical trials"""
    try:
        patient_user = get_object_or_404(PatientUser, identity=request.user)

        if request.method == 'POST':
            consent_type = request.POST.get('consent_type')
            consent_granted = request.POST.get('consent_granted') == 'on'
            
            PatientConsent.objects.update_or_create(
                patient_user=patient_user,
                consent_type=consent_type,
                defaults={'consent_granted': consent_granted}
            )
            messages.success(request, 'Consent updated successfully')
            return redirect('patient_portal:consents')
        
        consents = PatientConsent.objects.filter(patient_user=patient_user)
        
        # Ensure all consent types exist
        consent_types = ['data_sharing', 'clinical_trial', 'research']
        for consent_type in consent_types:
            PatientConsent.objects.get_or_create(
                patient_user=patient_user,
                consent_type=consent_type,
                defaults={'consent_granted': False}
            )
        
        consents = PatientConsent.objects.filter(patient_user=patient_user)
        
        context = {
            'consents': consents,
        }
        return render(request, 'patient_portal/consents.html', context)
    except PatientUser.DoesNotExist:
        messages.error(request, 'Patient profile not found.')
        return redirect('patient_portal:dashboard')

def patient_login(request):
    """Patient login view"""
    if request.user.is_authenticated:
        return redirect('patient_portal:dashboard')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            # Check if user has a patient profile
            try:
                patient_user = PatientUser.objects.get(identity=user)
                login(request, user)
                patient_user.last_login = timezone.now()
                patient_user.save()
                return redirect('patient_portal:dashboard')
            except PatientUser.DoesNotExist:
                messages.error(request, 'You do not have access to the patient portal. Please contact administrator.')
        else:
            messages.error(request, 'Invalid username or password')
    
    return render(request, 'patient_portal/login.html')

@login_required
def patient_logout(request):
    """Patient logout view"""
    logout(request)
    messages.success(request, 'You have been logged out successfully')
    return redirect('patient_portal:login')

@login_required
def update_health_records(request):
    """Update patient health records"""
    if request.method != 'POST':
        return redirect('patient_portal:health_records')
    
    try:
        patient_user = get_object_or_404(PatientUser, identity=request.user)
        patient_info, created = PatientInfo.objects.get_or_create(person=patient_user.person)
        
        tab = request.POST.get('tab', 'general')
        
        # Helper function to convert empty strings to None
        def get_value(field_name, convert_type=None):
            value = request.POST.get(field_name, '').strip()
            if not value:
                return None
            if convert_type:
                try:
                    return convert_type(value)
                except (ValueError, TypeError):
                    return None
            return value
        
        # Helper function for checkboxes
        def get_bool(field_name):
            return field_name in request.POST
        
        # Helper function for multi-select
        def get_multi_select(field_name):
            values = request.POST.getlist(field_name)
            return ', '.join(values) if values else None
        
        # Update based on tab
        if tab == 'general':
            # Update user name
            first_name = get_value('first_name') or ''
            last_name = get_value('last_name') or ''
            new_name = f"{first_name} {last_name}".strip()
            if new_name:
                request.user.name = new_name
                request.user.save()
            
            # Update patient info
            patient_info.patient_age = get_value('patient_age', int)
            patient_info.gender = get_value('gender')
            patient_info.weight_kg = get_value('weight_kg', float)
            patient_info.height_cm = get_value('height_cm', float)
            patient_info.ethnicity = get_value('ethnicity')
            patient_info.systolic_bp = get_value('systolic_bp', int)
            patient_info.diastolic_bp = get_value('diastolic_bp', int)
            patient_info.location = get_value('location')
            patient_info.postal_code = get_value('postal_code')
            patient_info.disease = get_value('disease')
            patient_info.stage = get_value('stage')
            patient_info.karnofsky_performance_status = get_value('karnofsky_performance_status', int)
            patient_info.ecog_performance_status = get_value('ecog_performance_status', int)
            patient_info.active_malignancies = get_multi_select('active_malignancies')
            patient_info.active_infection = get_bool('active_infection')
            patient_info.preexisting_conditions = get_multi_select('preexisting_conditions')
            patient_info.peripheral_neuropathy_grade = get_value('peripheral_neuropathy_grade', int)
            
        elif tab == 'treatment':
            patient_info.prior_lines_of_therapy = get_value('prior_lines_of_therapy', int)
            patient_info.prior_treatments = get_value('prior_treatments')
            patient_info.prior_chemotherapy = get_bool('prior_chemotherapy')
            patient_info.prior_radiation = get_bool('prior_radiation')
            patient_info.prior_surgery = get_bool('prior_surgery')
            patient_info.prior_immunotherapy = get_bool('prior_immunotherapy')
            patient_info.prior_targeted_therapy = get_bool('prior_targeted_therapy')
            patient_info.current_medications = get_value('current_medications')
            patient_info.allergies = get_value('allergies')
            
        elif tab == 'blood':
            patient_info.hemoglobin_g_dl = get_value('hemoglobin_g_dl', float)
            patient_info.hematocrit_percent = get_value('hematocrit_percent', float)
            patient_info.wbc_count_thousand_per_ul = get_value('wbc_count_thousand_per_ul', float)
            patient_info.rbc_million_per_ul = get_value('rbc_million_per_ul', float)
            patient_info.platelet_count_thousand_per_ul = get_value('platelet_count_thousand_per_ul', float)
            patient_info.anc_thousand_per_ul = get_value('anc_thousand_per_ul', float)
            patient_info.alc_thousand_per_ul = get_value('alc_thousand_per_ul', float)
            patient_info.amc_thousand_per_ul = get_value('amc_thousand_per_ul', float)
            patient_info.serum_creatinine_mg_dl = get_value('serum_creatinine_mg_dl', float)
            patient_info.creatinine_clearance_ml_min = get_value('creatinine_clearance_ml_min', float)
            patient_info.creatinine_mg_dl = get_value('creatinine_mg_dl', float)
            patient_info.egfr_ml_min_173m2 = get_value('egfr_ml_min_173m2', float)
            patient_info.bun_mg_dl = get_value('bun_mg_dl', float)
            patient_info.sodium_meq_l = get_value('sodium_meq_l', float)
            patient_info.potassium_meq_l = get_value('potassium_meq_l', float)
            patient_info.serum_calcium_mg_dl = get_value('serum_calcium_mg_dl', float)
            patient_info.calcium_mg_dl = get_value('calcium_mg_dl', float)
            patient_info.magnesium_mg_dl = get_value('magnesium_mg_dl', float)
            
        elif tab == 'labs':
            patient_info.bilirubin_total_mg_dl = get_value('bilirubin_total_mg_dl', float)
            patient_info.alt_u_l = get_value('alt_u_l', float)
            patient_info.ast_u_l = get_value('ast_u_l', float)
            patient_info.alkaline_phosphatase_u_l = get_value('alkaline_phosphatase_u_l', float)
            patient_info.albumin_g_dl = get_value('albumin_g_dl', float)
            patient_info.troponin_ng_ml = get_value('troponin_ng_ml', float)
            patient_info.bnp_pg_ml = get_value('bnp_pg_ml', float)
            patient_info.lvef_percent = get_value('lvef_percent', float)
            patient_info.glucose_mg_dl = get_value('glucose_mg_dl', float)
            patient_info.hba1c_percent = get_value('hba1c_percent', float)
            patient_info.ldh_u_l = get_value('ldh_u_l', float)
            patient_info.inr = get_value('inr', float)
            patient_info.pt_seconds = get_value('pt_seconds', float)
            patient_info.ptt_seconds = get_value('ptt_seconds', float)
            patient_info.cea_ng_ml = get_value('cea_ng_ml', float)
            patient_info.ca19_9_u_ml = get_value('ca19_9_u_ml', float)
            patient_info.psa_ng_ml = get_value('psa_ng_ml', float)
            patient_info.hiv_status = get_value('hiv_status')
            patient_info.hepatitis_b_status = get_value('hepatitis_b_status')
            patient_info.hepatitis_c_status = get_value('hepatitis_c_status')
            
        elif tab == 'behavior':
            patient_info.able_to_consent = get_bool('able_to_consent')
            patient_info.has_caregiver = get_bool('has_caregiver')
            patient_info.using_contraceptive = get_bool('using_contraceptive')
            patient_info.is_pregnant = get_bool('is_pregnant')
            patient_info.pregnancy_test_result = get_value('pregnancy_test_result')
            patient_info.is_lactating = get_bool('is_lactating')
            patient_info.tobacco_use_status = get_value('tobacco_use_status')
            patient_info.recreational_drug_use = get_value('recreational_drug_use')
            patient_info.mental_health_disorders = get_value('mental_health_disorders')
            patient_info.geographic_risk_factors = get_value('geographic_risk_factors')
            patient_info.occupational_risk_factors = get_value('occupational_risk_factors')
            patient_info.english_speak_understand = get_bool('english_speak_understand')
            patient_info.english_read_write = get_bool('english_read_write')
            patient_info.spanish_speak_understand = get_bool('spanish_speak_understand')
            patient_info.spanish_read_write = get_bool('spanish_read_write')
        
        patient_info.save()
        messages.success(request, f'✅ {tab.title()} information updated successfully!')
        
    except Exception as e:
        messages.error(request, f'❌ Error updating records: {str(e)}')
    
    return redirect('patient_portal:health_records')
