"""
Identity-resolved FHIR ingest API for the fhir_importers connector → ctomop.

POST /api/fhir/sync/

Mirrors the lab_results sync pattern (actor_iss/actor_sub identity resolution +
ScopedTokenPermission), and ingests the first-cut scope bound to the resolved
Person:

  - Patient        → demographics on the resolved Person (fill-if-empty)
  - Observation    → Measurement (LOINC concept lookup, value/unit/date)
  - Condition      → ConditionOccurrence (SNOMED/ICD lookup, onset date)
  - MedicationStatement → DrugExposure (RxNorm lookup, start date)

Ingest is batched for performance: concepts are preloaded with one query per
code system, ids come from next_pk_batch, and rows are bulk_created — so a full
patient compartment ingests in a couple of seconds, not a long-held request.
Person is resolved from identity, never demographic upsert. Oncology-specific
enrichment is deferred — see fhir_importers issue #10.
"""
import logging
from collections import defaultdict
from datetime import date, datetime

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.authorization import can_access_patient
from omop_core.models import (
    Concept, ConditionOccurrence, DrugExposure, Measurement, Person, ProvenanceRecord,
)
from omop_core.services.pk import next_pk_batch
from patient_portal.api.permissions import ScopedTokenPermission, get_request_org
# Reuse the proven HK-Labs concept-fallback machinery.
from patient_portal.api.lab_results.sync import HK_LABS_VOCAB_ID, _ensure_hk_deps

logger = logging.getLogger(__name__)

EHR_TYPE_CONCEPT_ID = 32817        # "EHR"
NO_MATCHING_CONCEPT_ID = 0         # OMOP "No matching concept"

# FHIR CodeableConcept system URI → OMOP vocabulary_id.
_SYSTEM_VOCAB = {
    'http://loinc.org': 'LOINC',
    'http://snomed.info/sct': 'SNOMED',
    'http://www.nlm.nih.gov/research/umls/rxnorm': 'RxNorm',
    'http://hl7.org/fhir/sid/icd-10-cm': 'ICD10CM',
    'http://hl7.org/fhir/sid/icd-10': 'ICD10CM',
}

_FALLBACK_CONCEPTS = {
    NO_MATCHING_CONCEPT_ID: ('No matching concept', 'Metadata', 'Undefined'),
    EHR_TYPE_CONCEPT_ID: ('EHR', 'Type Concept', 'Type Concept'),
}


def _ensure_concept(concept_id):
    """Return a Concept by id, creating an HK-Labs fallback if vocabularies aren't loaded."""
    concept = Concept.objects.filter(concept_id=concept_id).first()
    if concept:
        return concept
    name, domain_id, concept_class_id = _FALLBACK_CONCEPTS[concept_id]
    _ensure_hk_deps(domain_id, concept_class_id)
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


