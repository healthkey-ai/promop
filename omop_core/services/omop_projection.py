"""Project PatientRecord edits into today's OMOP facts, retaining earlier days."""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from omop_core.models import (
    ConditionOccurrence, DrugExposure, FieldConceptMapping, Measurement, Observation,
    PatientRecord, Person, ProcedureOccurrence,
)
from omop_core.services.pk import next_pk

logger = logging.getLogger(__name__)

# An explicit empty result stops derivation from falling back to older results.
CLEAR_VALUE = 'PatientRecord:cleared'

_TARGET_CONFIG = {
    'measurement': (
        Measurement, 'measurement_id', 'measurement_concept_id',
        'measurement_date', 'measurement_type_concept_id',
        'measurement_source_value', 'value_as_number', 'value_as_string',
    ),
    'observation': (
        Observation, 'observation_id', 'observation_concept_id',
        'observation_date', 'observation_type_concept_id',
        'observation_source_value', 'value_as_number', 'value_as_string',
    ),
    'condition': (
        ConditionOccurrence, 'condition_occurrence_id', 'condition_concept_id',
        'condition_start_date', 'condition_type_concept_id',
        'condition_source_value', None, None,
    ),
    'drug_exposure': (
        DrugExposure, 'drug_exposure_id', 'drug_concept_id',
        'drug_exposure_start_date', 'drug_type_concept_id',
        'drug_source_value', None, None,
    ),
    'procedure': (
        ProcedureOccurrence, 'procedure_occurrence_id', 'procedure_concept_id',
        'procedure_date', 'procedure_type_concept_id',
        'procedure_source_value', None, None,
    ),
}


def _is_empty(value):
    return value is None or value == '' or value == [] or value == {}


def projection_for_descriptor(entry):
    """Use the same recipe and value/unit semantics the editor advertises."""
    if not entry or not entry.get('writable') or entry.get('target') != 'patient_record':
        return None
    if not entry.get('projection'):
        return None
    return {
        **entry['projection'],
        'value_kind': entry.get('value_kind'),
        'unit': entry.get('unit') or entry['projection'].get('unit'),
    }


def curated_values_from_snapshot(snapshot):
    """Read approved scalar mappings during external OMOP refreshes only.

    Direct saves acknowledge successful projections without reading them back.
    Later imports must therefore be able to read approved mappings even when
    the field has no built-in extractor (for example supportive therapy dates).
    All facts come from the snapshot already loaded by that external refresh.
    """
    from omop_core.services.patient_record_service import PATIENT_RECORD_OMOP_MAPPED_FIELDS
    from omop_core.services.write_descriptor import (
        _LIFECYCLE_FIELDS, _WRITE_RECIPE_INCOMPLETE,
        get_serializer_read_only_fields, mapping_target_for,
    )

    readable_fields = {
        field.name: field for field in PatientRecord._meta.concrete_fields
        if field.name in PATIENT_RECORD_OMOP_MAPPED_FIELDS
        and field.name not in get_serializer_read_only_fields()
        and field.name not in _LIFECYCLE_FIELDS
        and field.name not in _WRITE_RECIPE_INCOMPLETE
        and field.get_internal_type() != 'JSONField'
    }

    indexed = {}
    for target, rows in (('measurement', snapshot.measurements), ('observation', snapshot.observations)):
        _, _, concept_field, _, _, source_field, _, _ = _TARGET_CONFIG[target]
        by_key = {}
        for row in rows:
            by_key.setdefault((getattr(row, concept_field), getattr(row, source_field)), row)
        indexed[target] = by_key
    values = {}
    # One joined mapping lookup is sufficient. The editor descriptor also
    # loads choices, units, and unrelated vocabulary recipes that readers do
    # not need and that would exceed the full-refresh query budget.
    mappings = FieldConceptMapping.objects.filter(
        status='approved', field_name__in=readable_fields,
    ).exclude(value_kind='json').exclude(field_name='cytogenetic_markers', vocabulary_id='SNOMED', concept_code='107675007').values(
        'field_name', 'omop_table', 'concept_id', 'concept__concept_code', 'concept__vocabulary_id', 'source_value',
    )
    from omop_core.services.field_values import ValueResolver
    mappings = list(mappings)
    resolver = ValueResolver.for_snapshot(snapshot)
    from omop_core.services.breast_cancer import STAGE_QUESTIONS
    from omop_core.services.patient_record_service import _HISTOLOGIC_TYPE_LOINCS
    for mapping in mappings:
        name = mapping['field_name']
        # Built-in staging and histology readers select across supported
        # questions/tables and linked events. A default scalar mapping must
        # not overwrite their newer result with its older question row.
        known_codes = STAGE_QUESTIONS.get(name, {})
        if name == 'histologic_type':
            known_codes = _HISTOLOGIC_TYPE_LOINCS
        if mapping['concept__vocabulary_id'] == 'LOINC' and mapping['concept__concept_code'] in known_codes:
            continue
        target = mapping_target_for(mapping['omop_table'])
        if target not in indexed:
            continue
        source_value = mapping['source_value'] or mapping['concept__concept_code']
        matching = [indexed[target].get((mapping['concept_id'], source_value))]
        for choice in resolver.choices.get((name, ''), []):
            answer = resolver.mapping(choice)
            if answer and answer.question_concept_id:
                question = answer.question_concept
                matching.append(indexed[question.domain_id.lower()].get((question.pk, question.concept_code)))
        matching = [r for r in matching if r is not None]
        from omop_core.services.breast_cancer import fact_date
        row = max(matching, key=lambda r: (fact_date(r), r.pk)) if matching else None
        if row is None:
            continue
        value = resolver.reverse(name, row)
        if value is None:
            continue
        try:
            value = readable_fields[name].to_python(value)
            if name == 'cytogenetic_markers':
                from omop_core.services.cytogenetics import (
                    normalise_cytogenetic_markers, read_cytogenetic_summary,
                )
                value = normalise_cytogenetic_markers(read_cytogenetic_summary(row))
            values[name] = value
        except (ValidationError, ValueError, TypeError):
            continue
    return values


