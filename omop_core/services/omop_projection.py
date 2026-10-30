"""Project PatientRecord edits into today's OMOP facts, retaining earlier days."""
import logging
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from omop_core.models import (
    ConditionOccurrence, DrugExposure, FieldConceptMapping, Measurement, Observation,
    PatientRecord, Person, ProcedureOccurrence,
)
from omop_core.services.breast_mapping_safety import wrong_breast_field_mappings
from omop_core.services.mappings import CONCEPT_PATIENT_REPORTED_TYPE
from omop_core.services.pk import next_pk

logger = logging.getLogger(__name__)

# An explicit empty result stops derivation from falling back to older results.
CLEAR_VALUE = 'PatientRecord:cleared'

# OMOP type concept "Patient self-report" (Type Concept vocabulary).
PATIENT_SELF_REPORT_TYPE = CONCEPT_PATIENT_REPORTED_TYPE

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
    if (not entry or entry.get('clinical_input_contract')
            or not entry.get('writable') or entry.get('target') != 'patient_record'):
        return None
    if not entry.get('projection'):
        return None
    return {
        **entry['projection'],
        'value_kind': entry.get('value_kind'),
        'unit': entry.get('unit') or entry['projection'].get('unit'),
    }


def curated_values_from_snapshot(snapshot, *, confirmed_clinical_fields=None):
    """Read approved scalar mappings during external OMOP refreshes only.

    Direct saves acknowledge successful projections without reading them back.
    Later imports must therefore be able to read approved mappings even when
    the field has no built-in extractor (for example supportive therapy dates).
    All facts come from the snapshot already loaded by that external refresh.
    """
    from omop_core.services.patient_record_service import (
        PATIENT_RECORD_OMOP_MAPPED_FIELDS, _ASSERTION_DETAIL_FIELDS,
        _latest_assertion_rows, _latest_blood_count_measurements,
        _latest_loinc_measurement,
    )
    from omop_core.services.clinical_units import blood_count_projection
    from omop_core.services.field_write_service import _invert_inverse_assertion
    from omop_core.services.write_descriptor import (
        PINNED_PROJECTION_FIELDS, _LIFECYCLE_FIELDS, _WRITE_RECIPE_INCOMPLETE,
        get_serializer_read_only_fields, mapping_target_for,
    )

    readable_fields = {
        field.name: field for field in PatientRecord._meta.concrete_fields
        if field.name in PATIENT_RECORD_OMOP_MAPPED_FIELDS
        and field.name not in get_serializer_read_only_fields()
        and field.name not in _LIFECYCLE_FIELDS
        and field.name not in _WRITE_RECIPE_INCOMPLETE
        and field.name not in PINNED_PROJECTION_FIELDS
        and field.get_internal_type() != 'JSONField'
    }

    indexed = {}
    for target, rows in (('measurement', snapshot.measurements), ('observation', snapshot.observations)):
        _, _, concept_field, _, _, source_field, _, _ = _TARGET_CONFIG[target]
        by_key = {}
        for row in rows:
            by_key.setdefault((getattr(row, concept_field), getattr(row, source_field)), row)
        indexed[target] = by_key
    from omop_core.services.clinical_input_contract import (
        NEW_INPUT_FIELDS, select_sct_source, values_from_snapshot,
    )
    values = values_from_snapshot(snapshot, confirmed_fields=confirmed_clinical_fields)
    values.pop('sct_date', None)
    if confirmed_clinical_fields is not None:
        confirmed_clinical_fields.discard('sct_date')
    # Assertion fields are answered across both OMOP tables: the latest row
    # wins, and a newer unusable answer deliberately leaves the field unknown.
    # An exact mapping reads only one table, so use the same selected row as
    # the assertion extractor for precedence. A reviewed mapping to another
    # concept is still read when no assertion-code row answers the field.
    assertion_rows = dict(_latest_assertion_rows(snapshot))
    for field, selected in list(assertion_rows.items()):
        detail_field = _ASSERTION_DETAIL_FIELDS.get(selected[2])
        if detail_field:
            assertion_rows.setdefault(detail_field, selected)
    count_dates = {
        field: (row.measurement_date, row.pk)
        for field, row in _latest_blood_count_measurements(snapshot).items()
    }
    # One joined mapping lookup is sufficient. The editor descriptor also
    # loads choices, units, and unrelated vocabulary recipes that readers do
    # not need and that would exceed the full-refresh query budget.
    mappings = FieldConceptMapping.objects.filter(
        status='approved', field_name__in=readable_fields,
    ).exclude(wrong_breast_field_mappings()).exclude(value_kind='json').exclude(field_name='cytogenetic_markers', vocabulary_id='SNOMED', concept_code='107675007').values(
        'field_name', 'omop_table', 'concept_id', 'concept__concept_code', 'source_value',
    )
    sct_mapping = None
    for mapping in mappings:
        name = mapping['field_name']
        if name == 'sct_date':
            sct_mapping = mapping
            continue
        source_value = mapping['source_value'] or mapping['concept__concept_code']
        if name in NEW_INPUT_FIELDS:
            # Dedicated decoder owns units, tombstones and invalid latest facts;
            # generic conversion must not resurrect an older mapped result.
            continue
        target = mapping_target_for(mapping['omop_table'])
        if target not in indexed:
            continue
        row = indexed[target].get((mapping['concept_id'], source_value))
        if row is None:
            continue
        selected = assertion_rows.get(name)
        if selected is not None:
            row_date = (getattr(row, 'measurement_date', None)
                        or getattr(row, 'observation_date', None))
            # Only a strictly older approved mapping is suppressed. A mapped
            # row and the selected assertion can share a date on different
            # tables, and their table-local ids are not comparable (separate
            # sequences); deferring to the other table there would silently
            # undo a reviewed same-day edit on the next refresh.
            if (row_date or date.min) < (selected[0] or date.min):
                continue
        question = {
            'ki67_proliferation_index': '29593-1',
            'test_methodology': '85069-3',
        }.get(name)
        if question:
            latest = _latest_loinc_measurement(snapshot, question)
            row_date = getattr(row, 'measurement_date', None) or row.observation_date
            if latest and (latest.measurement_date, latest.pk) >= (row_date, row.pk):
                # The extractor has already handled this question, including
                # coded answers, newer source-only results, and explicit clears.
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
            # The stored row carries the source answer. Invert only when the
            # mapping is the reviewed inverse code for this field; another
            # approved concept already describes the field directly.
            value = _invert_inverse_assertion(
                name, value, source_value, mapping['concept__concept_code'])
            value = readable_fields[name].to_python(value)
            if name == 'cytogenetic_markers':
                from omop_core.services.cytogenetics import (
                    normalise_cytogenetic_markers, read_cytogenetic_summary,
                )
                value = normalise_cytogenetic_markers(read_cytogenetic_summary(row))
            values[name] = value
            if name == 'sct_date' and confirmed_clinical_fields is not None:
                # A valid custom source is the final curated winner. Its source
                # evidence, not an older invalid SDK row, resolves pending intent.
                confirmed_clinical_fields.add(name)
        except (ValidationError, ValueError, TypeError):
            continue
    sct = select_sct_source(snapshot, sct_mapping)
    if sct.has_value:
        values['sct_date'] = sct.value
    if sct.confirmed and confirmed_clinical_fields is not None:
        confirmed_clinical_fields.add('sct_date')
    return values


