"""
Identity-resolved FHIR ingest API for the fhir_importers connector → ctomop.

POST /api/fhir/sync/

Mirrors the lab_results sync pattern (actor_iss/actor_sub identity resolution +
ScopedTokenPermission), but accepts a FHIR R4 Bundle and ingests the first-cut
scope bound to the resolved Person:

  - Patient        → demographics on the resolved Person (fill-if-empty)
  - Observation    → Measurement (LOINC concept lookup, value/unit/date)
  - Condition      → ConditionOccurrence (SNOMED/ICD lookup, onset date)
  - MedicationStatement → DrugExposure (RxNorm lookup, start date)

Person is resolved from identity, never demographic upsert — ctomop owns the
identity↔Person link. Oncology-specific enrichment (ECOG, stage, biomarkers,
therapy-line episodes) is deferred — see fhir_importers issue #10.
"""
import logging
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
from omop_core.services.pk import next_pk
from omop_core.services.patient_info_service import refresh_patient_info
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


def _lookup_concept(codeable):
    """Resolve a FHIR CodeableConcept to a standard OMOP Concept, or None."""
    for coding in _codings(codeable):
        code = coding.get('code')
        if not code:
            continue
        vocab = _SYSTEM_VOCAB.get(coding.get('system', ''))
        qs = Concept.objects.filter(concept_code=code)
        concept = (qs.filter(vocabulary_id=vocab).first() if vocab else None) or qs.first()
        if concept:
            return concept
    return None


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
        person_id = data.get('person_id')
        bundle = data['bundle']

        # --- Resolve target Person (identity-first; person_id = on-behalf-of) ---
        resolution = self._resolve_person(request, actor_iss, actor_sub, person_id)
        if isinstance(resolution, Response):
            return resolution
        person, org = resolution

        ehr_type = _ensure_concept(EHR_TYPE_CONCEPT_ID)
        no_match = _ensure_concept(NO_MATCHING_CONCEPT_ID)

        # --- Group bundle resources (first-cut scope) ---
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

        result = {
            'person_id': person.person_id,
            'measurement_ids': [],
            'condition_ids': [],
            'drug_exposure_ids': [],
            'demographics_updated': False,
        }

        if patient_res:
            result['demographics_updated'] = self._update_demographics(person, patient_res)

        for obs in observations:
            m_id = self._ingest_observation(person, obs, ehr_type, no_match, actor_iss, actor_sub, org)
            if m_id is not None:
                result['measurement_ids'].append(m_id)

        for cond in conditions:
            c_id = self._ingest_condition(person, cond, ehr_type, no_match, actor_iss, actor_sub, org)
            if c_id is not None:
                result['condition_ids'].append(c_id)

        for med in medications:
            d_id = self._ingest_medication(person, med, ehr_type, no_match, actor_iss, actor_sub, org)
            if d_id is not None:
                result['drug_exposure_ids'].append(d_id)

        # Signals were suppressed per-row; refresh the denormalized view once.
        refresh_patient_info(person)

        return Response(result, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    # Person resolution (mirrors patient_portal.api.lab_results.sync)
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # Resource ingestion
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

    def _record(self, instance, source_user_id, target_person_id, org):
        ProvenanceRecord.objects.create(
            source='EHR_SYNC',
            source_user_id=source_user_id,
            target_patient_id=str(target_person_id),
            organization=org,
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
        )

    @staticmethod
    def _actor_id(actor_iss, actor_sub):
        return f"{actor_iss}|{actor_sub}" if actor_iss and actor_sub else ''

    def _ingest_observation(self, person, obs, ehr_type, no_match, actor_iss, actor_sub, org):
        obs_date = _parse_date(obs.get('effectiveDateTime')) or _parse_date(
            (obs.get('effectivePeriod') or {}).get('start'))
        if obs_date is None:
            return None
        concept = _lookup_concept(obs.get('code'))
        source_value = _source_text(obs.get('code'))[:50]

        qty = obs.get('valueQuantity') or {}
        value_number = qty.get('value')
        unit = qty.get('unit') or qty.get('code')
        value_string = obs.get('valueString') or _source_text(obs.get('valueCodeableConcept'))

        # Dedup: same person + concept (or source text) + date + value.
        if Measurement.objects.filter(
            person=person,
            measurement_date=obs_date,
            measurement_concept_id=concept.concept_id if concept else 0,
            measurement_source_value=source_value,
            value_as_number=value_number,
        ).exists():
            return None  # idempotent re-sync: report only newly-created rows

        m = Measurement(
            measurement_id=next_pk(Measurement, 'measurement_id'),
            person=person,
            measurement_concept_id=concept.concept_id if concept else 0,
            measurement_date=obs_date,
            measurement_type_concept=ehr_type,
            value_as_number=value_number,
            value_as_string=(value_string or '')[:60],
            measurement_source_value=source_value,
            unit_source_value=(unit or '')[:50],
        )
        m._skip_patient_info_refresh = True
        m.save()
        self._record(m, self._actor_id(actor_iss, actor_sub), person.person_id, org)
        return m.measurement_id

    def _ingest_condition(self, person, cond, ehr_type, no_match, actor_iss, actor_sub, org):
        start = _parse_date(cond.get('onsetDateTime')) or _parse_date(
            (cond.get('onsetPeriod') or {}).get('start'))
        if start is None:
            return None
        concept = _lookup_concept(cond.get('code'))
        source_value = _source_text(cond.get('code'))[:50]

        if ConditionOccurrence.objects.filter(
            person=person,
            condition_concept_id=concept.concept_id if concept else NO_MATCHING_CONCEPT_ID,
            condition_start_date=start,
            condition_source_value=source_value,
        ).exists():
            return None

        co = ConditionOccurrence(
            condition_occurrence_id=next_pk(ConditionOccurrence, 'condition_occurrence_id'),
            person=person,
            condition_concept=concept or no_match,
            condition_start_date=start,
            condition_type_concept=ehr_type,
            condition_source_value=source_value,
        )
        co._skip_patient_info_refresh = True
        co.save()
        self._record(co, self._actor_id(actor_iss, actor_sub), person.person_id, org)
        return co.condition_occurrence_id

    def _ingest_medication(self, person, med, ehr_type, no_match, actor_iss, actor_sub, org):
        start = _parse_date((med.get('effectivePeriod') or {}).get('start')) or _parse_date(
            med.get('effectiveDateTime'))
        if start is None:
            return None
        codeable = med.get('medicationCodeableConcept')
        concept = _lookup_concept(codeable)
        source_value = _source_text(codeable)[:50]
        end = _parse_date((med.get('effectivePeriod') or {}).get('end'))

        if DrugExposure.objects.filter(
            person=person,
            drug_concept_id=concept.concept_id if concept else NO_MATCHING_CONCEPT_ID,
            drug_exposure_start_date=start,
            drug_source_value=source_value,
        ).exists():
            return None

        de = DrugExposure(
            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
            person=person,
            drug_concept=concept or no_match,
            drug_exposure_start_date=start,
            drug_exposure_end_date=end,
            drug_type_concept=ehr_type,
            drug_source_value=source_value,
        )
        de._skip_patient_info_refresh = True
        de.save()
        self._record(de, self._actor_id(actor_iss, actor_sub), person.person_id, org)
        return de.drug_exposure_id
