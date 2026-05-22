"""
Dedicated sync API for hk-labs → ctomop writes.

POST /api/lab-results/sync/

Accepts a batch of measurements from hk-labs commit step.
Handles:
  - LOINC concept lookup
  - UCUM unit mapping
  - HK-Labs source concept creation (for LOINC-unmatched tests)
  - CareSite get_or_create (if lab_name provided)
  - VisitOccurrence creation (one per upload/commit)
Returns: created measurement_ids + visit_occurrence_id
"""
import re
import unicodedata
from datetime import date

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.authorization import can_access_patient, get_actor_role
from omop_core.models import (
    CareSite, Concept, Measurement, Person, ProvenanceRecord, VisitOccurrence,
)
from patient_portal.api.permissions import ScopedTokenPermission, get_request_org

logger = logging.getLogger(__name__)

HK_LABS_VOCAB_ID = 'HK-Labs'
HK_LABS_CONCEPT_ID_START = 2000000000
PATIENT_SELF_REPORT_CONCEPT_ID = 32865
DOCUMENT_EXTRACTION_CONCEPT_ID = 32883
OUTPATIENT_VISIT_CONCEPT_ID = 9202


def _normalize_slug(name):
    """Normalize a test name to a stable concept_code slug: 'hkl:<slug>'."""
    s = unicodedata.normalize('NFKD', name.lower())
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return f'hkl:{s}'


def _next_pk(model, pk_field):
    last = model.objects.order_by(f'-{pk_field}').values_list(pk_field, flat=True).first()
    return (last + 1) if last else 1


def _next_hk_concept_id():
    last = (
        Concept.objects
        .filter(vocabulary_id=HK_LABS_VOCAB_ID)
        .order_by('-concept_id')
        .values_list('concept_id', flat=True)
        .first()
    )
    return (last + 1) if last else HK_LABS_CONCEPT_ID_START


class MeasurementItemSerializer(serializers.Serializer):
    loinc_code = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    test_name = serializers.CharField()
    value = serializers.DecimalField(max_digits=15, decimal_places=5, required=False, allow_null=True)
    value_string = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    unit = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    measured_at = serializers.DateField()
    range_low = serializers.DecimalField(max_digits=15, decimal_places=5, required=False, allow_null=True)
    range_high = serializers.DecimalField(max_digits=15, decimal_places=5, required=False, allow_null=True)
    source_text = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    source_unit = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    match_method = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class SyncRequestSerializer(serializers.Serializer):
    person_id = serializers.IntegerField(required=False, allow_null=True)
    actor_iss = serializers.CharField(required=False, allow_blank=True, default="")
    actor_sub = serializers.CharField(required=False, allow_blank=True, default="")
    measurements = MeasurementItemSerializer(many=True)
    lab_name = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    lab_date = serializers.DateField(required=False, allow_null=True)
    report_filename = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    source_type = serializers.ChoiceField(
        choices=['patient_self_report', 'document_extraction'],
        default='document_extraction',
    )


