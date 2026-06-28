from rest_framework import serializers
from patient_portal.models import Identity
from omop_core.models import (
    PatientInfo, Concept,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    PatientDocument, PatientTrialEnrollment, ProvenanceRecord,
    Survey, PatientSurveyResponse,
    StemCellTransplant, SctEligibility,
    Organization, OrgTrust, OrgInvitation, GroupAccess,
)
from omop_oncology.models import Episode, EpisodeEvent
from datetime import date
from django.utils.timezone import localdate
from django.utils import timezone


class UserSerializer(serializers.ModelSerializer):
    is_org_admin = serializers.SerializerMethodField()

    class Meta:
        model = Identity
        fields = ['id', 'sub', 'email', 'name', 'is_staff', 'is_org_admin']

    def get_is_org_admin(self, obj):
        now = timezone.now()
        from django.db.models import Q
        return GroupAccess.objects.filter(
            identity=obj,
            role='org_admin',
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        ).exists()


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


class OrgTrustSerializer(serializers.ModelSerializer):
    granting_org_slug = serializers.SlugRelatedField(
        source='granting_org', slug_field='slug', read_only=True,
    )
    # Write field: accepts an org PK when creating a trust
    trusted_org = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )
    # Read field: exposes the trusted org's slug in responses
    trusted_org_slug = serializers.SlugRelatedField(
        source='trusted_org', slug_field='slug', read_only=True, allow_null=True,
    )

    class Meta:
        model = OrgTrust
        fields = [
            'id', 'granting_org_slug',
            'trusted_org', 'trusted_org_slug',
            'trusted_domain', 'created_at',
        ]
        read_only_fields = ['id', 'granting_org_slug', 'trusted_org_slug', 'created_at']

    def validate(self, data):
        trusted_org = data.get('trusted_org')
        trusted_domain = data.get('trusted_domain', '')
        if trusted_org and trusted_domain:
            raise serializers.ValidationError(
                'Specify either trusted_org or trusted_domain, not both.'
            )
        if not trusted_org and not trusted_domain:
            raise serializers.ValidationError(
                'Specify either trusted_org or trusted_domain.'
            )
        return data


class OrgInvitationSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    org_slug = serializers.SlugRelatedField(source='org', slug_field='slug', read_only=True)

    class Meta:
        model = OrgInvitation
        fields = [
            'id', 'org_slug', 'email', 'role', 'status',
            'expires_at', 'created_at',
        ]
        read_only_fields = ['id', 'org_slug', 'status', 'expires_at', 'created_at']

    def get_status(self, obj):
        return obj.status


class GroupAccessSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='identity.email', read_only=True)
    org_slug = serializers.SlugRelatedField(source='org', slug_field='slug', read_only=True)
    group_name = serializers.CharField(source='group.name', read_only=True, default=None)

    class Meta:
        model = GroupAccess
        fields = [
            'id', 'email', 'org_slug', 'group_name', 'role',
            'expires_at', 'granted_at',
        ]
        read_only_fields = [
            'id', 'email', 'org_slug', 'group_name', 'role',
            'expires_at', 'granted_at',
        ]


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


class GenderField(serializers.CharField):
    """Translates between display values (Male/Female) and DB codes (M/F)."""
    DISPLAY_TO_CODE = {'Male': 'M', 'Female': 'F', 'Other': '', 'Unknown': ''}
    CODE_TO_DISPLAY = {'M': 'Male', 'F': 'Female'}

    def to_representation(self, value):
        return self.CODE_TO_DISPLAY.get(value, 'Unknown')

    def to_internal_value(self, data):
        title = str(data).title()
        return self.DISPLAY_TO_CODE.get(title, data)