def project_field_to_omop(mapping) -> int:
    """Backfill pending user edits, not values already derived from OMOP.

    The pending-field list makes retries idempotent. Lock each record through
    projection and refresh, using the same lock as the PATCH handler.
    """
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    if mapping.status != 'approved':
        return 0
    projection = projection_for_descriptor(
        build_writable_field_descriptor().get(mapping.field_name)
    )
    if not projection:
        return 0
    records = PatientRecord.objects.filter(
        user_edited_fields__contains=[mapping.field_name],
    ).values_list('pk', flat=True)
    count = 0
    for record_id in records.iterator():
        try:
            with transaction.atomic():
                record = PatientRecord.objects.select_for_update().select_related('person').get(pk=record_id)
                if mapping.field_name not in (record.user_edited_fields or []):
                    continue
                if project_single_value(record.person, mapping.field_name,
                                        getattr(record, mapping.field_name), projection):
                    refresh_patient_record(record.person)
                    count += 1
        except Exception:
            # Database exceptions can contain patient values; keep diagnostics
            # free of identifiers, values, and exception text/tracebacks.
            logger.warning('OMOP mapping backfill failed for a record; edit remains pending')
    return count


def project_single_value(person, field_name, value, projection, *, acknowledge_existing=False,
                         after_pk=None):
    """Project a complete field edit or roll back every constituent write."""
    try:
        with transaction.atomic():
            Person.objects.select_for_update().get(pk=person.pk)
            return _project_single_value(
                person, field_name, value, projection,
                acknowledge_existing=acknowledge_existing, after_pk=after_pk,
            )
    except Exception:
        # Exceptions can contain patient values; never include their text.
        logger.warning('OMOP value projection failed; edit remains pending')
        return False


