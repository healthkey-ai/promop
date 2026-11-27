"""Removing a created field: retire it by default, delete it only when nothing depends on it.

A created field (``CustomPatientField``) is global: one definition, one approved
``FieldConceptMapping``, optional ``FieldChoice`` answers and ``FieldDiseaseMembership``
rows, shared by every organization on the instance. Records hold its values in
``PatientRecord.custom_fields`` and, once projected, as OMOP facts under the
mapping's source key. A mistake or a demo needs a way to undo it, and the database
makes a plain delete impossible anyway (``CustomPatientField.mapping`` is ``PROTECT``).

**Retire** (the default; reversible). ``retired_at`` is set and the field's mapping is
flipped to ``rejected``, so every reader that asks for approved mappings or active
fields stops seeing it: the patient tabs, the writable-fields descriptor, the Field
Mappings lists, the patient-visible fields, the derive and provisional passes and the
formulas. Nothing a record holds changes: ``custom_fields.<name>`` and the OMOP facts
stay exactly as they were, the definition, answers and scope are kept, and
``refresh_patient_record`` carries a retired field's stored value over unchanged
(``retired_values``). ``restore`` approves the mapping again and clears ``retired_at``.
The name stays taken: a field cannot be created again under a retired name, only
restored, so the stored values are never silently adopted by a different definition.

**Delete** (permanent). Only when ``usage`` finds nothing to lose: no record holds a
value under the key and no OMOP fact holding an answer exists under the field's own source key
(a row whose value was cleared holds none and is deleted with the field). Facts that
only sit at its concept are the patients' own data and do not block (#2930). It removes
the definition, its mapping, formula, scope, synonyms, answers (with their aliases,
codes and concept decisions), and the record-level traces that carry the name but no
value: ``user_edited_fields`` pins (a pending clear), proposal rejections and
provisional entries. The record's values are not touched because there are none.
"""
import json
import logging

from django.db import transaction
from django.utils import timezone

from omop_core.services import custom_field_values

logger = logging.getLogger(__name__)

# Per projection target: the model's concept and source-value columns.
_FACT_TABLES = {
    'measurement': ('Measurement', 'measurement_concept_id', 'measurement_source_value'),
    'observation': ('Observation', 'observation_concept_id', 'observation_source_value'),
    'condition': ('ConditionOccurrence', 'condition_concept_id', 'condition_source_value'),
    'drug_exposure': ('DrugExposure', 'drug_concept_id', 'drug_source_value'),
    'procedure': ('ProcedureOccurrence', 'procedure_concept_id', 'procedure_source_value'),
}


class RemovalError(Exception):
    """A removal the field's state refuses. ``code`` names the reason for a client."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _holders(name: str):
    """The records whose ``custom_fields`` hold a value under ``name`` (a JSON null is none)."""
    from django.db.models import JSONField, Value
    from django.db.models.fields.json import KeyTransform

    from omop_core.models import PatientRecord

    # The name is data, never part of a lookup path: ``psa__level`` is one key, not a
    # nested one, and ``x__isnull`` is not a lookup.
    return (PatientRecord.objects.filter(custom_fields__has_key=name)
            .annotate(_held=KeyTransform(name, 'custom_fields'))
            .exclude(_held=Value(None, output_field=JSONField())))


def _own_facts(field):
    """The OMOP rows written under the field's own source key, or ``None`` for a table that has none."""
    from django.apps import apps

    mapping = field.mapping
    target = custom_field_values.target_for(mapping.omop_table)
    if target is None:
        return None
    model_name, _concept_column, source_column = _FACT_TABLES[target]
    model = apps.get_model('omop_core', model_name)
    return model.objects.filter(**{source_column: custom_field_values.source_key(field)}), target


def _valueless(rows, target: str):
    """The rows of ``rows`` that carry no answer: what clearing a value leaves behind.

    Only a measurement or an observation can be empty; any other fact is its own answer.
    """
    from django.db.models import Q

    if target not in ('measurement', 'observation'):
        return rows.none()
    return rows.filter(
        Q(value_as_number__isnull=True),
        Q(value_as_string__isnull=True) | Q(value_as_string=''),
        Q(value_as_concept_id__isnull=True) | Q(value_as_concept_id=0),
    )


def _has_facts(field) -> bool:
    """Whether any OMOP row holding an answer was written under the field's own source key.

    Rows that merely sit at the field's concept are the patients' own data (a PSA
    result at LOINC 2857-1 exists whether or not anyone defined a field for it).
    Deleting the field removes how the data is shown, never the rows, and a field
    created again over that concept reads them again, so they do not block a delete.
    A row under the field's key whose value was cleared holds no answer either: it is
    the field's own empty trace and goes with the field (``_drop_empty_facts``).
    Only a row under the key that still holds an answer blocks.
    """
    found = _own_facts(field)
    if found is None:
        return False
    rows, target = found
    return rows.exclude(pk__in=_valueless(rows, target).values('pk')).exists()