class PatientInfoSerializer(serializers.ModelSerializer):
    person_id = serializers.IntegerField(source='person.person_id', read_only=True)
    patient_name = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()
    gender = GenderField(required=False, allow_blank=True, allow_null=True)
    refractory_status = serializers.CharField(source='treatment_refractory_status', read_only=True)
    first_line_therapy_display = serializers.SerializerMethodField()
    second_line_therapy_display = serializers.SerializerMethodField()
    later_therapy_display = serializers.SerializerMethodField()

    class Meta:
        model = PatientInfo
        fields = '__all__'
        # organization and person must never be client-writable: they are
        # set server-side from the auth token / FHIR upload respectively.
        # A client supplying either field in a PATCH would bypass tenant
        # isolation (organization) or reassign the record to another patient.
        read_only_fields = (
            'organization', 'person', 'created_at', 'updated_at',
            'first_line_therapy_display', 'second_line_therapy_display', 'later_therapy_display',
        )

    def get_patient_name(self, obj):
        if obj.person:
            full_name = f"{obj.person.given_name or ''} {obj.person.family_name or ''}".strip()
            return full_name if full_name else f"Patient {obj.person.person_id}"
        return f"Patient {obj.person.person_id}"

    def to_representation(self, instance):
        # Bulk-fetch all Concept rows referenced by therapy_id fields in one query,
        # replacing the per-field Concept.objects.filter() calls in the display methods.
        # NOTE: this fires one DB query per instance — do NOT use PatientInfoSerializer
        # in list views (many=True) without pre-fetching therapy_id concepts, as it
        # will produce N queries for N patients. Use PatientListSerializer for lists.
        concept_ids = set()
        if instance.first_line_therapy_id:
            concept_ids.add(instance.first_line_therapy_id)
        if instance.second_line_therapy_id:
            concept_ids.add(instance.second_line_therapy_id)
        concept_ids.update(instance.later_therapy_ids or [])
        self._therapy_concept_cache = (
            {c.concept_id: c for c in Concept.objects.filter(concept_id__in=concept_ids).only('concept_id', 'concept_name')}
            if concept_ids else {}
        )
        return super().to_representation(instance)

    def get_age(self, obj):
        if obj.date_of_birth:
            today = date.today()
            age = today.year - obj.date_of_birth.year - ((today.month, today.day) < (obj.date_of_birth.month, obj.date_of_birth.day))
            return age
        return None

    def get_first_line_therapy_display(self, obj):
        if obj.first_line_therapy_id:
            cache = getattr(self, '_therapy_concept_cache', {})
            c = cache.get(obj.first_line_therapy_id)
            return c.concept_name if c else obj.first_line_therapy
        return obj.first_line_therapy

    def get_second_line_therapy_display(self, obj):
        if obj.second_line_therapy_id:
            cache = getattr(self, '_therapy_concept_cache', {})
            c = cache.get(obj.second_line_therapy_id)
            return c.concept_name if c else obj.second_line_therapy
        return obj.second_line_therapy

    def get_later_therapy_display(self, obj):
        ids = obj.later_therapy_ids or []
        if not ids:
            return None
        cache = getattr(self, '_therapy_concept_cache', {})
        names = []
        for cid in ids:
            c = cache.get(cid)
            names.append(c.concept_name if c else str(cid))
        return names

    def validate_sct_date(self, value):
        if value is not None and value > localdate():
            raise serializers.ValidationError("SCT date cannot be in the future.")
        return value

    def validate_stem_cell_transplant_history(self, value):
        if not value:
            return value
        allowed = set(StemCellTransplant.objects.values_list('title', flat=True))
        bad = [v for v in value if not isinstance(v, str) or v not in allowed]
        if bad:
            raise serializers.ValidationError(
                f"Unrecognized stem_cell_transplant_history values: {bad}. "
                f"Allowed: {sorted(allowed)}"
            )
        return value

    def validate_sct_eligibility(self, value):
        if not value:
            return value
        allowed = set(SctEligibility.objects.values_list('title', flat=True))
        bad = [v for v in value if not isinstance(v, str) or v not in allowed]
        if bad:
            raise serializers.ValidationError(
                f"Unrecognized sct_eligibility values: {bad}. "
                f"Allowed: {sorted(allowed)}"
            )
        for transplant_type in ('autologous', 'allogeneic'):
            eligible = f'eligible for {transplant_type} SCT'
            ineligible = f'ineligible for {transplant_type} SCT'
            if eligible in value and ineligible in value:
                raise serializers.ValidationError(
                    f"Cannot be both eligible and ineligible for {transplant_type} SCT."
                )
        return value

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
        extra_kwargs = {'condition_occurrence_id': {'required': False}}


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
        extra_kwargs = {'drug_exposure_id': {'required': False}}


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
        extra_kwargs = {'measurement_id': {'required': False}}


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
        extra_kwargs = {'observation_id': {'required': False}}


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
        extra_kwargs = {'procedure_occurrence_id': {'required': False}}


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


class SurveySerializer(serializers.ModelSerializer):
    class Meta:
        model = Survey
        fields = ['id', 'external_id', 'name', 'title', 'description',
                  'status', 'disease', 'pages', 'estimated_minutes', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_pages(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('pages must be a list.')
        return value


class PatientSurveyResponseSerializer(serializers.ModelSerializer):
    survey_title = serializers.CharField(source='survey.title', read_only=True)
    survey_name = serializers.CharField(source='survey.name', read_only=True)

    class Meta:
        model = PatientSurveyResponse
        fields = ['id', 'person', 'survey', 'survey_title', 'survey_name',
                  'values', 'values_dates', 'percent_complete',
                  'started_at', 'completed_at', 'consent_date', 'consent_signature',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_percent_complete(self, value):
        if not (0 <= value <= 100):
            raise serializers.ValidationError('percent_complete must be between 0 and 100.')
        return value

    def validate_values(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('values must be a dict.')
        return value

    def validate_values_dates(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('values_dates must be a dict.')
        return value

    def update(self, instance, validated_data):
        # Merge incoming values/values_dates into existing dicts (autosave support).
        for field in ('values', 'values_dates'):
            if field in validated_data:
                current = getattr(instance, field) or {}
                validated_data[field] = {**current, **validated_data[field]}
        return super().update(instance, validated_data)
