from rest_framework import serializers
from django.contrib.auth.models import User
from omop_core.models import (
    PatientInfo,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    PatientDocument, PatientTrialEnrollment, ProvenanceRecord,
)
from omop_oncology.models import Episode, EpisodeEvent
from datetime import date


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name']


class PatientListSerializer(serializers.ModelSerializer):
    """Serializer for patient list view with key fields"""
    person_id = serializers.IntegerField(source='person.person_id', read_only=True)
    patient_name = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField(format='%Y-%m-%d', read_only=True)
    
    class Meta:
        model = PatientInfo
        fields = [
            'id',
            'person_id',
            'patient_name',
            'age',
            'disease',
            'stage',
            'updated_at',
        ]
    
    def get_patient_name(self, obj):
        # Get name from Person model (OMOP extension)
        if obj.person:
            full_name = f"{obj.person.given_name or ''} {obj.person.family_name or ''}".strip()
            return full_name if full_name else f"Patient {obj.person.person_id}"
        return f"Patient {obj.person.person_id}"
    
    def get_age(self, obj):
        if obj.date_of_birth:
            today = date.today()
            age = today.year - obj.date_of_birth.year - ((today.month, today.day) < (obj.date_of_birth.month, obj.date_of_birth.day))
            return age
        return None


class PatientInfoSerializer(serializers.ModelSerializer):
    """Read-only serializer for PatientInfo. All fields are derived from OMOP tables via refresh_patient_info."""
    person_id = serializers.IntegerField(source='person.person_id', read_only=True)
    patient_name = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()
    gender = serializers.SerializerMethodField()
    refractory_status = serializers.CharField(source='treatment_refractory_status', read_only=True)

    class Meta:
        model = PatientInfo
        fields = '__all__'
        read_only_fields = []

    def get_patient_name(self, obj):
        if obj.person:
            full_name = f"{obj.person.given_name or ''} {obj.person.family_name or ''}".strip()
            return full_name if full_name else f"Patient {obj.person.person_id}"
        return f"Patient {obj.person.person_id}"

    def get_age(self, obj):
        if obj.date_of_birth:
            today = date.today()
            age = today.year - obj.date_of_birth.year - ((today.month, today.day) < (obj.date_of_birth.month, obj.date_of_birth.day))
            return age
        return None

    def get_gender(self, obj):
        if obj.person and obj.person.gender_concept:
            gender_name = obj.person.gender_concept.concept_name
            if gender_name == 'MALE':
                return 'Male'
            elif gender_name == 'FEMALE':
                return 'Female'
            else:
                return 'Other'
        return 'Unknown'

# ---------------------------------------------------------------------------
# OMOP clinical event serializers
# ---------------------------------------------------------------------------

class ConditionOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConditionOccurrence
        fields = [
            'condition_occurrence_id', 'person', 'condition_concept',
            'condition_start_date', 'condition_start_datetime',
            'condition_end_date', 'condition_end_datetime',
            'condition_type_concept', 'condition_status_concept',
            'stop_reason', 'condition_source_value', 'condition_source_concept',
            'condition_status_source_value',
        ]


class DrugExposureSerializer(serializers.ModelSerializer):
    class Meta:
        model = DrugExposure
        fields = [
            'drug_exposure_id', 'person', 'drug_concept',
            'drug_exposure_start_date', 'drug_exposure_start_datetime',
            'drug_exposure_end_date', 'drug_exposure_end_datetime',
            'drug_type_concept', 'stop_reason', 'quantity', 'days_supply',
            'route_concept', 'lot_number',
            'drug_source_value', 'drug_source_concept',
            'route_source_value', 'dose_unit_source_value',
        ]


class MeasurementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Measurement
        fields = [
            'measurement_id', 'person', 'measurement_concept',
            'measurement_date', 'measurement_datetime',
            'measurement_type_concept', 'operator_concept',
            'value_as_number', 'value_as_string', 'value_as_concept',
            'unit_concept', 'range_low', 'range_high',
            'measurement_source_value', 'measurement_source_concept',
            'unit_source_value', 'value_source_value',
        ]


class ObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Observation
        fields = [
            'observation_id', 'person', 'observation_concept',
            'observation_date', 'observation_datetime',
            'observation_type_concept',
            'value_as_number', 'value_as_string', 'value_as_concept',
            'qualifier_concept', 'unit_concept',
            'observation_source_value', 'observation_source_concept',
            'unit_source_value', 'qualifier_source_value', 'value_source_value',
        ]


class ProcedureOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcedureOccurrence
        fields = [
            'procedure_occurrence_id', 'person', 'procedure_concept',
            'procedure_date', 'procedure_datetime',
            'procedure_end_date', 'procedure_end_datetime',
            'procedure_type_concept', 'modifier_concept', 'quantity',
            'procedure_source_value', 'procedure_source_concept',
            'modifier_source_value',
        ]


class EpisodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Episode
        fields = [
            'episode_id', 'person', 'episode_concept',
            'episode_start_date', 'episode_start_datetime',
            'episode_end_date', 'episode_end_datetime',
            'episode_number', 'episode_object_concept', 'episode_type_concept',
            'episode_source_value', 'episode_source_concept',
        ]


class EpisodeEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = EpisodeEvent
        fields = ['episode_id', 'event_id', 'episode_event_field_concept']


# ---------------------------------------------------------------------------
# HealthTree parity serializers
# ---------------------------------------------------------------------------

class PatientDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientDocument
        fields = [
            'id', 'person', 'doc_type', 'title',
            'file_url', 'file_name', 'verified', 'uploaded_at',
        ]


# ---------------------------------------------------------------------------
# Clinical trial enrollment (status tracker — metadata from EXACT)
# ---------------------------------------------------------------------------

class PatientTrialEnrollmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientTrialEnrollment
        fields = ['id', 'person', 'trial_id', 'nct_id', 'status']


class ProvenanceRecordSerializer(serializers.ModelSerializer):
    record_type = serializers.CharField(source='content_type.model', read_only=True)

    class Meta:
        model = ProvenanceRecord
        fields = ['id', 'source', 'source_user_id', 'target_patient_id',
                  'modification_reason', 'created_at', 'record_type', 'object_id', 'organization']
