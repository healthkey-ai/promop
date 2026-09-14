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
    from omop_core.services.patient_record_service import (
        PATIENT_RECORD_OMOP_MAPPED_FIELDS, _latest_blood_count_measurements,
    )
    from omop_core.services.clinical_units import blood_count_projection
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
    count_dates = {
        field: (row.measurement_date, row.pk)
        for field, row in _latest_blood_count_measurements(snapshot).items()
    }
    # One joined mapping lookup is sufficient. The editor descriptor also
    # loads choices, units, and unrelated vocabulary recipes that readers do
    # not need and that would exceed the full-refresh query budget.
    mappings = FieldConceptMapping.objects.filter(
        status='approved', field_name__in=readable_fields,
    ).exclude(value_kind='json').exclude(field_name='cytogenetic_markers', vocabulary_id='SNOMED', concept_code='107675007').values(
        'field_name', 'omop_table', 'concept_id', 'concept__concept_code', 'source_value',
    )
    for mapping in mappings:
        name = mapping['field_name']
        target = mapping_target_for(mapping['omop_table'])
        if target not in indexed:
            continue
        source_value = mapping['source_value'] or mapping['concept__concept_code']
        row = indexed[target].get((mapping['concept_id'], source_value))
        if row is None:
            continue
        count_field = {
            'absolute_neutrophile_count': 'anc_thousand_per_ul',
            'platelet_count': 'platelet_count_thousand_per_ul',
        }.get(name, name)
        if count_field in ('anc_thousand_per_ul', 'platelet_count_thousand_per_ul'):
            # Approved question mappings still carry source-scale results.
            row_date = getattr(row, 'measurement_date', None) or row.observation_date
            if count_dates.get(count_field, (row_date, row.pk)) > (row_date, row.pk):
                continue
            count_dates[count_field] = (row_date, row.pk)
            values.update(blood_count_projection(count_field, row))
            continue
        value = row.value_as_number if row.value_as_number is not None else row.value_as_string
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
        try:
            return project_selections(person, value, projection)
        except Exception:
            logger.warning('Cytogenetic projection failed; edit remains pending')
            return False
    target = projection.get('omop_table')
    concept_id = projection.get('concept_id')
    source_value = projection.get('source_value')
    if target not in _TARGET_CONFIG or concept_id is None or not source_value:
        return False
    model, pk_field, concept_field, date_field, type_field, src_field, val_num, val_str = _TARGET_CONFIG[target]
    # Occurrence tables cannot encode a null/negative answer. Leave that edit
    # pending on PatientRecord instead of inventing an affirmative occurrence.
    if val_num is None and (_is_empty(value) or value is False):
        return False

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
            if existing and not note_changed and all(getattr(instance, f) == v for f, v in previous.items()):
                return acknowledge_existing
            instance._skip_patient_record_refresh = True
            instance.save()
        return True
    except Exception:
        logger.warning('OMOP value projection failed; edit remains pending')
        return False


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
