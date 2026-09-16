"""Author supportive courses separately from numbered anticancer therapy lines."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import DiseaseTherapyRegimen, PatientRecord, Person, SupportiveTherapyCourse, TherapyRegimen
from omop_core.services.treatment_catalog import disease_code, disease_filter, outcomes_for_disease
from .permissions import PatientCrudPermission, PatientSelfScopePermission


class SupportiveTherapySerializer(serializers.ModelSerializer):
    regimen_code = serializers.SlugRelatedField(source='regimen', slug_field='code', queryset=TherapyRegimen.objects.all())
    regimen_title = serializers.CharField(source='regimen.title', read_only=True)

    class Meta:
        model = SupportiveTherapyCourse
        fields = ['id', 'person', 'regimen_code', 'regimen_title', 'start_date', 'end_date', 'intent', 'discontinuation_reason']
        read_only_fields = ['id']

    def validate(self, data):
        person = data.get('person') or self.instance.person
        if self.instance and person.pk != self.instance.person_id:
            raise serializers.ValidationError({'person': 'Cannot move a supportive therapy to another patient.'})
        regimen = data.get('regimen') or (self.instance.regimen if self.instance else None)
        record = PatientRecord.objects.filter(person=person).first()
        links = DiseaseTherapyRegimen.objects.filter(regimen=regimen, round__code='supportive_therapy')
        code = disease_code(record.disease if record else '')
        if code:
            links = links.filter(disease_filter(code))
        if not links.exists() and not (self.instance and self.instance.regimen_id == regimen.pk):
            raise serializers.ValidationError({'regimen_code': 'Select an available supportive therapy for this disease.'})
        start = data.get('start_date', self.instance.start_date if self.instance else None)
        end = data.get('end_date', self.instance.end_date if self.instance else None)
        if start and start > timezone.localdate():
            raise serializers.ValidationError({'start_date': 'Start date cannot be in the future.'})
        if start and end and end < start:
            raise serializers.ValidationError({'end_date': 'End date cannot precede start date.'})
        return data


class SupportiveTherapyViewSet(viewsets.ViewSet):
    permission_classes = [PatientCrudPermission, PatientSelfScopePermission]

    def _save(self, request, instance=None):
        from .views import TherapyLineViewSet
        from .serializers import PatientRecordSerializer
        from omop_core.services.supportive_therapy_service import project_supportive_course
        from omop_core.services.patient_record_service import refresh_patient_record

        if instance:
            # Check the stored owner before validating any client-supplied person.
            denied = TherapyLineViewSet()._check_person_write_permission(request, instance.person)
            if denied is not None:
                return denied
            self.check_object_permissions(request, instance)
        else:
            person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.all()).run_validation(request.data.get('person'))
            denied = TherapyLineViewSet()._check_person_write_permission(request, person)
            if denied is not None:
                return denied
        serializer = SupportiveTherapySerializer(instance, data=request.data, partial=instance is not None)
        serializer.is_valid(raise_exception=True)
        person = instance.person if instance else serializer.validated_data['person']
        denied = TherapyLineViewSet()._check_person_write_permission(request, person)
        if denied is not None:
            return denied
        with transaction.atomic():
            # Same lock order as PatientRecord PATCH and derivation.
            get_object_or_404(PatientRecord.objects.select_for_update(), person=person)
            if instance:
                instance = SupportiveTherapyCourse.objects.select_for_update().get(pk=instance.pk)
                serializer = SupportiveTherapySerializer(instance, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
            course = serializer.save()
            project_supportive_course(course)
            record = refresh_patient_record(person)
        return Response({'course': SupportiveTherapySerializer(course).data,
                         'patient_info': PatientRecordSerializer(record, context={'request': request}).data}, status=200 if instance else 201)

    def create(self, request):
        return self._save(request)

    def partial_update(self, request, pk=None):
        return self._save(request, get_object_or_404(SupportiveTherapyCourse.objects.select_related('person', 'regimen'), pk=pk))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def therapy_outcomes(request):
    return Response(outcomes_for_disease(request.query_params.get('disease', '')))
