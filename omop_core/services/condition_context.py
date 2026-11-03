"""Diagnosis-scoped narrative attributes, with typed CDM event relationships."""
from datetime import datetime, time

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from omop_core.models import Concept, ConditionOccurrence, FieldConceptMapping, Measurement, Observation
from omop_core.services.clinical_text import OwnedTextReader, store_text
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh

FIELD = 'condition_occurrence.condition_occurrence_id'
SPECS = {
    'note': (Measurement, 'Measurement', '48767-8', 'phr:condition-note'),
    'care_team': (Observation, 'Observation', '85847-2', 'phr:condition-care-team'),
}
EVENT_FIELDS = {Measurement: 'meas_event_field_concept', Observation: 'obs_event_field_concept'}


def _event_concepts():
    # Vocabulary releases contain several CDM versions; accept all correctly
    # typed field concepts, including older imports using the field as code.
    return Concept.objects.filter(vocabulary_id='CDM', domain_id='Metadata', invalid_reason__isnull=True).filter(
        Q(concept_code=FIELD) | Q(concept_name=FIELD)).order_by('concept_id')


def _mappings():
    return {m.field_name.removeprefix('condition.'): m for m in
            FieldConceptMapping.objects.filter(field_name__in=[f'condition.{k}' for k in SPECS])}


def measurement_context_filter():
    """Keep diagnosis annotations out of lab results and lab activity counts."""
    keys = {'phr:condition-note'} | set(FieldConceptMapping.objects.filter(
        field_name='condition.note').exclude(source_value='').values_list('source_value', flat=True))
    return Q(measurement_source_value__in=keys, meas_event_field_concept_id__in=_event_concepts().values('pk'))


def condition_contexts(person_id, condition_ids):
    """Batch read only this patient's facts linked to these condition events.

    Source keys remain the identity when a concept changes. Old concept-0
    observations remain readable after the note mapping moves to Measurement.
    A blank latest fact explicitly clears the field and hides earlier history.
    """
    ids = set(condition_ids)
    if not ids:
        return {}
    mappings = _mappings()
    keys = {key: {spec[3], mappings[key].source_value} - {''} if key in mappings
            else {spec[3]} for key, spec in SPECS.items()}
    event_ids = list(_event_concepts().values_list('pk', flat=True))
    result, latest = {}, {}
    for model in (Measurement, Observation):
        table = model._meta.db_table
        rows = list(model.objects.filter(**{
            'person_id': person_id, 'is_erroneous': False,
            f'{table}_event_id__in': ids,
            f'{EVENT_FIELDS[model]}_id__in': event_ids,
            f'{table}_source_value__in': set().union(*keys.values()),
        }).order_by(f'{table}_date', f'{table}_datetime', 'pk'))
        reader = OwnedTextReader(person_id, rows)
        for row in rows:
            key = next(k for k, values in keys.items() if getattr(row, f'{table}_source_value') in values)
            event_id = getattr(row, f'{table}_event_id')
            # Dates precede IDs because IDs are independent across CDM tables.
            stamp = (getattr(row, f'{table}_date'),
                     getattr(row, f'{table}_datetime') or timezone.make_aware(
                         datetime.combine(getattr(row, f'{table}_date'), time.min)),
                     model is SPECS[key][0], row.pk)
            if (event_id, key) in latest and stamp <= latest[event_id, key]:
                continue
            latest[event_id, key] = stamp
            value = reader.read(row, namespace='condition', context=f'condition.{key}')
            result.setdefault(event_id, {})[key] = value or ''
    return result


@transaction.atomic
def save_condition_context(condition, values, *, record_provenance=None):
    """Append immutable snapshots; serialize concurrent edits on the parent."""
    condition = ConditionOccurrence.objects.select_for_update().get(pk=condition.pk, person_id=condition.person_id)
    if condition.is_erroneous:
        raise ValidationError('Cannot edit an entered-in-error diagnosis.')
    event = _event_concepts().first()
    if event is None:
        raise ValidationError('The CDM condition event vocabulary must be loaded before saving diagnosis context.')
    mappings = _mappings()
    resolved = {}
    # Validate the entire patch before inserting any fact or overflow note.
    for key in values:
        model, domain, _, default_source = SPECS[key]
        mapping = mappings.get(key)
        if (mapping is None or mapping.status != 'approved' or mapping.value_kind != 'string'
                or mapping.omop_table != model._meta.db_table):
            raise ValidationError({key: 'An approved string mapping in the expected OMOP table is required.'})
        concept = Concept.objects.filter(
            pk=mapping.concept_id, domain_id=domain, standard_concept='S', invalid_reason__isnull=True,
        ).first() if mapping.concept_id else None
        if concept is None:
            concept = Concept.objects.filter(vocabulary_id=mapping.vocabulary_id, concept_code=mapping.concept_code,
                                             domain_id=domain, standard_concept='S', invalid_reason__isnull=True).first()
        if concept is None:
            raise ValidationError({key: 'The mapped standard concept must be loaded in the expected OMOP domain.'})
        source = mapping.source_value or default_source
        if len(source) > 50:
            raise ValidationError({key: 'The mapping source key exceeds the CDM field width.'})
        resolved[key] = (model, concept, source, mapping.type_concept_id or 32817)
    previous = condition_contexts(condition.person_id, [condition.pk]).get(condition.pk, {})
    now = timezone.now()
    with suppress_patient_record_refresh():
        for key, value in values.items():
            value = value or ''
            if value == previous.get(key, ''):
                continue
            model, concept, source, type_id = resolved[key]
            table = model._meta.db_table
            row = model(**{
                f'{table}_id': next_pk(model, f'{table}_id'), 'person_id': condition.person_id,
                f'{table}_concept': concept, f'{table}_type_concept_id': type_id,
                f'{table}_date': now.date(), f'{table}_datetime': now,
                f'{table}_event_id': condition.pk, EVENT_FIELDS[model]: event,
                f'{table}_source_value': source, 'visit_occurrence_id': condition.visit_occurrence_id,
            })
            row.value_as_string = store_text(row, value, namespace='condition', context=f'condition.{key}')
            row.save(force_insert=True)
            if record_provenance:
                record_provenance(row)
    return condition_contexts(condition.person_id, [condition.pk]).get(condition.pk, {})