def _project_single_value(person, field_name, value, projection, *, acknowledge_existing=False,
                          after_pk=None, _clear_overrides=True):
    """Update a matching non-erroneous fact today, or create today's fact.

    Matching includes the concept and source key. Earlier dates are history and
    are never updated. The person lock serializes concurrent first writes for a
    day; an atomic savepoint keeps projection failures from poisoning PATCH.

    Normally returns whether a fact changed. Direct saves can acknowledge an
    identical existing fact too, so it is not mistaken for a failed projection.
    Collection edits can supply after_pk to keep corrections newer than an
    aggregate import that superseded earlier individual rows on the same day.
    """
    if field_name == 'cytogenetic_markers' and 'choice_projections' in projection:
        from omop_core.services.cytogenetics import project_selections
        if not project_selections(person, value, projection):
            raise ValueError('Cytogenetic projection unavailable')
        return True
    from omop_core.services.field_values import ValueResolver
    resolver = ValueResolver([field_name])
    if _is_empty(value) and _clear_overrides:
        # Clear prior question overrides too, including withdrawn decisions.
        from omop_core.services.field_values import historical_questions
        overrides = historical_questions(field_name, projection.get('context_key', ''))
        with transaction.atomic():
            changed, seen = False, set()
            for question in overrides:
                if question.pk in seen:
                    continue
                seen.add(question.pk)
                changed |= _project_single_value(person, field_name, None, {
                    **projection, 'concept_id': question.pk, 'omop_table': question.domain_id.lower(),
                    'source_value': question.concept_code,
                }, acknowledge_existing=acknowledge_existing, after_pk=after_pk, _clear_overrides=False)
            base = _project_single_value(person, field_name, None, projection,
                acknowledge_existing=acknowledge_existing, after_pk=after_pk, _clear_overrides=False)
            return changed or base
    choice = resolver.resolve(field_name, value, projection.get('context_key', '')) if not _is_empty(value) else None
    answer_mapping = resolver.mapping(choice)
    raw_value = value
    if choice:
        value = choice.canonical_value
    projection = dict(projection)
    if answer_mapping and answer_mapping.role == 'fact':
        raise ValueError('An assertion requires an event-aware writer')
    if answer_mapping and answer_mapping.question_concept_id:
        question = answer_mapping.question_concept
        projection.update(concept_id=question.pk, omop_table=question.domain_id.lower(), source_value=question.concept_code)
    target = projection.get('omop_table')
    concept_id = projection.get('concept_id')
    source_value = projection.get('source_value')
    if target not in _TARGET_CONFIG or concept_id is None or not source_value:
        raise ValueError('Projection recipe unavailable')
    model, pk_field, concept_field, date_field, type_field, src_field, val_num, val_str = _TARGET_CONFIG[target]
    # Occurrence tables cannot encode a null/negative answer. Leave that edit
    # pending on PatientRecord instead of inventing an affirmative occurrence.
    if val_num is None and (_is_empty(value) or value is False):
        raise ValueError('Occurrence tables cannot encode a null or negative answer')

    today = timezone.localdate()
    try:
        with transaction.atomic():
            Person.objects.select_for_update().get(pk=person.pk)
            candidates = model.objects.filter(
                person=person, is_erroneous=False,
                **{concept_field: concept_id, src_field: source_value, date_field: today},
            )
            if after_pk is not None:
                candidates = candidates.filter(pk__gt=after_pk)
            instance = candidates.order_by('-' + pk_field).first()
            existing = instance is not None
            if instance is None:
                instance = model(**{
                    pk_field: next_pk(model, pk_field), 'person': person,
                    concept_field: concept_id, src_field: source_value,
                    date_field: today, type_field: projection.get('type_concept_id') or 32817,
                })
            answer_fields = ('value_as_number', 'value_as_string', 'value_as_concept_id',
                             'value_source_value', 'unit_source_value', 'unit_concept_id')
            previous = {f: getattr(instance, f) for f in answer_fields} if val_num else {}
            note_changed = False
            if val_num is not None:
                # Reset all answer columns, including answers on same-day imports.
                instance.value_as_number = None
                instance.value_as_string = None
                instance.value_as_concept_id = None
                instance.value_source_value = CLEAR_VALUE if _is_empty(value) else None
                if not _is_empty(value):
                    kind = projection.get('value_kind')
                    if kind in ('string', 'date', 'json'):
                        if field_name == 'cytogenetic_markers':
                            from omop_core.services.cytogenetics import store_cytogenetic_summary
                            instance.value_as_string, note_changed = store_cytogenetic_summary(instance, str(value))
                        else:
                            instance.value_as_string = str(value)
                    else:
                        try:
                            instance.value_as_number = float(value)
                        except (ValueError, TypeError):
                            instance.value_as_string = str(value)
                instance.unit_source_value = projection.get('unit') or None
                instance.unit_concept_id = projection.get('unit_concept_id') or None
                if answer_mapping and not _is_empty(value):
                    from omop_core.services.field_values import store_source_answer
                    instance.value_as_concept_id = answer_mapping.target_concept_id
                    instance.value_source_value, source_changed = store_source_answer(instance, raw_value)
                    note_changed |= source_changed
            if existing and not note_changed and all(getattr(instance, f) == v for f, v in previous.items()):
                return acknowledge_existing
            instance._skip_patient_record_refresh = True
            instance.save()
        return True
    except Exception:
        # Propagate across recursive clears; the public boundary rolls back
        # the entire edit, including already-written override clear markers.
        raise


def without_cleared_history(rows, target):
    """Hide cleared results and their predecessors from derivation, not storage.

    Input is sorted newest first by date and ID. A later non-empty result is
    retained and becomes current even when a previous day was cleared.
    """
    _, _, concept_field, _, _, source_field, _, _ = _TARGET_CONFIG[target]
    cleared = set()
    result = []
    for row in rows:
        key = (getattr(row, concept_field), getattr(row, source_field))
        if row.value_source_value == CLEAR_VALUE:
            cleared.add(key)
        elif key not in cleared:
            result.append(row)
    return result