def project_field_to_omop(mapping) -> int:
    """Backfill pending user edits, not values already derived from OMOP.

    The pending-field list makes retries idempotent. Lock each record through
    projection and refresh, using the same lock as the PATCH handler.
    """
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.services.write_descriptor import (
        PINNED_PROJECTION_FIELDS, build_writable_field_descriptor,
    )
    from omop_core.services.clinical_input_contract import uses_dedicated_writer

    if mapping.field_name in ('drug_exposure.role', 'drug_exposure.category'):
        # The pending JSON/revision log does not preserve per-attribute fact
        # authorship. Curation cannot turn a patient answer into an EHR fact.
        return 0
    if uses_dedicated_writer(mapping.field_name):
        # Curation is not evidence of who authored pending projection values.
        return 0
    if mapping.field_name in PINNED_PROJECTION_FIELDS:
        # A reviewed built-in recipe owns these fields. A curator mapping must
        # not re-project a pending current-state answer: the backfill has no
        # author, so it would write the recipe type and could take over a
        # clinical fact (#5521).
        return 0
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
                         after_pk=None, type_concept_id=None, patient_user_id=None):
    """Update a matching non-erroneous fact today, or create today's fact.

    Matching includes the concept and source key. Earlier dates are history and
    are never updated. The person lock serializes concurrent first writes for a
    day; an atomic savepoint keeps projection failures from poisoning PATCH.

    *type_concept_id* types the fact by who wrote it, overriding the recipe's
    type: 32865 when a patient or their representative is the author. It then
    also narrows matching to facts of that type, so a patient's answer never
    updates a clinical fact in place and takes over its type and provenance;
    it sits beside it as its own fact. The rule holds the other way too: an
    edit by anyone else never updates a patient's self-report fact, unless the
    recipe itself is typed self-report. *patient_user_id* (``issuer|sub``)
    records a PATIENT_SELF provenance row whenever the patient themselves
    writes, the same row the PHR entry endpoints record.

    Normally returns whether a fact changed. Direct saves can acknowledge an
    identical existing fact too, so it is not mistaken for a failed projection.
    Collection edits can supply after_pk to keep corrections newer than an
    aggregate import that superseded earlier individual rows on the same day.
    """
    from omop_core.services.clinical_input_contract import uses_dedicated_writer
    if uses_dedicated_writer(field_name):
        raise ValidationError('Clinical inputs require the dedicated source writer.')
    if field_name == 'cytogenetic_markers' and 'choice_projections' in projection:
        from omop_core.services.cytogenetics import project_selections
        try:
            return project_selections(person, value, projection)
        except Exception:
            logger.warning('Cytogenetic projection failed; edit remains pending')
            return False
    # Some descriptors project affirmative answers only: No/Unknown is not a
    # negative occurrence, so it stays pending on PatientRecord. Distinct from
    # an ordinary boolean observation, which records No as 0.0 (#5521). The
    # rows an earlier Yes wrote are withdrawn, or derivation would keep
    # reading a fact the record no longer asserts.
    if projection.get('affirmative_only') and (value is False or _is_empty(value)):
        _retract_affirmative_facts(person, projection, type_concept_id)
        return False
    target = projection.get('omop_table')
    concept_id = projection.get('concept_id')
    source_value = projection.get('source_value')
    if target not in _TARGET_CONFIG or concept_id is None or not source_value:
        return False
    # This path carries the PatientRecord value. A reviewed inverse assertion
    # code stores the opposite answer; other approved concepts keep their own.
    from omop_core.services.field_write_service import _invert_inverse_assertion
    value = _invert_inverse_assertion(
        field_name, value, source_value, projection.get('concept_code'))
    model, pk_field, concept_field, date_field, type_field, src_field, val_num, val_str = _TARGET_CONFIG[target]
    # Occurrence tables cannot encode a null/negative answer. Leave that edit
    # pending on PatientRecord instead of inventing an affirmative occurrence.
    if val_num is None and (_is_empty(value) or value is False):
        return False

    today = timezone.localdate()
    fact_type = type_concept_id or projection.get('type_concept_id') or 32817
    try:
        with transaction.atomic():
            Person.objects.select_for_update().get(pk=person.pk)
            from omop_core.services.cancer_details import projection_scope
            scope = projection_scope(person, field_name, model)
            candidates = model.objects.filter(
                person=person, is_erroneous=False,
                **{concept_field: concept_id, src_field: source_value, date_field: today}, **scope,
            )
            # Reusing an older same-author row behind another author's newer
            # fact acknowledges a correction that refresh will immediately undo.
            # Only the current same-day fact is eligible for in-place reuse.
            current_pk = candidates.order_by('-' + pk_field).values_list(pk_field, flat=True).first()
            if current_pk is not None:
                candidates = candidates.filter(pk=current_pk)
            if type_concept_id is not None:
                candidates = candidates.filter(**{type_field: type_concept_id})
            elif fact_type != PATIENT_SELF_REPORT_TYPE:
                candidates = candidates.exclude(**{type_field: PATIENT_SELF_REPORT_TYPE})
            if after_pk is not None:
                candidates = candidates.filter(pk__gt=after_pk)
            instance = candidates.order_by('-' + pk_field).first()
            existing = instance is not None
            if instance is None:
                instance = model(**{
                    pk_field: next_pk(model, pk_field), 'person': person,
                    concept_field: concept_id, src_field: source_value,
                    date_field: today, type_field: fact_type, **scope,
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
                    elif kind == 'concept':
                        instance.value_as_string = str(value)
                        concept_pk = projection.get('value_concept_map', {}).get(str(value))
                        if concept_pk is not None:
                            instance.value_as_concept_id = concept_pk
                    else:
                        try:
                            instance.value_as_number = float(value)
                        except (ValueError, TypeError):
                            instance.value_as_string = str(value)
                instance.unit_source_value = projection.get('unit') or None
                instance.unit_concept_id = projection.get('unit_concept_id') or None
            if existing and not note_changed and all(getattr(instance, f) == v for f, v in previous.items()):
                if acknowledge_existing and patient_user_id:
                    _record_patient_provenance(instance, patient_user_id)
                return acknowledge_existing
            if scope and any(value is not None for value in scope.values()):
                setattr(instance, f'{target}_datetime', timezone.now())
            instance._skip_patient_record_refresh = True
            instance.save()
            if patient_user_id:
                _record_patient_provenance(instance, patient_user_id)
        return True
    except Exception:
        logger.warning('OMOP value projection failed; edit remains pending')
        return False


def _retract_affirmative_facts(person, projection, type_concept_id=None):
    """Mark the facts an affirmative-only recipe wrote as entered in error.

    Only this recipe's own rows are touched, found by the source value it
    writes, so an imported diagnosis with the same concept is never flagged.
    The author rule is project_single_value's: a patient's No retracts only
    self-report rows, and anyone else's No retracts only the rest. Flagging
    rather than deleting keeps the history and takes effect the same day; an
    end date of today would still count as current.
    """
    target = projection.get('omop_table')
    concept_id = projection.get('concept_id')
    source_value = projection.get('source_value')
    if target not in _TARGET_CONFIG or concept_id is None or not source_value:
        return
    model, _pk, concept_field, _date, type_field, src_field, _num, _str = _TARGET_CONFIG[target]
    fact_type = type_concept_id or projection.get('type_concept_id') or 32817
    rows = model.objects.filter(
        person=person, is_erroneous=False,
        **{concept_field: concept_id, src_field: source_value},
    )
    if type_concept_id is not None:
        rows = rows.filter(**{type_field: type_concept_id})
    elif fact_type != PATIENT_SELF_REPORT_TYPE:
        rows = rows.exclude(**{type_field: PATIENT_SELF_REPORT_TYPE})
    try:
        with transaction.atomic():
            # The same lock the Yes path takes, so a No cannot evaluate its
            # rows before a concurrent Yes has committed its own.
            Person.objects.select_for_update().get(pk=person.pk)
            for row in rows:
                row.is_erroneous = True
                row.erroneous_reason = 'retracted by a later PatientRecord answer'
                row._skip_patient_record_refresh = True
                row.save(update_fields=['is_erroneous', 'erroneous_reason'])
                del row._skip_patient_record_refresh
    except Exception:
        logger.warning('Could not retract the earlier affirmative fact; edit remains pending')


def patient_self_report_type():
    """32865 when this database has the concept, else None.

    Fact type columns are foreign keys checked when the request's transaction
    commits, after the projection savepoint has closed. A type the vocabulary
    lacks would fail the whole save there rather than leave the edit pending,
    so without the concept the recipe's type stands, as it did before.
    """
    from omop_core.models import Concept

    if Concept.objects.filter(concept_id=PATIENT_SELF_REPORT_TYPE).exists():
        return PATIENT_SELF_REPORT_TYPE
    logger.warning('Concept %s (Patient self-report) is not loaded; patient edits keep the recipe type',
                   PATIENT_SELF_REPORT_TYPE)
    return None


def _record_patient_provenance(instance, patient_user_id):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import ProvenanceRecord

    ProvenanceRecord.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(type(instance)), object_id=instance.pk,
        source='PATIENT_SELF', source_user_id=patient_user_id,
    )


def without_cleared_history(rows, target):
    """Hide cleared results and their predecessors from derivation, not storage.

    Input is sorted newest first by date and ID. A later non-empty result is
    retained and becomes current even when a previous day was cleared.
    """
    _, _, concept_field, _, _, source_field, _, _ = _TARGET_CONFIG[target]
    cleared = set()
    result = []
    event = 'measurement_event_id' if target == 'measurement' else 'observation_event_id'
    kind = 'meas_event_field_concept_id' if target == 'measurement' else 'obs_event_field_concept_id'
    for row in rows:
        key = (getattr(row, concept_field), getattr(row, source_field),
               getattr(row, event, None), getattr(row, kind, None))
        if row.value_source_value == CLEAR_VALUE:
            cleared.add(key)
        elif key not in cleared:
            result.append(row)
    return result
