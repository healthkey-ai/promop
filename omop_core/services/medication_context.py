"""D08/D18: purpose narratives belonging to one typed DrugExposure event."""
import logging

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from omop_core.models import Concept, DrugExposure, FieldConceptMapping, Observation, PatientRecord
from omop_core.services.clinical_text import OwnedTextReader, store_text
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh

FIELD = 'drug_exposure.drug_exposure_id'
SOURCES = {'role': 'phr:drug-exposure-role', 'category': 'phr:drug-exposure-category'}


def drug_event_concepts():
    """Accept the standard CDM namespace across vocabulary releases."""
    return Concept.objects.filter(vocabulary_id='CDM', domain_id='Metadata',
                                  invalid_reason__isnull=True).filter(
        Q(concept_code=FIELD) | Q(concept_name=FIELD)).order_by('pk')


def _mappings():
    return {m.field_name.removeprefix('drug_exposure.'): m for m in
            FieldConceptMapping.objects.filter(field_name__in=[f'drug_exposure.{k}' for k in SOURCES])}


def medication_contexts(person_id, exposure_ids, *, type_concept_id=None):
    """Batch read by exposure, never by drug name or ingredient.

    Source identity survives vocabulary corrections. Latest blank explicitly
    clears; erroneous facts are excluded. Event IDs alone are not relationships.
    """
    ids = set(exposure_ids)
    if not ids:
        return {}
    mappings = _mappings()
    keys = {key: {source, mappings[key].source_value} - {''} if key in mappings
            else {source} for key, source in SOURCES.items()}
    rows = list(Observation.objects.filter(
        person_id=person_id, is_erroneous=False,
        observation_event_id__in=DrugExposure.objects.filter(
            person_id=person_id, pk__in=ids, is_erroneous=False).values('pk'),
        obs_event_field_concept_id__in=drug_event_concepts().values('pk'),
        observation_source_value__in=set().union(*keys.values()),
    ).order_by('observation_date', F('observation_datetime').asc(nulls_first=True), 'pk'))
    if type_concept_id is not None:
        rows = [row for row in rows if row.observation_type_concept_id == type_concept_id]
    reader = OwnedTextReader(person_id, rows)
    result = {}
    for row in rows:
        matching = [key for key, sources in keys.items() if row.observation_source_value in sources]
        if len(matching) != 1:
            continue  # A misconfigured shared source key cannot label two fields.
        key = matching[0]
        result.setdefault(row.observation_event_id, {})[key] = reader.read(
            row, namespace='medication', context=f'drug_exposure.{key}') or ''
    return result


@transaction.atomic
def save_medication_context(exposure, values, *, record_provenance=None, type_concept_id=None):
    """Append immutable snapshots, locking the parent to serialize edits."""
    exposure = DrugExposure.objects.select_for_update().get(pk=exposure.pk, person_id=exposure.person_id)
    if exposure.is_erroneous:
        raise ValidationError('Cannot edit an entered-in-error medication exposure.')
    event = drug_event_concepts().first()
    if event is None:
        raise ValidationError('The CDM drug exposure event vocabulary must be loaded before saving medication context.')
    mappings, resolved = _mappings(), {}
    for key in values:
        mapping = mappings.get(key)
        if (mapping is None or mapping.status != 'approved' or mapping.value_kind != 'string'
                or mapping.omop_table != 'observation'):
            raise ValidationError({key: 'An approved string mapping in Observation is required.'})
        concepts = Concept.objects.filter(domain_id='Observation', standard_concept='S', invalid_reason__isnull=True)
        concept = concepts.filter(pk=mapping.concept_id).first() if mapping.concept_id else None
        if concept is None:
            concept = concepts.filter(vocabulary_id=mapping.vocabulary_id, concept_code=mapping.concept_code).first()
        if concept is None:
            raise ValidationError({key: 'The mapped standard Observation concept must be loaded.'})
        source = mapping.source_value or SOURCES[key]
        if len(source) > 50 or any(source in {other_source, mappings[other].source_value if other in mappings else ''}
                                   for other, other_source in SOURCES.items() if other != key):
            raise ValidationError({key: 'The mapping requires a distinct source key of at most 50 characters.'})
        fact_type = type_concept_id or mapping.type_concept_id or 32817
        if not Concept.objects.filter(pk=fact_type, invalid_reason__isnull=True).exists():
            raise ValidationError({key: 'The mapped fact type must be loaded before projection.'})
        resolved[key] = concept, source, fact_type
    previous_by_type = {pk: medication_contexts(exposure.person_id, [exposure.pk], type_concept_id=pk).get(
        exposure.pk, {}) for pk in {spec[2] for spec in resolved.values()}}
    now = timezone.now()
    with suppress_patient_record_refresh():
        for key, value in values.items():
            value = value or ''
            concept, source, type_id = resolved[key]
            if value == previous_by_type[type_id].get(key, ''):
                continue
            row = Observation(
                observation_id=next_pk(Observation, 'observation_id'), person_id=exposure.person_id,
                observation_concept=concept, observation_type_concept_id=type_id,
                observation_date=now.date(), observation_datetime=now,
                observation_event_id=exposure.pk, obs_event_field_concept=event,
                observation_source_value=source, visit_occurrence_id=exposure.visit_occurrence_id,
            )
            row.value_as_string = store_text(row, value, namespace='medication', context=f'drug_exposure.{key}')
            row.save(force_insert=True)
            if record_provenance:
                record_provenance(row)
    return medication_contexts(exposure.person_id, [exposure.pk]).get(exposure.pk, {})


