"""
Identity-resolved FHIR ingest API for patient self-service and EHR connector uploads.

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
import json
import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.authorization import can_write_patient
from omop_core.models import (
    Concept, ConditionOccurrence, DrugExposure, Measurement, Observation, Person,
    PatientDocument, ProcedureOccurrence, ProvenanceRecord,
)
from omop_core.services.pk import next_pk_batch
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.permissions import ScopedTokenPermission, get_request_org, is_service_token
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
    'http://hl7.org/fhir/sid/cvx': 'CVX',
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
        source='HealthKey',
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


def _parse_datetime(value):
    """FHIR dateTime → aware datetime, or None for date-only values.

    Preserves sub-daily timestamps (e.g. per-reading heart rate) so they land in
    Measurement.measurement_datetime instead of being truncated to a date.
    """
    if not isinstance(value, str) or len(value) <= 10:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        from django.utils import timezone as _tz
        dt = _tz.make_aware(dt, _tz.utc)
    return dt


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


def _parse_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _reference_range_bound(obs, bound):
    ranges = obs.get('referenceRange') or []
    if not isinstance(ranges, list):
        return None
    for ref_range in ranges:
        if not isinstance(ref_range, dict):
            continue
        quantity = ref_range.get(bound) or {}
        value = quantity.get('value')
        parsed = _parse_decimal(value)
        if parsed is not None:
            return parsed
    return None


def _first_codeable(items):
    if isinstance(items, dict):
        return items
    for item in items or []:
        if isinstance(item, dict):
            return item
    return None


# Observation.extension marker the phr-mobile-bridge sets on daily aggregates
# (steps, active energy, daily HR avg, …). Such rows are upserted by
# (person, concept, date) so a changed daily value replaces the prior row
# instead of stacking — while unmarked clinical readings keep value-level dedup.
AGGREGATION_EXT_URL = 'https://healthkey.ai/fhir/aggregation'


def _is_daily_rollup(obs):
    for ext in obs.get('extension') or []:
        if ext.get('url') == AGGREGATION_EXT_URL and ext.get('valueCode') == 'daily':
            return True
    return False


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
    # Provenance recorded for every row this view writes.
    provenance_source = 'EHR_SYNC'
    # When True (patient self-service), ignore person_id/actor_* and resolve the
    # Person from the authenticated identity — a patient can only write their own.
    self_service_only = False

    @transaction.atomic
    def post(self, request):
        # Bounded multi-version interchange (TI.5.2#01): decline non-R4 requests.
        from patient_portal.api.fhir.integrity import (
            check_fhir_version, verify_content_digest,
        )
        version_error = check_fhir_version(request)
        if version_error:
            return Response({'detail': version_error},
                            status=status.HTTP_406_NOT_ACCEPTABLE)

        # Content integrity (S.3.6#10 / PH.2.3#09): opt-in digest of the raw
        # request body. No integrity header → unchanged behavior. Guarded so a
        # consumed request stream can never break existing (no-header) clients.
        try:
            raw_body = request.body
        except Exception:
            raw_body = None
        if raw_body is not None:
            digest_error = verify_content_digest(request, raw_body)
            if digest_error:
                return Response({'detail': digest_error},
                                status=status.HTTP_400_BAD_REQUEST)

        serializer = FhirSyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if self.self_service_only:
            actor_iss = actor_sub = ''
            person_id = None
            source_user_id = f"{getattr(request.user, 'issuer', '')}|{getattr(request.user, 'sub', '')}"
        else:
            actor_iss = data.get('actor_iss', '')
            actor_sub = data.get('actor_sub', '')
            person_id = data.get('person_id')
            if not is_service_token(request):
                if getattr(request.user, 'is_authenticated', False):
                    actor_iss = getattr(request.user, 'issuer', '') or ''
                    actor_sub = getattr(request.user, 'sub', '') or ''
                else:
                    actor_iss = actor_sub = ''
            source_user_id = f"{actor_iss}|{actor_sub}" if actor_iss and actor_sub else ''
        bundle = data['bundle']

        resolution = self._resolve_person(request, actor_iss, actor_sub, person_id)
        if isinstance(resolution, Response):
            return resolution
        person, org = resolution

        ehr_type = _ensure_concept(EHR_TYPE_CONCEPT_ID)
        no_match = _ensure_concept(NO_MATCHING_CONCEPT_ID)

        # Group bundle resources.
        patient_res = None
        skipped = defaultdict(int)
        observations, conditions, medications = [], [], []
        allergies, immunizations, procedures, diagnostic_reports = [], [], [], []
        document_references = []
        for entry in bundle.get('entry', []) or []:
            res = (entry or {}).get('resource', {}) or {}
            rtype = res.get('resourceType')
            if rtype == 'Patient' and patient_res is None:
                patient_res = res
            elif rtype == 'Observation':
                observations.append(res)
            elif rtype == 'Condition':
                conditions.append(res)
            elif rtype in ('MedicationStatement', 'MedicationRequest'):
                # Epic R4 exposes meds as MedicationRequest; both carry
                # medicationCodeableConcept. Handled uniformly below.
                medications.append(res)
            elif rtype == 'AllergyIntolerance':
                allergies.append(res)
            elif rtype == 'Immunization':
                immunizations.append(res)
            elif rtype == 'Procedure':
                procedures.append(res)
            elif rtype == 'DiagnosticReport':
                diagnostic_reports.append(res)
            elif rtype == 'DocumentReference':
                document_references.append(res)
            else:
                self._count_skipped(skipped, rtype or 'Unknown',
                                    'unsupported_resource_type')

        concept_cache = self._preload_concepts(
            observations, conditions, medications, allergies, immunizations,
            procedures, diagnostic_reports)

        result = {
            'person_id': person.person_id,
            'demographics_updated': bool(patient_res) and self._update_demographics(person, patient_res),
            'measurement_ids': self._ingest_observations(
                person, observations, ehr_type, concept_cache, source_user_id, org,
                skipped),
            'condition_ids': self._ingest_conditions(
                person, conditions, ehr_type, no_match, concept_cache, source_user_id, org,
                skipped),
            'drug_exposure_ids': self._ingest_medications(
                person, medications, ehr_type, no_match, concept_cache, source_user_id, org,
                skipped),
            'procedure_ids': self._ingest_procedures(
                person, procedures, ehr_type, no_match, concept_cache, source_user_id, org,
                skipped),
            'immunization_ids': self._ingest_immunizations(
                person, immunizations, ehr_type, no_match, concept_cache, source_user_id, org,
                skipped),
            'observation_ids': self._ingest_clinical_observations(
                person, allergies, diagnostic_reports, ehr_type, no_match, concept_cache, source_user_id, org,
                skipped),
            'document_ids': self._ingest_document_references(
                person, document_references, source_user_id, org, skipped),
        }
        result['skipped'] = self._skipped_summary(skipped)

        # The person's CURRENT record totals after this ingest — the accurate
        # "records on file" the connector displays (immune to re-sync dedup or
        # out-of-band data changes; the last chunk's value is the final count).
        result['totals'] = {
            'measurements': Measurement.objects.filter(person=person).count(),
            'conditions': ConditionOccurrence.objects.filter(person=person).count(),
            'medications': DrugExposure.objects.filter(person=person).count(),
            'procedures': ProcedureOccurrence.objects.filter(person=person).count(),
            'observations': Observation.objects.filter(person=person).count(),
            'documents': PatientDocument.objects.filter(person=person).count(),
        }

        # NOTE: the denormalized PatientRecord is intentionally NOT rebuilt here.
        # refresh_patient_record is O(N) in the person's data (per-row queries) and
        # would put the synchronous ingest back into "slow request" territory.
        # First-cut display reads the OMOP tables directly; PatientRecord
        # derivation is part of the deferred oncology enrichment (fhir_importers
        # #10) and should run out-of-band, not inside this request.
        return Response(result, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    # Concept resolution (batched)
    # ------------------------------------------------------------------ #
    def _count_skipped(self, skipped, resource_type, reason):
        skipped[(resource_type or 'Unknown', reason)] += 1

    def _skipped_summary(self, skipped):
        return [
            {'resourceType': resource_type, 'reason': reason, 'count': count}
            for (resource_type, reason), count in sorted(skipped.items())
        ]

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
                collect(res.get('vaccineCode'))
                interpretation = res.get('interpretation')
                if isinstance(interpretation, dict):
                    collect(interpretation)
                elif isinstance(interpretation, list):
                    for item in interpretation or []:
                        collect(item)

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
    def _ingest_observations(self, person, observations, ehr_type, cache, source_user_id, org, skipped):
        # Daily rollups (steps/energy/daily-avg) upsert by (person, concept, date);
        # everything else dedups on (date, datetime, concept, source, value).
        rollups = [o for o in observations if _is_daily_rollup(o)]
        discrete = [o for o in observations if not _is_daily_rollup(o)]
        ids = self._insert_discrete_observations(
            person, discrete, ehr_type, cache, source_user_id, org, skipped)
        if rollups:
            ids += self._upsert_rollup_observations(
                person, rollups, ehr_type, cache, source_user_id, org, skipped)
        return ids

    def _parse_obs(self, obs, cache):
        """Pull the fields one Observation maps onto a Measurement."""
        effective = obs.get('effectiveDateTime') or (obs.get('effectivePeriod') or {}).get('start')
        concept = self._lookup(obs.get('code'), cache)
        interpretation = _first_codeable(obs.get('interpretation'))
        interpretation_concept = self._lookup(interpretation, cache)
        qty = obs.get('valueQuantity') or {}
        return {
            'date': _parse_date(effective),
            'dt': _parse_datetime(effective),
            'cid': concept.concept_id if concept else 0,
            'vcid': interpretation_concept.concept_id if interpretation_concept else None,
            'sv': _source_text(obs.get('code'))[:50],
            'value': qty.get('value'),
            'unit': (qty.get('unit') or qty.get('code') or '')[:50],
            'vstr': (obs.get('valueString') or _source_text(obs.get('valueCodeableConcept')) or '')[:60],
            'value_source': _source_text(interpretation)[:50],
            'range_low': _reference_range_bound(obs, 'low'),
            'range_high': _reference_range_bound(obs, 'high'),
        }

    def _insert_discrete_observations(self, person, observations, ehr_type, cache, source_user_id, org, skipped):
        # Mapped rows dedup by resolved concept, not producer display text. If
        # no concept resolved, keep source text in the key so unrelated local
        # metrics do not collapse into one concept_id=0 row.
        def identity(date_value, datetime_value, concept_id, source_value, value):
            source_key = source_value if not concept_id else None
            return (date_value, datetime_value, concept_id, source_key,
                    _norm_num(value))
        existing = {
            identity(d, dt, cid, sv, v)
            for d, dt, cid, sv, v in Measurement.objects.filter(person=person).values_list(
                'measurement_date', 'measurement_datetime', 'measurement_concept_id',
                'measurement_source_value', 'value_as_number')
        }
        seen = set(existing)
        rows = []
        for obs in observations:
            o = self._parse_obs(obs, cache)
            if o['date'] is None:
                self._count_skipped(skipped, 'Observation', 'missing_effective_date')
                continue
            key = identity(o['date'], o['dt'], o['cid'], o['sv'], o['value'])
            if key in seen:
                continue
            seen.add(key)
            rows.append(Measurement(
                person=person,
                measurement_concept_id=o['cid'],
                measurement_date=o['date'],
                measurement_datetime=o['dt'],
                measurement_type_concept=ehr_type,
                value_as_number=o['value'],
                value_as_string=o['vstr'],
                value_as_concept_id=o['vcid'],
                measurement_source_value=o['sv'],
                unit_source_value=o['unit'],
                value_source_value=o['value_source'],
                range_low=o['range_low'],
                range_high=o['range_high'],
            ))
        return self._bulk_insert(Measurement, 'measurement_id', rows, source_user_id, person, org)

    def _upsert_rollup_observations(self, person, observations, ehr_type, cache, source_user_id, org, skipped):
        """Replace any prior row for (person, concept, date) with the new daily
        value, collapsing stale stacked rows — so a changed daily aggregate
        updates in place instead of accumulating duplicates."""
        desired = {}  # key -> parsed obs; last in the bundle wins
        for obs in observations:
            o = self._parse_obs(obs, cache)
            if o['date'] is None:
                self._count_skipped(skipped, 'Observation', 'missing_effective_date')
                continue
            # Unmapped rows all share concept_id 0, so a plain (cid, date) key
            # collapses every distinct unmapped metric into one slot per day.
            # Add source_value to the key for cid == 0 so they coexist; mapped
            # rows keep the natural (concept, date) grain.
            sv_key = o['sv'] if not o['cid'] else None
            desired[(o['cid'], o['date'], sv_key)] = o
        if not desired:
            return []

        meas_ct = ContentType.objects.get_for_model(Measurement)
        touched, new_rows = [], []
        with suppress_patient_record_refresh():
            for (cid, obs_date, sv_key), o in desired.items():
                existing_qs = Measurement.objects.filter(
                    person=person, measurement_concept_id=cid, measurement_date=obs_date,
                )
                if sv_key is not None:
                    existing_qs = existing_qs.filter(measurement_source_value=sv_key)
                existing = list(existing_qs.order_by('measurement_id'))
                if not existing:
                    new_rows.append(Measurement(
                        person=person,
                        measurement_concept_id=cid,
                        measurement_date=obs_date,
                        measurement_datetime=o['dt'],
                        measurement_type_concept=ehr_type,
                        value_as_number=o['value'],
                        value_as_string=o['vstr'],
                        value_as_concept_id=o['vcid'],
                        measurement_source_value=o['sv'],
                        unit_source_value=o['unit'],
                        value_source_value=o['value_source'],
                        range_low=o['range_low'],
                        range_high=o['range_high'],
                    ))
                    continue

                keep, extras = existing[0], existing[1:]
                if extras:  # collapse historical stacked rows for this concept/day
                    extra_ids = [m.measurement_id for m in extras]
                    ProvenanceRecord.objects.filter(
                        content_type=meas_ct, object_id__in=extra_ids).delete()
                    Measurement.objects.filter(measurement_id__in=extra_ids).delete()

                changed = (
                    _norm_num(keep.value_as_number) != _norm_num(o['value'])
                    or keep.measurement_datetime != o['dt']
                    or keep.value_as_string != o['vstr']
                    or keep.value_as_concept_id != o['vcid']
                    or keep.unit_source_value != o['unit']
                    or keep.value_source_value != o['value_source']
                    or keep.range_low != o['range_low']
                    or keep.range_high != o['range_high']
                )
                if changed:
                    keep.value_as_number = o['value']
                    keep.measurement_datetime = o['dt']
                    keep.value_as_string = o['vstr']
                    keep.value_as_concept_id = o['vcid']
                    keep.unit_source_value = o['unit']
                    keep.value_source_value = o['value_source']
                    keep.range_low = o['range_low']
                    keep.range_high = o['range_high']
                    keep._skip_patient_record_refresh = True
                    keep.save(update_fields=['value_as_number', 'measurement_datetime',
                                             'value_as_string', 'value_as_concept',
                                             'unit_source_value', 'value_source_value',
                                             'range_low', 'range_high'])
                if changed or extras:
                    touched.append(keep.measurement_id)
            inserted = self._bulk_insert(
                Measurement, 'measurement_id', new_rows, source_user_id, person, org)
        return touched + inserted

    def _ingest_conditions(self, person, conditions, ehr_type, no_match, cache, source_user_id, org, skipped):
        rows = []
        for cond in conditions:
            start = _parse_date(cond.get('onsetDateTime')) or _parse_date(
                (cond.get('onsetPeriod') or {}).get('start'))
            if start is None:
                self._count_skipped(skipped, 'Condition', 'missing_onset_date')
                continue
            concept = self._lookup(cond.get('code'), cache)
            rows.append(ConditionOccurrence(
                person=person,
                condition_concept=concept or no_match,
                condition_start_date=start,
                condition_type_concept=ehr_type,
                condition_source_value=_source_text(cond.get('code'))[:50],
            ))
        return self._upsert_clinical(
            ConditionOccurrence, 'condition_occurrence_id', 'condition_concept_id',
            'condition_start_date', 'condition_source_value', person, rows, source_user_id, org)

    def _ingest_medications(self, person, medications, ehr_type, no_match, cache, source_user_id, org, skipped):
        rows = []
        for med in medications:
            # MedicationStatement: effectivePeriod/effectiveDateTime.
            # MedicationRequest (Epic R4): authoredOn.
            start = (
                _parse_date((med.get('effectivePeriod') or {}).get('start'))
                or _parse_date(med.get('effectiveDateTime'))
                or _parse_date(med.get('authoredOn'))
            )
            if start is None:
                self._count_skipped(skipped, med.get('resourceType') or 'MedicationStatement',
                                    'missing_effective_or_authored_date')
                continue
            codeable = med.get('medicationCodeableConcept')
            concept = self._lookup(codeable, cache)
            rows.append(DrugExposure(
                person=person,
                drug_concept=concept or no_match,
                drug_exposure_start_date=start,
                drug_exposure_end_date=_parse_date((med.get('effectivePeriod') or {}).get('end')),
                drug_type_concept=ehr_type,
                drug_source_value=_source_text(codeable)[:50],
            ))
        return self._upsert_clinical(
            DrugExposure, 'drug_exposure_id', 'drug_concept_id',
            'drug_exposure_start_date', 'drug_source_value', person, rows, source_user_id, org)

    def _ingest_procedures(self, person, procedures, ehr_type, no_match, cache, source_user_id, org, skipped):
        rows = []
        for proc in procedures:
            date = _parse_date(proc.get('performedDateTime')) or _parse_date(
                (proc.get('performedPeriod') or {}).get('start'))
            if date is None:
                self._count_skipped(skipped, 'Procedure', 'missing_performed_date')
                continue
            concept = self._lookup(proc.get('code'), cache)
            rows.append(ProcedureOccurrence(
                person=person,
                procedure_concept=concept or no_match,
                procedure_date=date,
                procedure_datetime=_parse_datetime(proc.get('performedDateTime')),
                procedure_type_concept=ehr_type,
                procedure_source_value=_source_text(proc.get('code'))[:50],
            ))
        return self._upsert_clinical(
            ProcedureOccurrence, 'procedure_occurrence_id', 'procedure_concept_id',
            'procedure_date', 'procedure_source_value', person, rows, source_user_id, org)

    def _ingest_immunizations(self, person, immunizations, ehr_type, no_match, cache, source_user_id, org, skipped):
        # OMOP models immunizations as drug exposures (shares the DrugExposure table).
        rows = []
        for imm in immunizations:
            date = _parse_date(imm.get('occurrenceDateTime'))
            if date is None:
                self._count_skipped(skipped, 'Immunization', 'missing_occurrence_date')
                continue
            concept = self._lookup(imm.get('vaccineCode'), cache)
            rows.append(DrugExposure(
                person=person,
                drug_concept=concept or no_match,
                drug_exposure_start_date=date,
                drug_type_concept=ehr_type,
                drug_source_value=_source_text(imm.get('vaccineCode'))[:50],
                route_source_value='VACCINE',
            ))
        return self._upsert_clinical(
            DrugExposure, 'drug_exposure_id', 'drug_concept_id',
            'drug_exposure_start_date', 'drug_source_value', person, rows, source_user_id, org)

    def _ingest_clinical_observations(self, person, allergies, reports, ehr_type, no_match, cache, source_user_id, org, skipped):
        # AllergyIntolerance + DiagnosticReport land in the OMOP observation table.
        # Allergies are tagged with qualifier_source_value='ALLERGY' so they can
        # be filtered separately for the allergy list endpoint (PHR-S FM PH.2.5).
        items = [
            {'code': a.get('code'),
             'effective': a.get('recordedDate') or a.get('onsetDateTime'),
             'resource_type': 'AllergyIntolerance',
             'value': a.get('criticality') or '',
             'qualifier': 'ALLERGY',
             'value_source': _source_text(a.get('clinicalStatus'))[:50]}
            for a in allergies
        ] + [
            {'code': r.get('code'),
             'effective': r.get('effectiveDateTime') or r.get('issued'),
             'resource_type': 'DiagnosticReport',
             'value': r.get('conclusion') or '',
             'qualifier': None,
             'value_source': None}
            for r in reports
        ]
        rows = []
        for item in items:
            date = _parse_date(item['effective'])
            if date is None:
                self._count_skipped(skipped, item['resource_type'],
                                    'missing_effective_date')
                continue
            concept = self._lookup(item['code'], cache)
            rows.append(Observation(
                person=person,
                observation_concept=concept or no_match,
                observation_date=date,
                observation_datetime=_parse_datetime(item['effective']),
                observation_type_concept=ehr_type,
                value_as_string=(item['value'] or '')[:60],
                observation_source_value=_source_text(item['code'])[:50],
                qualifier_source_value=item.get('qualifier'),
                value_source_value=item.get('value_source'),
            ))
        return self._upsert_clinical(
            Observation, 'observation_id', 'observation_concept_id',
            'observation_date', 'observation_source_value', person, rows, source_user_id, org)

    def _document_doc_type(self, doc):
        doc_type = doc.get('type') if isinstance(doc.get('type'), dict) else None
        categories = doc.get('category') or []
        if not isinstance(categories, list):
            categories = []
        text = ' '.join(
            part for part in [
                _source_text(doc_type),
                ' '.join(_source_text(cat) for cat in categories if isinstance(cat, dict)),
                doc.get('description') or '',
            ]
            if part
        ).lower()
        if any(term in text for term in ('imaging', 'radiology', 'scan', 'mri', 'ct ')):
            return 'IMAGING'
        if any(term in text for term in ('lab', 'laboratory', 'pathology', 'result')):
            return 'LAB_RESULTS'
        if 'consent' in text:
            return 'CONSENT'
        if any(term in text for term in ('advance directive', 'living will')):
            return 'ADVANCE_DIRECTIVE'
        return 'OTHER'

    def _document_status(self, doc):
        status_value = (doc.get('docStatus') or doc.get('status') or '').lower()
        if status_value == 'superseded':
            return PatientDocument.STATUS_SUPERSEDED
        if status_value in ('entered-in-error', 'revoked'):
            return PatientDocument.STATUS_REVOKED
        return PatientDocument.STATUS_ACTIVE

    def _document_attachment(self, doc):
        contents = doc.get('content') or []
        if not isinstance(contents, list):
            return {}
        for content in contents:
            if not isinstance(content, dict):
                continue
            attachment = content.get('attachment') or {}
            if isinstance(attachment, dict) and attachment:
                return attachment
        return {}

    def _ingest_document_references(self, person, document_references, source_user_id, org, skipped):
        ids = []
        for doc in document_references:
            attachment = self._document_attachment(doc)
            file_url = (attachment.get('url') or '')[:200]
            title = (
                attachment.get('title')
                or doc.get('description')
                or _source_text(doc.get('type'))
                or doc.get('id')
                or ''
            )
            if not file_url and not title:
                self._count_skipped(skipped, 'DocumentReference', 'missing_content')
                continue
            context = doc.get('context') or {}
            if not isinstance(context, dict):
                context = {}
            period = context.get('period') or {}
            if not isinstance(period, dict):
                period = {}
            effective_date = (
                _parse_date(doc.get('date'))
                or _parse_date(period.get('start'))
            )
            defaults = {
                'doc_type': self._document_doc_type(doc),
                'title': title[:255] or None,
                'file_url': file_url or None,
                'file_name': (attachment.get('title') or file_url.rsplit('/', 1)[-1])[:255] or None,
                'status': self._document_status(doc),
                'effective_date': effective_date,
            }
            lookup = {'person': person}
            if file_url:
                lookup['file_url'] = file_url
            else:
                lookup['title'] = defaults['title']
                lookup['effective_date'] = effective_date

            obj, _created = PatientDocument.objects.update_or_create(
                **lookup,
                defaults=defaults,
            )
            ids.append(obj.id)

            if _created:
                ProvenanceRecord.objects.create(
                    source=self.provenance_source,
                    source_user_id=source_user_id or '',
                    target_patient_id=str(person.person_id),
                    organization=org,
                    content_type=ContentType.objects.get_for_model(PatientDocument),
                    object_id=obj.id,
                )
        return ids

    def _upsert_clinical(self, model, pk_field, cid_field, date_field, sv_field,
                         person, rows, source_user_id, org):
        """Idempotent upsert for clinical rows, keyed by (source_value, date) —
        the stable identity of a clinical event, independent of how its code
        resolves. If a row already exists for that key, its concept is updated in
        place when it changed (so a vocabulary load upgrading 'No matching
        concept' to a real concept doesn't strand a duplicate) and any stacked
        duplicates are collapsed onto the earliest row; otherwise the row is
        inserted. `rows` is a list of unsaved model instances (person already
        set). Returns touched + inserted pk ids."""
        if not rows:
            return []
        desired = {(getattr(r, sv_field), getattr(r, date_field)): r for r in rows}  # last wins
        ct = ContentType.objects.get_for_model(model)
        touched, new_rows = [], []
        with suppress_patient_record_refresh():
            for (sv, date), inst in desired.items():
                existing = list(model.objects.filter(**{
                    'person': person, sv_field: sv, date_field: date}).order_by(pk_field))
                if not existing:
                    new_rows.append(inst)
                    continue
                keep, extras = existing[0], existing[1:]
                if extras:  # collapse historical stacked rows for this event
                    extra_ids = [getattr(m, pk_field) for m in extras]
                    ProvenanceRecord.objects.filter(
                        content_type=ct, object_id__in=extra_ids).delete()
                    model.objects.filter(**{f'{pk_field}__in': extra_ids}).delete()
                if getattr(keep, cid_field) != getattr(inst, cid_field):
                    setattr(keep, cid_field, getattr(inst, cid_field))
                    keep._skip_patient_record_refresh = True
                    keep.save(update_fields=[cid_field])
                    touched.append(getattr(keep, pk_field))
                elif extras:
                    touched.append(getattr(keep, pk_field))
            inserted = self._bulk_insert(model, pk_field, new_rows, source_user_id, person, org)
        return touched + inserted

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
                source=self.provenance_source,
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
        org = get_request_org(request)

        if is_on_behalf_of:
            if is_service_token(request):
                if has_explicit_actor and actor_identity is None:
                    return Response({'detail': 'Actor identity not found.'},
                                    status=status.HTTP_403_FORBIDDEN)
                if has_explicit_actor and not can_write_patient(actor_identity, person_id):
                    return Response({'detail': 'Actor does not have write access to this patient.'},
                                    status=status.HTTP_403_FORBIDDEN)
            elif org is None:
                if not has_explicit_actor:
                    return Response(
                        {'detail': 'actor_iss and actor_sub required when writing on behalf of another person.'},
                        status=status.HTTP_400_BAD_REQUEST)
                if not can_write_patient(actor_identity, person_id):
                    return Response({'detail': 'Actor does not have write access to this patient.'},
                                    status=status.HTTP_403_FORBIDDEN)

        if org is not None:
            from omop_core.models import PatientRecord
            if not PatientRecord.objects.filter(person_id=person_id, organization=org).exists():
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


class FhirPatientSyncView(FhirSyncView):
    """POST /api/fhir/patient-sync/ — the **connector** path (plan item B0): a
    patient ingests their OWN HealthKit data with their Firebase token, no service
    token. Unlike /api/fhir/sync/ (service-token, on-behalf-of), this permits a
    regular authenticated patient identity, resolves the Person from that identity
    (any person_id / actor_* in the body is ignored, so a patient can never write
    to someone else's record), and records provenance PATIENT_SELF.
    """
    permission_classes = [IsAuthenticated]
    throttle_scope = 'patient_sync'
    provenance_source = 'PATIENT_SELF'
    self_service_only = True


class FhirPatientDeleteView(APIView):
    """POST /api/fhir/patient-delete/ — propagate HealthKit deletions (plan item
    B4). HealthKit reports deletions as opaque UUIDs, and OMOP has no per-sample
    external id, so the app identifies rows by what it synced. Each target removes
    the patient's own Measurement rows matching ``source_value`` + ``date``
    (optionally narrowed by ``datetime`` and ``value``), plus their provenance.
    Scoped to the authenticated patient — a patient can only delete their own.

    Body: ``{"targets": [{"source_value": "Heart rate", "date": "2026-05-01",
    "datetime": "2026-05-01T08:00:00Z", "value": 61}, ...]}``
    """
    permission_classes = [IsAuthenticated]
    throttle_scope = 'patient_sync'

    @transaction.atomic
    def post(self, request):
        if not (hasattr(request.user, 'issuer') and request.user.issuer != 'urn:service'):
            return Response({'detail': 'Patient identity required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        from patient_portal.services import resolve_or_create_person
        person = resolve_or_create_person(request.user)

        targets = request.data.get('targets')
        if not isinstance(targets, list):
            return Response({'detail': 'targets must be a list.'},
                            status=status.HTTP_400_BAD_REQUEST)

        meas_ct = ContentType.objects.get_for_model(Measurement)
        deleted = 0
        with suppress_patient_record_refresh():
            for target in targets:
                sv = (target.get('source_value') or '')[:50]
                obs_date = _parse_date(target.get('date') or target.get('datetime'))
                if not sv or obs_date is None:
                    continue
                qs = Measurement.objects.filter(
                    person=person, measurement_source_value=sv, measurement_date=obs_date)
                obs_dt = _parse_datetime(target.get('datetime'))
                if obs_dt is not None:
                    qs = qs.filter(measurement_datetime=obs_dt)
                if target.get('value') is not None:
                    qs = qs.filter(value_as_number=target['value'])
                ids = list(qs.values_list('measurement_id', flat=True))
                if ids:
                    ProvenanceRecord.objects.filter(
                        content_type=meas_ct, object_id__in=ids).delete()
                    qs.delete()
                    deleted += len(ids)
        return Response({'person_id': person.person_id, 'deleted': deleted},
                        status=status.HTTP_200_OK)


class FhirPatientConsentView(APIView):
    """GET/POST /api/fhir/patient-consent/ — record the patient's HealthKit
    data-sharing consent server-side (plan item B6). Upserts a single
    ``data_sharing`` PatientConsent for the authenticated patient; the per-category
    scope (vitals / activity / sleep / clinical) is stored in ``consent_document``.

    POST body: ``{"granted": true, "categories": ["vitals", "activity"]}``
    """
    permission_classes = [IsAuthenticated]
    throttle_scope = 'patient_sync'

    def _patient_user(self, request):
        if not (hasattr(request.user, 'issuer') and request.user.issuer != 'urn:service'):
            return None
        from patient_portal.services import resolve_or_create_person
        from patient_portal.models import PatientUser
        resolve_or_create_person(request.user)   # ensure the PatientUser link exists
        return PatientUser.objects.filter(identity=request.user).first()

    def post(self, request):
        patient_user = self._patient_user(request)
        if patient_user is None:
            return Response({'detail': 'Patient identity required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        from patient_portal.models import PatientConsent
        granted = bool(request.data.get('granted', False))
        categories = request.data.get('categories') or []
        PatientConsent.objects.update_or_create(
            patient_user=patient_user, consent_type='data_sharing',
            defaults={'consent_granted': granted,
                      'consent_document': json.dumps({'healthkit_categories': categories})})
        return Response({'granted': granted, 'categories': categories},
                        status=status.HTTP_200_OK)

    def get(self, request):
        patient_user = self._patient_user(request)
        if patient_user is None:
            return Response({'detail': 'Patient identity required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        from patient_portal.models import PatientConsent
        consent = PatientConsent.objects.filter(
            patient_user=patient_user, consent_type='data_sharing').first()
        categories = []
        if consent and consent.consent_document:
            try:
                categories = json.loads(consent.consent_document).get('healthkit_categories', [])
            except (ValueError, TypeError):
                categories = []
        return Response({'granted': bool(consent and consent.consent_granted),
                         'categories': categories}, status=status.HTTP_200_OK)