def _parse_date(value):
    """FHIR date/dateTime → date. Tolerates 'YYYY-MM-DD' and full datetimes."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _codings(codeable):
    return (codeable or {}).get('coding', []) or []


def _source_text(codeable):
    """Human-readable fallback for a CodeableConcept (text or first display/code)."""
    if not codeable:
        return ''
    if codeable.get('text'):
        return codeable['text']
    for coding in _codings(codeable):
        if coding.get('display'):
            return coding['display']
        if coding.get('code'):
            return coding['code']
    return ''


def _norm_num(value):
    """Normalize a measurement value to a comparable form for dedup (float | None)."""
    return float(value) if value is not None else None


class FhirSyncRequestSerializer(serializers.Serializer):
    person_id = serializers.IntegerField(required=False, allow_null=True)
    actor_iss = serializers.CharField(required=False, allow_blank=True, default="")
    actor_sub = serializers.CharField(required=False, allow_blank=True, default="")
    bundle = serializers.JSONField()

    def validate_bundle(self, value):
        if not isinstance(value, dict) or value.get('resourceType') != 'Bundle':
            raise serializers.ValidationError("bundle must be a FHIR Bundle resource.")
        if len(value.get('entry', []) or []) > 1000:
            raise serializers.ValidationError("Maximum 1000 bundle entries per request.")
        return value

    def validate_actor_iss(self, value):
        if '|' in value:
            raise serializers.ValidationError("Pipe character not allowed in actor_iss.")
        return value

    def validate_actor_sub(self, value):
        if '|' in value:
            raise serializers.ValidationError("Pipe character not allowed in actor_sub.")
        return value


class FhirSyncView(APIView):
    """POST /api/fhir/sync/ — ingest a FHIR R4 Bundle for an identity-resolved Person."""

    permission_classes = [ScopedTokenPermission]
    throttle_scope = 'sync'

    @transaction.atomic
    def post(self, request):
        serializer = FhirSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        actor_iss = data.get('actor_iss', '')
        actor_sub = data.get('actor_sub', '')
        bundle = data['bundle']

        resolution = self._resolve_person(request, actor_iss, actor_sub, data.get('person_id'))
        if isinstance(resolution, Response):
            return resolution
        person, org = resolution
        source_user_id = f"{actor_iss}|{actor_sub}" if actor_iss and actor_sub else ''

        ehr_type = _ensure_concept(EHR_TYPE_CONCEPT_ID)
        no_match = _ensure_concept(NO_MATCHING_CONCEPT_ID)

        # Group bundle resources (first-cut scope).
        patient_res = None
        observations, conditions, medications = [], [], []
        for entry in bundle.get('entry', []) or []:
            res = (entry or {}).get('resource', {}) or {}
            rtype = res.get('resourceType')
            if rtype == 'Patient' and patient_res is None:
                patient_res = res
            elif rtype == 'Observation':
                observations.append(res)
            elif rtype == 'Condition':
                conditions.append(res)
            elif rtype == 'MedicationStatement':
                medications.append(res)

        concept_cache = self._preload_concepts(observations, conditions, medications)

        result = {
            'person_id': person.person_id,
            'demographics_updated': bool(patient_res) and self._update_demographics(person, patient_res),
            'measurement_ids': self._ingest_observations(
                person, observations, ehr_type, concept_cache, source_user_id, org),
            'condition_ids': self._ingest_conditions(
                person, conditions, ehr_type, no_match, concept_cache, source_user_id, org),
            'drug_exposure_ids': self._ingest_medications(
                person, medications, ehr_type, no_match, concept_cache, source_user_id, org),
        }

        # NOTE: the denormalized PatientInfo is intentionally NOT rebuilt here.
        # refresh_patient_info is O(N) in the person's data (per-row queries) and
        # would put the synchronous ingest back into "slow request" territory.
        # First-cut display reads the OMOP tables directly; PatientInfo
        # derivation is part of the deferred oncology enrichment (fhir_importers
        # #10) and should run out-of-band, not inside this request.
        return Response(result, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    # Concept resolution (batched)
    # ------------------------------------------------------------------ #
    def _preload_concepts(self, *resource_lists) -> dict:
        """Resolve every coding in the bundle with a few `__in` queries.

        Returns a cache keyed by ('vocab', code) for system-specific matches and
        ('*', code) for cross-vocabulary fallback.
        """
        by_vocab = defaultdict(set)
        all_codes = set()

        def collect(codeable):
            for coding in _codings(codeable):
                code = coding.get('code')
                if not code:
                    continue
                all_codes.add(code)
                vocab = _SYSTEM_VOCAB.get(coding.get('system', ''))
                if vocab:
                    by_vocab[vocab].add(code)

        for resources in resource_lists:
            for res in resources:
                collect(res.get('code'))
                collect(res.get('medicationCodeableConcept'))

        cache: dict = {}
        for vocab, codes in by_vocab.items():
            for c in Concept.objects.filter(vocabulary_id=vocab, concept_code__in=list(codes)):
                cache[(vocab, c.concept_code)] = c
        if all_codes:
            for c in Concept.objects.filter(concept_code__in=list(all_codes)):
                cache.setdefault(('*', c.concept_code), c)
        return cache

    def _lookup(self, codeable, cache):
        for coding in _codings(codeable):
            code = coding.get('code')
            if not code:
                continue
            vocab = _SYSTEM_VOCAB.get(coding.get('system', ''))
            concept = (cache.get((vocab, code)) if vocab else None) or cache.get(('*', code))
            if concept:
                return concept
        return None

    # ------------------------------------------------------------------ #
    # Batched ingestion
    # ------------------------------------------------------------------ #
    def _ingest_observations(self, person, observations, ehr_type, cache, source_user_id, org):
        existing = {
            (d, cid, sv, _norm_num(v))
            for d, cid, sv, v in Measurement.objects.filter(person=person).values_list(
                'measurement_date', 'measurement_concept_id', 'measurement_source_value',
                'value_as_number')
        }
        seen = set(existing)
        rows = []
        for obs in observations:
            obs_date = _parse_date(obs.get('effectiveDateTime')) or _parse_date(
                (obs.get('effectivePeriod') or {}).get('start'))
            if obs_date is None:
                continue
            concept = self._lookup(obs.get('code'), cache)
            cid = concept.concept_id if concept else 0
            sv = _source_text(obs.get('code'))[:50]
            qty = obs.get('valueQuantity') or {}
            value_number = qty.get('value')
            unit = qty.get('unit') or qty.get('code')
            value_string = obs.get('valueString') or _source_text(obs.get('valueCodeableConcept'))

            key = (obs_date, cid, sv, _norm_num(value_number))
            if key in seen:
                continue
            seen.add(key)
            rows.append(Measurement(
                person=person,
                measurement_concept_id=cid,
                measurement_date=obs_date,
                measurement_type_concept=ehr_type,
                value_as_number=value_number,
                value_as_string=(value_string or '')[:60],
                measurement_source_value=sv,
                unit_source_value=(unit or '')[:50],
            ))
        return self._bulk_insert(Measurement, 'measurement_id', rows, source_user_id, person, org)

    def _ingest_conditions(self, person, conditions, ehr_type, no_match, cache, source_user_id, org):
        existing = set(ConditionOccurrence.objects.filter(person=person).values_list(
            'condition_concept_id', 'condition_start_date', 'condition_source_value'))
        seen = set(existing)
        rows = []
        for cond in conditions:
            start = _parse_date(cond.get('onsetDateTime')) or _parse_date(
                (cond.get('onsetPeriod') or {}).get('start'))
            if start is None:
                continue
            concept = self._lookup(cond.get('code'), cache)
            cid = concept.concept_id if concept else NO_MATCHING_CONCEPT_ID
            sv = _source_text(cond.get('code'))[:50]
            key = (cid, start, sv)
            if key in seen:
                continue
            seen.add(key)
            rows.append(ConditionOccurrence(
                person=person,
                condition_concept=concept or no_match,
                condition_start_date=start,
                condition_type_concept=ehr_type,
                condition_source_value=sv,
            ))
        return self._bulk_insert(
            ConditionOccurrence, 'condition_occurrence_id', rows, source_user_id, person, org)

    def _ingest_medications(self, person, medications, ehr_type, no_match, cache, source_user_id, org):
        existing = set(DrugExposure.objects.filter(person=person).values_list(
            'drug_concept_id', 'drug_exposure_start_date', 'drug_source_value'))
        seen = set(existing)
        rows = []
        for med in medications:
            start = _parse_date((med.get('effectivePeriod') or {}).get('start')) or _parse_date(
                med.get('effectiveDateTime'))
            if start is None:
                continue
            codeable = med.get('medicationCodeableConcept')
            concept = self._lookup(codeable, cache)
            cid = concept.concept_id if concept else NO_MATCHING_CONCEPT_ID
            sv = _source_text(codeable)[:50]
            key = (cid, start, sv)
            if key in seen:
                continue
            seen.add(key)
            rows.append(DrugExposure(
                person=person,
                drug_concept=concept or no_match,
                drug_exposure_start_date=start,
                drug_exposure_end_date=_parse_date((med.get('effectivePeriod') or {}).get('end')),
                drug_type_concept=ehr_type,
                drug_source_value=sv,
            ))
        return self._bulk_insert(
            DrugExposure, 'drug_exposure_id', rows, source_user_id, person, org)

    def _bulk_insert(self, model, pk_field, rows, source_user_id, person, org):
        """Assign batched PKs, bulk_create, and record EHR_SYNC provenance."""
        if not rows:
            return []
        ids = next_pk_batch(model, pk_field, len(rows))
        for row, pk in zip(rows, ids):
            setattr(row, pk_field, pk)
        model.objects.bulk_create(rows)

        ct = ContentType.objects.get_for_model(model)
        ProvenanceRecord.objects.bulk_create([
            ProvenanceRecord(
                source='EHR_SYNC',
                source_user_id=source_user_id,
                target_patient_id=str(person.person_id),
                organization=org,
                content_type=ct,
                object_id=pk,
            )
            for pk in ids
        ])
        return list(ids)

    # ------------------------------------------------------------------ #
    # Demographics + person resolution (mirrors lab_results.sync)
    # ------------------------------------------------------------------ #
    def _update_demographics(self, person, patient_res):
        """Fill empty demographic fields from the Patient resource (never clobber
        real data). resolve_or_create_person seeds placeholders (year_of_birth=1900,
        *_source_value='unknown') on auto-provision — treat those as unset."""
        changed = []
        name = (patient_res.get('name') or [{}])[0]
        given = ' '.join(name.get('given', []) or []) if name.get('given') else ''
        family = name.get('family', '') or ''
        if given and not person.given_name:
            person.given_name = given[:50]; changed.append('given_name')
        if family and not person.family_name:
            person.family_name = family[:50]; changed.append('family_name')

        bd = _parse_date(patient_res.get('birthDate'))
        if bd and person.year_of_birth in (None, 1900):
            person.year_of_birth = bd.year
            person.month_of_birth = bd.month
            person.day_of_birth = bd.day
            changed += ['year_of_birth', 'month_of_birth', 'day_of_birth']

        gender = patient_res.get('gender')
        if gender and person.gender_source_value in (None, '', 'unknown'):
            person.gender_source_value = gender[:50]; changed.append('gender_source_value')

        if changed:
            person.save(update_fields=changed)
        return bool(changed)

    def _resolve_person(self, request, actor_iss, actor_sub, person_id):
        is_on_behalf_of = bool(person_id)

        if not person_id:
            if hasattr(request.user, 'issuer') and request.user.issuer != 'urn:service':
                from patient_portal.services import resolve_or_create_person
                person_id = resolve_or_create_person(request.user).person_id
            else:
                person_id = self._resolve_person_from_identity(actor_iss, actor_sub)
            if person_id is None:
                return Response({'detail': 'Cannot resolve person from actor identity.'},
                                status=status.HTTP_400_BAD_REQUEST)

        person = Person.objects.filter(person_id=person_id).first()
        if person is None:
            return Response({'detail': 'Person not found.'}, status=status.HTTP_404_NOT_FOUND)

        actor_identity = self._resolve_actor_identity(actor_iss, actor_sub, request.user)
        has_explicit_actor = bool(actor_iss and actor_sub)

        if is_on_behalf_of:
            if has_explicit_actor and actor_identity is None:
                return Response({'detail': 'Actor identity not found.'},
                                status=status.HTTP_403_FORBIDDEN)
            if has_explicit_actor and actor_identity:
                if not can_access_patient(actor_identity, person_id):
                    return Response({'detail': 'Actor does not have access to this patient.'},
                                    status=status.HTTP_403_FORBIDDEN)
            elif not has_explicit_actor and not getattr(request.user, 'is_superuser', False):
                return Response(
                    {'detail': 'actor_iss and actor_sub required when writing on behalf of another person.'},
                    status=status.HTTP_400_BAD_REQUEST)

        org = get_request_org(request)
        if org is not None:
            from omop_core.models import PatientInfo
            if not PatientInfo.objects.filter(person_id=person_id, organization=org).exists():
                return Response({'detail': 'Person not in your organization.'},
                                status=status.HTTP_403_FORBIDDEN)

        return person, org

    def _resolve_actor_identity(self, actor_iss, actor_sub, request_user):
        if actor_iss and actor_sub:
            from patient_portal.models import Identity
            return Identity.objects.filter(issuer=actor_iss, sub=actor_sub).first()
        if request_user and request_user.is_authenticated:
            return request_user
        return None

    def _resolve_person_from_identity(self, actor_iss, actor_sub):
        if not actor_iss or not actor_sub:
            return None
        from patient_portal.models import Identity
        from patient_portal.services import resolve_or_create_person
        identity, created = Identity.objects.get_or_create(issuer=actor_iss, sub=actor_sub)
        if created:
            identity.set_unusable_password()
            identity.save(update_fields=['password'])
        return resolve_or_create_person(identity).person_id