def _drop_empty_facts(field) -> None:
    """Delete the emptied rows the field itself wrote, with the links that have no cascade."""
    from django.contrib.contenttypes.models import ContentType

    from omop_core.models import MeasurementOwnership, ProvenanceRecord
    from omop_core.signals import suppress_patient_record_refresh

    found = _own_facts(field)
    if found is None:
        return
    rows, target = found
    empty = _valueless(rows, target)
    pk_name = rows.model._meta.pk.name
    ids = list(empty.values_list(pk_name, flat=True))
    if not ids:
        return
    # A GenericForeignKey has no cascade, and MeasurementOwnership holds a bare integer.
    ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(rows.model), object_id__in=ids).delete()
    if target == 'measurement':
        MeasurementOwnership.objects.filter(measurement_id__in=ids).delete()
    # The rows hold no value, so no record's derivation changes.
    with suppress_patient_record_refresh():
        rows.model.objects.filter(**{f'{pk_name}__in': ids}).delete()


def usage(field) -> dict:
    """What depends on a created field: the counts a confirmation shows, and whether it may be deleted.

    ``records_with_value`` is one query; ``has_facts`` is one existence query on the
    field's table. ``can_delete`` is true only when both are empty.
    """
    from omop_core.models import FieldChoice

    records = _holders(field.field_name).count()
    facts = _has_facts(field)
    answers = (FieldChoice.objects.filter(field_name=field.field_name)
               .exclude(context_key__startswith='lookup:').count())
    return {
        'field_name': field.field_name,
        'retired': field.retired_at is not None,
        'records_with_value': records,
        'has_facts': facts,
        'answers': answers,
        'can_delete': records == 0 and not facts,
    }


def _lock(field, related: bool = False):
    """The field row, locked. A field another request deleted meanwhile is a ``gone`` refusal."""
    from omop_core.models import CustomPatientField

    rows = CustomPatientField.objects.select_for_update()
    if related:
        rows = rows.select_related('mapping')
    try:
        return rows.get(pk=field.pk)
    except CustomPatientField.DoesNotExist:
        raise RemovalError('gone', 'This field no longer exists.') from None


def retire(field, user=None) -> bool:
    """Hide the field everywhere and keep everything it holds. False if it already was retired."""
    from omop_core.models import CustomPatientField, FieldConceptMapping

    with transaction.atomic():
        locked = _lock(field)
        if locked.retired_at is not None:
            return False
        now = timezone.now()
        locked.retired_at = now
        locked.save(update_fields=['retired_at', 'updated_at'])
        # ``update()``, not ``save()``: approving a mapping re-projects every record's
        # value into OMOP (a signal), and this change must not move any data.
        FieldConceptMapping.objects.filter(pk=locked.mapping_id).update(
            status='rejected', updated_at=now)
        _drop_derived_entries(locked.field_name)
        transaction.on_commit(lambda: logger.info(
            'Created field %s retired by %s', locked.field_name, getattr(user, 'pk', None)))
    field.retired_at = now
    return True


def restore(field) -> None:
    """Bring a retired field back, with its mapping, answers, scope and values as they were."""
    from omop_core.models import CustomPatientField, FieldConceptMapping

    with transaction.atomic():
        locked = _lock(field, related=True)
        if locked.retired_at is None:
            raise RemovalError('not_retired', 'This field is not removed.')
        mapping = locked.mapping
        if mapping.omop_table and mapping.source_value:
            clash = (FieldConceptMapping.objects.filter(
                status='approved', omop_table__iexact=mapping.omop_table.strip(),
                source_value=mapping.source_value).exclude(pk=mapping.pk).order_by('field_name').first())
            if clash is not None:
                raise RemovalError(
                    'mapping_conflict',
                    'Another approved mapping uses this field\'s table and source value '
                    f'({clash.field_name}); change that one before restoring.',
                )
        now = timezone.now()
        FieldConceptMapping.objects.filter(pk=mapping.pk).update(status='approved', updated_at=now)
        locked.retired_at = None
        locked.save(update_fields=['retired_at', 'updated_at'])
    field.retired_at = None


