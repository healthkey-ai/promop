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
    CareSite, Concept, Measurement, MeasurementOwnership,
    Person, ProvenanceRecord, VisitOccurrence,
)
from omop_core.services.pk import next_pk, next_pk_batch
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


def _ensure_hk_deps(domain_id, concept_class_id):
    """Ensure the HK-Labs vocabulary, the given Domain, and ConceptClass exist."""
    from omop_core.models import Domain, ConceptClass, Vocabulary
    Vocabulary.objects.get_or_create(
        vocabulary_id=HK_LABS_VOCAB_ID,
        defaults={'vocabulary_name': 'HealthKey Labs', 'vocabulary_concept_id': 0},
    )
    Domain.objects.get_or_create(
        domain_id=domain_id,
        defaults={'domain_name': domain_id, 'domain_concept_id': 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id=concept_class_id,
        defaults={'concept_class_name': concept_class_id, 'concept_class_concept_id': 0},
    )


_HK_FALLBACK_CONCEPTS = {
    OUTPATIENT_VISIT_CONCEPT_ID: ('Outpatient Visit', 'Visit', 'Visit'),
    PATIENT_SELF_REPORT_CONCEPT_ID: ('Patient self-report', 'Type Concept', 'Type Concept'),
    DOCUMENT_EXTRACTION_CONCEPT_ID: ('Document extraction', 'Type Concept', 'Type Concept'),
}


def _ensure_concept(concept_id):
    """Return a Concept by ID, auto-creating an HK-Labs fallback if Athena vocabularies are not loaded."""
    concept = Concept.objects.filter(concept_id=concept_id).first()
    if concept:
        return concept

    fallback = _HK_FALLBACK_CONCEPTS.get(concept_id)
    if not fallback:
        return None
    name, domain_id, concept_class_id = fallback

    _ensure_hk_deps(domain_id, concept_class_id)

    logger.warning(
        'OMOP concept %d (%s) missing — creating HK-Labs fallback. '
        'Run load_athena_vocabularies for standard concepts.',
        concept_id, name,
    )
    return Concept.objects.create(
        concept_id=concept_id,
        concept_name=name,
        domain_id=domain_id,
        vocabulary_id=HK_LABS_VOCAB_ID,
        concept_class_id=concept_class_id,
        standard_concept=None,
        concept_code=f'hkl:fallback-{concept_id}',
        valid_start_date=date(1970, 1, 1),
        valid_end_date=date(2099, 12, 31),
    )


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

    def validate_measurements(self, value):
        if len(value) > 500:
            raise serializers.ValidationError("Maximum 500 measurements per sync request.")
        if len(value) == 0:
            raise serializers.ValidationError("At least one measurement is required.")
        return value

    def validate_actor_iss(self, value):
        if '|' in value:
            raise serializers.ValidationError("Pipe character not allowed in actor_iss.")
        return value

    def validate_actor_sub(self, value):
        if '|' in value:
            raise serializers.ValidationError("Pipe character not allowed in actor_sub.")
        return value


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
    throttle_scope = 'sync'

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
            if hasattr(request.user, 'issuer') and request.user.issuer != 'urn:service':
                from patient_portal.services import resolve_or_create_person
                person = resolve_or_create_person(request.user)
                person_id = person.person_id
            else:
                person_id = self._resolve_person_from_identity(actor_iss, actor_sub)
            if person_id is None:
                return Response(
                    {'detail': 'Cannot resolve person from actor identity.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if not Person.objects.filter(person_id=person_id).exists():
            return Response(
                {'detail': 'Person not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        actor_identity = self._resolve_actor_identity(actor_iss, actor_sub, request.user)
        has_explicit_actor = bool(actor_iss and actor_sub)

        if is_on_behalf_of:
            if has_explicit_actor and actor_identity is None:
                return Response(
                    {'detail': 'Actor identity not found.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if has_explicit_actor and actor_identity:
                if not can_access_patient(actor_identity, person_id):
                    return Response(
                        {'detail': 'Actor does not have access to this patient.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            elif not has_explicit_actor and not getattr(request.user, 'is_superuser', False):
                return Response(
                    {'detail': 'actor_iss and actor_sub required when writing on behalf of another person.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Org-scope enforcement for OAuth2 service clients
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

        visit_concept = _ensure_concept(OUTPATIENT_VISIT_CONCEPT_ID)
        type_concept = _ensure_concept(type_concept_id)
        if visit_concept is None or type_concept is None:
            return Response(
                {'detail': 'Required OMOP concepts not available. Run load_athena_vocabularies.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        items = data['measurements']
        loinc_codes = {item.get('loinc_code') for item in items if item.get('loinc_code')}
        loinc_cache = {}
        if loinc_codes:
            loinc_cache = {
                c.concept_code: c
                for c in Concept.objects.filter(vocabulary_id='LOINC', concept_code__in=loinc_codes)
            }
        unit_codes = {item.get('unit') for item in items if item.get('unit')}
        ucum_cache = {}
        if unit_codes:
            ucum_cache = {
                c.concept_code: c.concept_id
                for c in Concept.objects.filter(vocabulary_id='UCUM', concept_code__in=unit_codes)
            }

        hk_concept_cache = self._preload_hk_concepts(items, loinc_cache)
        care_site = self._get_or_create_care_site(data.get('lab_name'))
        visit = self._create_visit_occurrence(
            person_id=person_id,
            care_site=care_site,
            lab_date=data.get('lab_date') or date.today(),
            report_filename=data.get('report_filename'),
            visit_concept=visit_concept,
            type_concept=type_concept,
        )

        # Dedup: check for existing measurements, only create new ones
        all_measurement_ids = []
        new_items = []
        deduplicated_count = 0

        for item in items:
            existing_id = self._find_existing_measurement(
                person_id, item, loinc_cache, hk_concept_cache,
            )
            if existing_id is not None:
                all_measurement_ids.append(existing_id)
                deduplicated_count += 1
            else:
                all_measurement_ids.append(None)
                new_items.append(item)

        new_ids = next_pk_batch(Measurement, 'measurement_id', len(new_items)) if new_items else []
        new_id_iter = iter(new_ids)
        new_objects = []
        for i, item in enumerate(items):
            if all_measurement_ids[i] is not None:
                continue
            m_id = next(new_id_iter)
            all_measurement_ids[i] = m_id
            new_objects.append(self._build_measurement(
                measurement_id=m_id,
                person_id=person_id,
                item=item,
                visit=visit,
                type_concept=type_concept,
                loinc_cache=loinc_cache,
                ucum_cache=ucum_cache,
                hk_concept_cache=hk_concept_cache,
            ))
        if new_objects:
            Measurement.objects.bulk_create(new_objects)

        # Ownership: link all measurements (created + deduped) to this visit
        MeasurementOwnership.objects.bulk_create(
            [
                MeasurementOwnership(
                    measurement_id=m_id,
                    visit_occurrence_id=visit.visit_occurrence_id,
                )
                for m_id in all_measurement_ids
            ],
            ignore_conflicts=True,
        )

        # Provenance
        self._record_provenance(
            actor_identity=actor_identity,
            actor_iss=actor_iss,
            actor_sub=actor_sub,
            target_person_id=person_id,
            is_on_behalf_of=is_on_behalf_of,
            visit=visit,
            measurement_ids=all_measurement_ids,
            org=org,
            source_type=source_type,
        )

        created_count = len(new_objects)
        return Response({
            'visit_occurrence_id': visit.visit_occurrence_id,
            'measurement_ids': all_measurement_ids,
            'count': len(all_measurement_ids),
            'created_count': created_count,
            'deduplicated_count': deduplicated_count,
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
                           measurement_ids, org, source_type):
        """Record provenance for all measurements created in this sync."""
        if is_on_behalf_of:
            source = 'ADMIN_CORRECTION'
        elif source_type == 'patient_self_report':
            source = 'PATIENT_SELF'
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

        from patient_portal.models import Identity
        from patient_portal.services import resolve_or_create_person

        identity, created = Identity.objects.get_or_create(
            issuer=actor_iss, sub=actor_sub,
        )
        if created:
            identity.set_unusable_password()
            identity.save(update_fields=['password'])

        person = resolve_or_create_person(identity)
        return person.person_id

    def _preload_hk_concepts(self, items, loinc_cache):
        """Pre-fetch or create HK-Labs concepts for LOINC-unmatched tests."""
        names_needing_hk = set()
        for item in items:
            loinc_code = item.get('loinc_code')
            if not loinc_code or loinc_code not in loinc_cache:
                names_needing_hk.add(item['test_name'])

        if not names_needing_hk:
            return {}

        slugs = {_normalize_slug(name): name for name in names_needing_hk}
        existing = Concept.objects.filter(
            vocabulary_id=HK_LABS_VOCAB_ID,
            concept_code__in=list(slugs.keys()),
        )
        cache = {}
        for c in existing:
            for slug, name in list(slugs.items()):
                if slug == c.concept_code:
                    cache[name] = c.concept_id

        missing = names_needing_hk - set(cache.keys())
        if missing:
            _ensure_hk_deps('Measurement', 'Lab Test')
            new_ids = next_pk_batch(Concept, 'concept_id', len(missing))
            new_concepts = []
            for concept_id, name in zip(new_ids, missing):
                code = _normalize_slug(name)
                new_concepts.append(Concept(
                    concept_id=concept_id,
                    concept_name=name[:255],
                    domain_id='Measurement',
                    vocabulary_id=HK_LABS_VOCAB_ID,
                    concept_class_id='Lab Test',
                    standard_concept=None,
                    concept_code=code[:50],
                    valid_start_date=date(1970, 1, 1),
                    valid_end_date=date(2099, 12, 31),
                ))
                cache[name] = concept_id
            Concept.objects.bulk_create(new_concepts)

        return cache

    def _get_or_create_care_site(self, lab_name):
        if not lab_name:
            return None
        care_site = CareSite.objects.filter(care_site_name=lab_name).first()
        if care_site:
            return care_site
        cs_id = next_pk(CareSite, 'care_site_id')
        try:
            return CareSite.objects.create(
                care_site_id=cs_id,
                care_site_name=lab_name,
                care_site_source_value=lab_name[:50],
            )
        except Exception:
            return CareSite.objects.filter(care_site_name=lab_name).first()

    def _create_visit_occurrence(self, person_id, care_site, lab_date, report_filename, visit_concept, type_concept):
        source_value = (report_filename or '')[:50]
        care_site_id = care_site.care_site_id if care_site else None
        if source_value:
            # Idempotent path: dedup by (person, date, care_site, report_filename) so that
            # re-commits from hk-labs after a failed sync return the existing visit rather
            # than creating an orphan.
            existing = VisitOccurrence.objects.filter(
                person_id=person_id,
                visit_start_date=lab_date,
                care_site_id=care_site_id,
                visit_source_value=source_value,
            ).first()
            if existing:
                return existing
            # Row absent — allocate PK only now, then create.
            visit_id = next_pk(VisitOccurrence, 'visit_occurrence_id')
            visit, _ = VisitOccurrence.objects.get_or_create(
                person_id=person_id,
                visit_start_date=lab_date,
                care_site_id=care_site_id,
                visit_source_value=source_value,
                defaults={
                    'visit_occurrence_id': visit_id,
                    'visit_concept': visit_concept,
                    'visit_end_date': lab_date,
                    'visit_type_concept': type_concept,
                },
            )
            return visit
        # No report_filename: cannot dedup — each call produces a new VisitOccurrence.
        # Callers that need idempotency must supply a stable report_filename.
        visit_id = next_pk(VisitOccurrence, 'visit_occurrence_id')
        return VisitOccurrence.objects.create(
            visit_occurrence_id=visit_id,
            person_id=person_id,
            visit_concept=visit_concept,
            visit_start_date=lab_date,
            visit_end_date=lab_date,
            visit_type_concept=type_concept,
            care_site_id=care_site_id,
            visit_source_value=source_value,
        )

    def _build_measurement(self, measurement_id, person_id, item, visit, type_concept,
                           loinc_cache, ucum_cache, hk_concept_cache):
        loinc_code = item.get('loinc_code')
        test_name = item['test_name']

        measurement_concept_id = 0
        measurement_source_concept_id = None
        measurement_source_value = test_name[:50]

        if loinc_code:
            concept = loinc_cache.get(loinc_code)
            if concept:
                measurement_concept_id = concept.concept_id
            else:
                measurement_source_concept_id = hk_concept_cache.get(test_name)
        else:
            measurement_source_concept_id = hk_concept_cache.get(test_name)

        unit_concept_id = None
        unit_str = item.get('unit')
        if unit_str:
            unit_concept_id = ucum_cache.get(unit_str)

        return Measurement(
            measurement_id=measurement_id,
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

    _DEDUP_SQL = """
    SELECT measurement_id FROM measurement
    WHERE person_id = %s
      AND measurement_date = %s
      AND (measurement_concept_id = %s OR measurement_source_concept_id = %s)
      AND value_as_number IS NOT DISTINCT FROM %s
      AND value_as_string IS NOT DISTINCT FROM %s
    LIMIT 1
    """

    def _find_existing_measurement(self, person_id, item, loinc_cache, hk_concept_cache):
        """Return measurement_id of an existing duplicate, or None."""
        loinc_code = item.get('loinc_code')
        test_name = item['test_name']

        concept_id = 0
        source_concept_id = None
        if loinc_code:
            concept = loinc_cache.get(loinc_code)
            if concept:
                concept_id = concept.concept_id
            else:
                source_concept_id = hk_concept_cache.get(test_name)
        else:
            source_concept_id = hk_concept_cache.get(test_name)

        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(self._DEDUP_SQL, [
                person_id,
                item['measured_at'],
                concept_id,
                source_concept_id,
                item.get('value'),
                item.get('value_string') or '',
            ])
            row = cur.fetchone()
        return row[0] if row else None
