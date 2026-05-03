from rest_framework import serializers
from django.contrib.auth.models import User
from omop_core.models import (
    PatientInfo,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    PatientDocument,
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
        read_only_fields = '__all__'

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
        fields = '__all__'


class DrugExposureSerializer(serializers.ModelSerializer):
    class Meta:
        model = DrugExposure
        fields = '__all__'


class MeasurementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Measurement
        fields = '__all__'


class ObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Observation
        fields = '__all__'


class ProcedureOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcedureOccurrence
        fields = '__all__'


class EpisodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Episode
        fields = '__all__'


class EpisodeEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = EpisodeEvent
        fields = '__all__'


# ---------------------------------------------------------------------------
# HealthTree parity serializers
# ---------------------------------------------------------------------------

class PatientDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientDocument
        fields = '__all__'