def delete(field) -> dict:
    """Delete the field for good. Raises ``RemovalError('has_data')`` when anything depends on it.

    The field is retired (and that committed) before the final check and the delete:
    a retired field takes no new value from the record editor, so a write that
    resolved the field while it was active cannot land between the check and the
    delete. A refusal after that point leaves the field retired.
    """
    from omop_core.models import (
        CustomPatientField, FieldChoice, FieldConceptMapping, FieldDiseaseMembership,
        FieldFormula, FieldSynonym,
    )

    name = field.field_name
    found = usage(field)
    if not found['can_delete']:
        raise _has_data(found)
    retire(field)
    with transaction.atomic():
        # Lock the definition, so a restore or a second delete waits and then finds it gone.
        locked = _lock(field, related=True)
        found = usage(locked)
        if locked.retired_at is None or not found['can_delete']:
            raise _has_data(found)
        mapping_id = locked.mapping_id
        _drop_empty_facts(locked)
        locked.delete()
        FieldConceptMapping.objects.filter(pk=mapping_id).delete()
        FieldFormula.objects.filter(field_name=name).delete()
        FieldDiseaseMembership.objects.filter(field_name=name).delete()
        FieldSynonym.objects.filter(field_name=name).delete()
        # Answers go with their aliases, codes, scope and concept decisions (cascades).
        FieldChoice.objects.filter(field_name=name).delete()
        _drop_record_traces(name)
        _drop_derived_entries(name)
    return found


def _has_data(found: dict) -> RemovalError:
    return RemovalError(
        'has_data',
        'Records still hold values for this field, or OMOP facts are written under it. '
        'Retire it instead: its values are kept and it can be restored.',
        **found)


def _drop_record_traces(name: str) -> None:
    """Remove what records keep *about* a deleted field: pending-edit pins and rejections.

    Only reached when no record holds a value, so no stored value is lost. A pin is
    ``custom_fields.<name>`` in ``user_edited_fields`` (a pending clear); a retired
    field keeps its pins, which a derivation carries over until it is restored.
    One statement, so a concurrent edit's other pins are not overwritten.
    """
    from omop_core.models import PatientRecord, ProposalRejection

    pin = custom_field_values.pending_key(name)
    _jsonb_remove(PatientRecord, 'user_edited_fields', pin, array=True)
    ProposalRejection.objects.filter(field_name=pin).delete()


def _drop_derived_entries(name: str) -> None:
    """Take a field's key out of the provisional picture and its confidences.

    Both are rebuilt from the proposals at the next derivation, which no longer reads
    the field, so this only keeps the record read from marking a field that is gone.
    """
    from omop_core.models import PatientRecordConfidence, PatientRecordProvisional

    for model in (PatientRecordProvisional, PatientRecordConfidence):
        _jsonb_remove(model, 'custom_fields', name, array=False)


def _jsonb_remove(model, column: str, key: str, *, array: bool) -> None:
    """Remove ``key`` from a jsonb object column (or string element from an array), in one statement."""
    from django.db import connection

    table = connection.ops.quote_name(model._meta.db_table)
    col = connection.ops.quote_name(column)
    # ``jsonb_exists`` / ``@>`` select the rows that carry it, so unrelated rows are not rewritten.
    where = f'{col} @> %s::jsonb' if array else f'jsonb_exists({col}, %s)'
    probe = json.dumps([key]) if array else key
    with connection.cursor() as cursor:
        # Identifiers are quoted model metadata, never request data; the key is a bound parameter.
        cursor.execute(f'UPDATE {table} SET {col} = {col} - %s WHERE {where}', [key, probe])  # nosec B608


def retired_values(stored: dict | None) -> dict:
    """The values a record's ``custom_fields`` holds under retired fields, to carry over a derivation.

    A derivation rebuilds ``custom_fields`` from the active fields' facts. A retired
    field has no reader, so without this its stored value would be dropped by the next
    refresh and a restore would find nothing.
    """
    from omop_core.models import CustomPatientField

    stored = stored or {}
    if not stored:
        return {}
    retired = set(CustomPatientField.objects.retired()
                  .filter(field_name__in=list(stored)).values_list('field_name', flat=True))
    return {name: stored[name] for name in retired}


def retired_pins(user_edited_fields) -> set[str]:
    """The pending-edit pins (``custom_fields.<name>``) of retired fields, to keep through a derivation.

    A derivation keeps only the pins of fields it can still represent, so a pending edit or
    clear of a retired field would lose its pin and, after a restore, its protection. Only a
    permanent delete removes a pin.
    """
    from omop_core.models import CustomPatientField

    named = custom_field_values.pending_names(user_edited_fields)
    if not named:
        return set()
    retired = CustomPatientField.objects.retired().filter(field_name__in=list(named))
    return {custom_field_values.pending_key(name) for name in retired.values_list('field_name', flat=True)}
