"""D08/D18: purpose narratives belonging to one typed DrugExposure event."""
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from omop_core.models import Concept, DrugExposure, FieldConceptMapping, Observation
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


def medication_contexts(person_id, exposure_ids):
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
def save_medication_context(exposure, values, *, record_provenance=None):
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
        resolved[key] = concept, source, mapping.type_concept_id or 32817
    previous = medication_contexts(exposure.person_id, [exposure.pk]).get(exposure.pk, {})
    now = timezone.now()
    with suppress_patient_record_refresh():
        for key, value in values.items():
            value = value or ''
            if value == previous.get(key, ''):
                continue
            concept, source, type_id = resolved[key]
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