class SyncView(APIView):
    """
    POST /api/lab-results/sync/

    Body:
    {
      "person_id": 123,
      "measurements": [...],
      "lab_name": "Quest Diagnostics",
      "lab_date": "2026-05-15",
      "report_filename": "bloodwork-may-2026.pdf",
      "source_type": "document_extraction"
    }
    """
    permission_classes = [ScopedTokenPermission]

    @transaction.atomic
    def post(self, request):
        serializer = SyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        actor_iss = data.get('actor_iss', '')
        actor_sub = data.get('actor_sub', '')
        person_id = data.get('person_id')
        is_on_behalf_of = bool(person_id)

        if not person_id:
            person_id = self._resolve_person_from_identity(actor_iss, actor_sub)
            if person_id is None:
                return Response(
                    {'detail': 'Cannot resolve person from actor identity.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if not Person.objects.filter(person_id=person_id).exists():
            return Response(
                {'detail': f'Person {person_id} does not exist.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Authorization: check access only when actor is explicitly identified
        actor_identity = self._resolve_actor_identity(actor_iss, actor_sub, request.user)
        has_explicit_actor = bool(actor_iss and actor_sub)
        if has_explicit_actor and actor_identity and is_on_behalf_of:
            if not can_access_patient(actor_identity, person_id):
                return Response(
                    {'detail': 'Actor does not have access to this patient.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Legacy org-scoped check (OAuth2 service clients)
        org = get_request_org(request)
        if org is not None:
            from omop_core.models import PatientInfo
            if not PatientInfo.objects.filter(person_id=person_id, organization=org).exists():
                return Response(
                    {'detail': 'Person not in your organization.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        source_type = data['source_type']
        type_concept_id = (
            PATIENT_SELF_REPORT_CONCEPT_ID
            if source_type == 'patient_self_report'
            else DOCUMENT_EXTRACTION_CONCEPT_ID
        )

        care_site = self._get_or_create_care_site(data.get('lab_name'))
        visit = self._create_visit_occurrence(
            person_id=person_id,
            care_site=care_site,
            lab_date=data.get('lab_date') or date.today(),
            report_filename=data.get('report_filename'),
            type_concept_id=type_concept_id,
        )

        measurement_ids = []
        for item in data['measurements']:
            m_id = self._create_measurement(
                person_id=person_id,
                item=item,
                visit=visit,
                type_concept_id=type_concept_id,
            )
            measurement_ids.append(m_id)

        # Provenance
        self._record_provenance(
            actor_identity=actor_identity,
            actor_iss=actor_iss,
            actor_sub=actor_sub,
            target_person_id=person_id,
            is_on_behalf_of=is_on_behalf_of,
            visit=visit,
            measurement_ids=measurement_ids,
            org=org,
        )

        return Response({
            'visit_occurrence_id': visit.visit_occurrence_id,
            'measurement_ids': measurement_ids,
            'count': len(measurement_ids),
        }, status=status.HTTP_201_CREATED)

    def _resolve_actor_identity(self, actor_iss, actor_sub, request_user):
        """Resolve the actor Identity for authorization checks."""
        if actor_iss and actor_sub:
            from patient_portal.models import Identity
            try:
                return Identity.objects.get(issuer=actor_iss, sub=actor_sub)
            except Identity.DoesNotExist:
                return None
        if request_user and request_user.is_authenticated:
            return request_user
        return None

    def _record_provenance(self, *, actor_identity, actor_iss, actor_sub,
                           target_person_id, is_on_behalf_of, visit,
                           measurement_ids, org):
        """Record provenance for all measurements created in this sync."""
        if is_on_behalf_of:
            source = 'ADMIN_CORRECTION'
        else:
            source = 'DOCUMENT_EXTRACTION'

        source_user_id = ''
        if actor_iss and actor_sub:
            source_user_id = f"{actor_iss}|{actor_sub}"
        elif actor_identity:
            source_user_id = f"{actor_identity.issuer}|{actor_identity.sub}"

        ct = ContentType.objects.get_for_model(Measurement)
        records = [
            ProvenanceRecord(
                source=source,
                source_user_id=source_user_id,
                target_patient_id=str(target_person_id),
                organization=org,
                content_type=ct,
                object_id=m_id,
            )
            for m_id in measurement_ids
        ]
        ProvenanceRecord.objects.bulk_create(records)

    def _resolve_person_from_identity(self, actor_iss, actor_sub):
        """Resolve (issuer, sub) → person_id, auto-provisioning if needed."""
        if not actor_iss or not actor_sub:
            return None

        from patient_portal.models import Identity, PatientUser
        from omop_core.models import PatientInfo

        identity, _ = Identity.objects.get_or_create(
            issuer=actor_iss, sub=actor_sub,
        )

        try:
            return PatientUser.objects.get(identity=identity).person_id
        except PatientUser.DoesNotExist:
            pass

        if identity.email:
            pi = PatientInfo.objects.filter(email=identity.email).first()
            if pi:
                PatientUser.objects.create(identity=identity, person=pi.person)
                return pi.person_id

        last = Person.objects.order_by("-person_id").first()
        new_id = (last.person_id + 1) if last else 1000
        person = Person.objects.create(
            person_id=new_id,
            year_of_birth=1900,
            gender_source_value="unknown",
            race_source_value="unknown",
            ethnicity_source_value="unknown",
        )
        PatientUser.objects.create(identity=identity, person=person)
        return person.person_id

    def _get_or_create_care_site(self, lab_name):
        if not lab_name:
            return None
        care_site = CareSite.objects.filter(care_site_name=lab_name).first()
        if care_site:
            return care_site
        cs_id = _next_pk(CareSite, 'care_site_id')
        return CareSite.objects.create(
            care_site_id=cs_id,
            care_site_name=lab_name,
            care_site_source_value=lab_name[:50],
        )

    def _create_visit_occurrence(self, person_id, care_site, lab_date, report_filename, type_concept_id):
        visit_id = _next_pk(VisitOccurrence, 'visit_occurrence_id')
        visit_concept = Concept.objects.filter(concept_id=OUTPATIENT_VISIT_CONCEPT_ID).first()
        type_concept = Concept.objects.filter(concept_id=type_concept_id).first()

        if not visit_concept:
            visit_concept = Concept.objects.first()
        if not type_concept:
            type_concept = visit_concept

        return VisitOccurrence.objects.create(
            visit_occurrence_id=visit_id,
            person_id=person_id,
            visit_concept=visit_concept,
            visit_start_date=lab_date,
            visit_end_date=lab_date,
            visit_type_concept=type_concept,
            care_site_id=care_site.care_site_id if care_site else None,
            visit_source_value=(report_filename or '')[:50],
        )

    def _create_measurement(self, person_id, item, visit, type_concept_id):
        loinc_code = item.get('loinc_code')
        test_name = item['test_name']

        measurement_concept_id = 0
        measurement_source_concept_id = None
        measurement_source_value = item.get('match_method') or ''

        if loinc_code:
            concept = Concept.objects.filter(
                vocabulary_id='LOINC',
                concept_code=loinc_code,
            ).first()
            if concept:
                measurement_concept_id = concept.concept_id
            else:
                measurement_source_concept_id = self._get_or_create_hk_concept(test_name)
                measurement_source_value = test_name[:50]
        else:
            measurement_source_concept_id = self._get_or_create_hk_concept(test_name)
            measurement_source_value = test_name[:50]

        unit_concept_id = None
        unit_str = item.get('unit')
        if unit_str:
            ucum_concept = Concept.objects.filter(
                vocabulary_id='UCUM',
                concept_code=unit_str,
            ).first()
            if ucum_concept:
                unit_concept_id = ucum_concept.concept_id

        type_concept = Concept.objects.filter(concept_id=type_concept_id).first()
        if not type_concept:
            type_concept = Concept.objects.first()

        m_id = _next_pk(Measurement, 'measurement_id')
        Measurement.objects.create(
            measurement_id=m_id,
            person_id=person_id,
            measurement_concept_id=measurement_concept_id,
            measurement_date=item['measured_at'],
            measurement_type_concept=type_concept,
            value_as_number=item.get('value'),
            value_as_string=item.get('value_string') or '',
            unit_concept_id=unit_concept_id,
            range_low=item.get('range_low'),
            range_high=item.get('range_high'),
            visit_occurrence=visit,
            measurement_source_value=measurement_source_value[:50],
            measurement_source_concept_id=measurement_source_concept_id,
            unit_source_value=(item.get('source_unit') or item.get('unit') or '')[:50],
            value_source_value=(item.get('source_text') or '')[:50],
        )
        return m_id

    def _get_or_create_hk_concept(self, test_name):
        """Get or create a HK-Labs custom vocabulary concept for a LOINC-unmatched test."""
        code = _normalize_slug(test_name)
        existing = Concept.objects.filter(
            vocabulary_id=HK_LABS_VOCAB_ID,
            concept_code=code,
        ).first()
        if existing:
            return existing.concept_id

        concept_id = _next_hk_concept_id()
        Concept.objects.create(
            concept_id=concept_id,
            concept_name=test_name[:255],
            domain_id='Measurement',
            vocabulary_id=HK_LABS_VOCAB_ID,
            concept_class_id='Lab Test',
            standard_concept=None,
            concept_code=code[:50],
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )
        return concept_id
