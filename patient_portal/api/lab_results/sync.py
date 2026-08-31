"""
Dedicated sync API for writing lab results into PRomop.

POST /api/lab-results/sync/

Accepts a batch of measurements from hk-labs commit step.
Handles:
  - LOINC concept lookup
  - UCUM unit mapping
  - governed resolution of LOINC-unmatched tests (services.code_mapping),
    which mints under HK-Labs *and* files the code in the review queue
  - CareSite get_or_create (if lab_name provided)
  - VisitOccurrence creation (one per upload/commit)
Returns: created measurement_ids + visit_occurrence_id
"""
from datetime import date

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.authorization import can_write_patient
from omop_core.models import (
    CareSite, Concept, Measurement, MeasurementOwnership,
    Person, ProvenanceRecord, VisitOccurrence,
)
from omop_core.services.code_mapping import resolve_source_code
from omop_core.services.pk import next_pk, next_pk_batch
from patient_portal.api.permissions import LabSyncPermission, get_request_org, is_service_token

logger = logging.getLogger(__name__)

HK_LABS_VOCAB_ID = 'HK-Labs'
HK_LABS_CONCEPT_ID_START = 2000000000
PATIENT_SELF_REPORT_CONCEPT_ID = 32865
DOCUMENT_EXTRACTION_CONCEPT_ID = 32883
OUTPATIENT_VISIT_CONCEPT_ID = 9202


ORIGIN_SYSTEM = 'hk-labs'


def _origin_system(match_method):
    """The proposal's origin_system, carrying hk-labs' own match tier.

    hk-labs classifies every test it sends -- `loinc`, `alias_exact`,
    `name_fallback`, `manual`, `unmatched` -- and that tier is the most useful
    thing a curator can know about a queued code: `unmatched` is hk-labs saying
    "a human has to look at this", while `name_fallback` is a guess it already
    made and wants confirmed.

    It rides in origin_system rather than notes because notes is curator prose
    -- an import writing there would eventually overwrite a human's -- and
    because the Code Mapping UI already renders origin_system beside the origin
    ("proposed by import (hk-labs:unmatched)"). Nothing filters on an exact
    'hk-labs', so qualifying it costs nothing.
    """
    method = (match_method or '').strip()
    return f'{ORIGIN_SYSTEM}:{method}'[:50] if method else ORIGIN_SYSTEM