RECORD_FIELD = 'medication_contexts'


def merge_contexts(current, patch):
    result = {str(pk): dict(values) for pk, values in current.items()}
    for pk, values in patch.items():
        result.setdefault(str(pk), {}).update(values)
    return result


def context_delta(previous, patch):
    return {str(pk): changes for pk, values in patch.items()
            if (changes := {key: value for key, value in values.items()
                            if key not in previous.get(str(pk), {})
                            or previous[str(pk)][key] != value})}


def validate_record_contexts(person_id, value, serializer_class, previous=None):
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValidationError('Provide an object keyed by up to 1,000 drug exposure IDs.')
    result = {}
    for raw_id, values in value.items():
        if (not isinstance(raw_id, str) or not raw_id.isascii() or not raw_id.isdigit()
                or len(raw_id) > 19 or not 0 < int(raw_id) <= 2**63 - 1):
            raise ValidationError('Each key must identify a drug exposure.')
        pk = str(int(raw_id))
        if pk in result:
            raise ValidationError('Duplicate drug exposure ID.')
        data = serializer_class(data=values)
        data.is_valid(raise_exception=True)
        result[pk] = {key: text or '' for key, text in data.validated_data.items()}
    owned = set(DrugExposure.objects.filter(person_id=person_id, pk__in=result, is_erroneous=False).values_list('pk', flat=True))
    for pk, values in result.items():
        if int(pk) not in owned and (pk not in (previous or {}) or context_delta(previous, {pk: values})):
            raise ValidationError('Every changed medication must be an active, owned drug exposure.')
    return result


def project_record_contexts(person, values, *, type_concept_id=None, patient_user_id=None):
    """Best-effort forward projection after PatientRecord has been saved."""
    from omop_core.services.omop_projection import _record_patient_provenance
    try:
        with transaction.atomic():
            rows = {str(row.pk): row for row in DrugExposure.objects.filter(
                person=person, pk__in=values, is_erroneous=False)}
            if set(rows) != set(values):
                raise ValidationError('Medication exposure is no longer available.')
            for pk, attributes in values.items():
                save_medication_context(rows[pk], attributes, type_concept_id=type_concept_id,
                    record_provenance=(lambda row: _record_patient_provenance(row, patient_user_id))
                    if patient_user_id else None)
        return True
    except Exception:
        logging.getLogger(__name__).warning('Medication projection failed; PatientRecord edit remains pending')
        return False


def phr_medication_contexts(person_id, exposure_ids):
    ids = set(exposure_ids)
    result = medication_contexts(person_id, ids)
    record = PatientRecord.objects.filter(person_id=person_id).values(RECORD_FIELD, 'user_edited_fields').first()
    if record and RECORD_FIELD in (record['user_edited_fields'] or []):
        eligible = set(DrugExposure.objects.filter(person_id=person_id, pk__in=ids, is_erroneous=False).values_list('pk', flat=True))
        for pk, values in (record[RECORD_FIELD] or {}).items():
            if int(pk) in eligible:
                result.setdefault(int(pk), {}).update(values)
    return result


def backfill_record_contexts(mapping):
    if mapping.status != 'approved':
        return 0
    count = 0
    for pk in PatientRecord.objects.filter(user_edited_fields__contains=[RECORD_FIELD]).values_list('pk', flat=True).iterator():
        with transaction.atomic():
            record = PatientRecord.objects.select_for_update().select_related('person').get(pk=pk)
            if RECORD_FIELD in (record.user_edited_fields or []) and project_record_contexts(
                    record.person, record.medication_contexts):
                count += 1
    return count