def _resolution_key(item):
    """The (source_vocabulary_id, source_code) a test is curated under.

    Only ever consulted for a test whose LOINC code did *not* resolve, so a
    LOINC code still present here is one this deploy has no concept for. That
    is the gap worth reporting -- rule 1 in the resolver files it for review
    and pointedly does not mint, since an HK concept shadowing a real LOINC one
    is what remap_shadow_concepts exists to undo.

    Otherwise prefer a lab-native code when the hk-labs parser found one: a
    code is stable across reports while the printed test name is not. Failing
    that the name is the only identity the test has, normalized when hk-labs
    sent a normalized form so one queue row covers every spelling of it.

    Never an HK-* source vocabulary: those are minting destinations, and
    SourceCodeConceptMapping.clean() rejects them outright.
    """
    loinc_code = (item.get('loinc_code') or '').strip()
    if loinc_code:
        return 'LOINC', loinc_code
    code = (item.get('source_code') or '').strip()
    if code:
        vocabulary_id = (item.get('source_code_system') or '').strip()
        if vocabulary_id.upper().startswith('HK-'):
            vocabulary_id = ''
        return vocabulary_id, code
    name = (item.get('test_name_normalized') or '').strip() or item['test_name'].strip()
    return '', name


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
        source='HealthKey',
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
    # How hk-labs itself resolved the test ('loinc', 'alias_exact',
    # 'name_fallback', 'manual', 'unmatched'). Recorded on the proposal a
    # LOINC-unmatched test raises, so a curator sees which tier produced it.
    match_method = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    # The test's own identity, as hk-labs parsed it: a lab-native code where
    # the report carried one, and a normalized form of the printed name. Both
    # feed _resolution_key.
    test_name_normalized = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    source_code = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    source_code_system = serializers.CharField(required=False, allow_null=True, allow_blank=True)


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
    permission_classes = [LabSyncPermission]
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

        # For regular end-user callers, attribution is always the authenticated
        # user: ignore any actor identity supplied in the request body. Every lab
        # result stays tied to a real user, and a patient cannot impersonate
        # another actor. Only trusted service tokens supply the actor explicitly
        # for server-to-server on-behalf-of writes.
        is_service = is_service_token(request)
        is_privileged = is_service
        if not is_privileged and getattr(request.user, 'is_authenticated', False):
            actor_iss = getattr(request.user, 'issuer', '') or ''
            actor_sub = getattr(request.user, 'sub', '') or ''
        elif not is_privileged:
            actor_iss = actor_sub = ''

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
        org = get_request_org(request)

        if is_on_behalf_of:
            if is_service:
                if has_explicit_actor and actor_identity is None:
                    return Response(
                        {'detail': 'Actor identity not found.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if has_explicit_actor and not can_write_patient(actor_identity, person_id):
                    return Response(
                        {'detail': 'Actor does not have write access to this patient.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            elif org is None:
                if not has_explicit_actor:
                    return Response(
                        {'detail': 'actor_iss and actor_sub required when writing on behalf of another person.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not can_write_patient(actor_identity, person_id):
                    return Response(
                        {'detail': 'Actor does not have write access to this patient.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )

        # Org-scope enforcement for OAuth2 service clients
        if org is not None:
            from omop_core.models import PatientRecord
            if not PatientRecord.objects.filter(person_id=person_id, organization=org).exists():
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

        concept_cache = self._resolve_unmatched_concepts(items, loinc_cache)
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
                person_id, item, loinc_cache, concept_cache,
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
                concept_cache=concept_cache,
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
        ProvenanceRecord.objects.bulk_create(records, ignore_conflicts=True)

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

    def _resolve_unmatched_concepts(self, items, loinc_cache):
        """Resolve every LOINC-unmatched test through the governed resolver.

        A test whose LOINC code resolves is already answered -- the code *is*
        the concept (rule 1 in services.code_mapping), so it needs no mapping
        and gets none. Everything else goes through ``resolve_source_code``,
        which honours an approved mapping, else mints under HK-Labs and files a
        *proposed* mapping beside it. Minting inline here instead, as this
        method used to, meant hk-labs' `unmatched` tests were quietly invented
        and never reached the Code Mapping queue -- hk-labs#50.

        Cost stays flat in the number of measurements: one resolution per
        distinct (source vocabulary, source code), cached for the rest of the
        request. That also keeps ``occurrence_count`` honest -- a proposal is
        bumped once per sync, not once per row of a repeated test.

        Returns ``{resolution key: Concept or None}``.
        """
        cache = {}
        for item in items:
            loinc_code = item.get('loinc_code')
            if loinc_code and loinc_code in loinc_cache:
                continue
            key = _resolution_key(item)
            if key in cache:
                continue
            source_vocabulary_id, source_code = key
            concept, _mapping = resolve_source_code(
                source_code=source_code,
                source_vocabulary_id=source_vocabulary_id,
                # The display text, which is what ingest writes into
                # measurement_source_value below. It becomes the proposal's
                # description, and _source_value_match matches stored rows on
                # the code *or* the description -- so an approval re-points
                # these rows even when the key is a lab-native code the
                # measurement itself never carried.
                source_text=item['test_name'],
                omop_table='measurement',
                source_system=_origin_system(item.get('match_method')),
            )
            cache[key] = concept
        return cache

    @staticmethod
    def _concept_ids(item, loinc_cache, concept_cache):
        """(measurement_concept_id, measurement_source_concept_id) for one item.

        A resolved LOINC code lands in measurement_concept_id, as before. A
        resolved-or-minted concept lands there too: that is the column
        ``repoint_clinical_rows`` rewrites when a curator approves the
        proposal, so a minted concept parked only in the source column would
        leave these rows stranded at the invented concept forever. The mint is
        *also* written to measurement_source_concept_id -- it genuinely is the
        concept for the source test, and rows written before this change carry
        it there and have to keep deduping.

        (0, None) means there was nothing to resolve to: a LOINC code whose
        concept this deploy has not loaded. The resolver records that gap
        without minting, because an HK concept shadowing a real LOINC one is
        exactly what remap_shadow_concepts exists to undo.
        """
        loinc_code = item.get('loinc_code')
        if loinc_code:
            concept = loinc_cache.get(loinc_code)
            if concept:
                return concept.concept_id, None
        concept = concept_cache.get(_resolution_key(item))
        if concept is None:
            return 0, None
        source_concept_id = (
            concept.concept_id
            if (concept.vocabulary_id or '').startswith('HK-')
            else None
        )
        return concept.concept_id, source_concept_id

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
        source_value = (report_filename or '')[:255]
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
                           loinc_cache, ucum_cache, concept_cache):
        measurement_source_value = item['test_name'][:50]
        measurement_concept_id, measurement_source_concept_id = self._concept_ids(
            item, loinc_cache, concept_cache,
        )

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

    # An unresolved row sits at concept 0 with no source concept, so the
    # concept clause above stops telling rows apart -- two different tests from
    # one draw reporting the same value would look like one row, and the second
    # would be dropped as a duplicate. Their test name is the only identity
    # they have left.
    _DEDUP_UNRESOLVED_SQL = """
    SELECT measurement_id FROM measurement
    WHERE person_id = %s
      AND measurement_date = %s
      AND measurement_concept_id = 0
      AND measurement_source_concept_id IS NULL
      AND measurement_source_value = %s
      AND value_as_number IS NOT DISTINCT FROM %s
      AND value_as_string IS NOT DISTINCT FROM %s
    LIMIT 1
    """

    def _find_existing_measurement(self, person_id, item, loinc_cache, concept_cache):
        """Return measurement_id of an existing duplicate, or None."""
        concept_id, source_concept_id = self._concept_ids(
            item, loinc_cache, concept_cache,
        )

        if concept_id == 0 and source_concept_id is None:
            sql = self._DEDUP_UNRESOLVED_SQL
            params = [person_id, item['measured_at'], item['test_name'][:50]]
        else:
            sql = self._DEDUP_SQL
            params = [person_id, item['measured_at'], concept_id, source_concept_id]
        params += [item.get('value'), item.get('value_string') or '']

        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return row[0] if row else None
