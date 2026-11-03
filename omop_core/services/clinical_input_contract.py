"""Versioned source-input SDK, gated by exact approved curated bindings.

The recipes preserve the published integration contract (#1845), not standard
clinical concepts or computed formulas. No mapping is approved by this module.
Callers must supply an already-authorized Person; curation grants no access.
"""
from datetime import date, datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


CLINICAL_INPUT_CONTRACT_VERSION = 2
CLEAR_VALUE = 'PatientRecord:cleared'
RECIPES = {
    'prothrombin_time_uln': ('cb:pt-uln', 'number', '1'),
    'activated_partial_thromboplastin_time_uln': ('cb:aptt-uln', 'number', '1'),
    'able_to_swallow_tablets_capsules': ('cb:swallow-tablets', 'boolean', None),
    'sct_date': ('mm-sct-date', 'date', None),
}
CLINICAL_INPUT_FIELDS = frozenset(RECIPES)
NEW_INPUT_FIELDS = CLINICAL_INPUT_FIELDS - {'sct_date'}


def capabilities(*, lock=False, mappings=None):
    """Per-field availability, independent of SDK version. Never trust a client descriptor.

    Writers lock existing mapping rows through commit, preventing approval or
    revocation from racing the recipe used for a write. Missing rows fail closed.
    Legacy SCT's 32817 placeholder is NOT permission for the 0/32865 SDK recipe.
    """
    from omop_core.models import Concept, FieldConceptMapping

    if mappings is None:
        rows = FieldConceptMapping.objects.filter(field_name__in=CLINICAL_INPUT_FIELDS).order_by('field_name')
        if lock:
            rows = rows.select_for_update()
        mappings = {row.field_name: row for row in rows}
    result = {}
    vocabulary_ready = None
    for field, (source, kind, unit) in RECIPES.items():
        row = mappings.get(field)
        compatible = row is not None and (
            row.status == 'approved' and row.omop_table == 'Observation'
            and row.concept_id == 0 and row.source_value == source
            and row.value_kind == kind and not row.multiple and not row.value_vocabulary
            and row.type_concept_id == 32865 and row.unit == (unit or '')
        )
        if compatible and vocabulary_ready is None:
            vocabulary_ready = (Concept.objects.filter(pk=0).exists()
                                and Concept.objects.filter(pk=32865, vocabulary_id='Type Concept').exists())
        ready = bool(compatible and vocabulary_ready)
        result[field] = {
            'writable': ready, 'contract_version': CLINICAL_INPUT_CONTRACT_VERSION,
            'reason': ('Approved compatible source-input recipe.' if ready else
                       'Requires an exact approved clinical-input mapping and source vocabulary.'),
        }
    return result


def descriptors(*, mappings=None):
    """Use the current PatientRecord envelope, but never its generic fact writer."""
    result = {}
    for field, capability in capabilities(mappings=mappings).items():
        source, kind, unit = RECIPES[field]
        result[field] = {
            **capability, 'kind': 'direct' if capability['writable'] else 'unmapped',
            'group': 'needs-concept-set',
            'target': 'patient_record',
            'clinical_input_contract': CLINICAL_INPUT_CONTRACT_VERSION,
            'value_kind': kind, 'unit': unit,
            'projection': {
                'omop_table': 'observation', 'concept_id': 0, 'type_concept_id': 32865,
                'source_value': source, 'unit': unit, 'unit_concept_id': None,
            },
        }
    return result


def uses_dedicated_writer(field):
    """Protect new fields even without a recipe; preserve legacy SCT native writes."""
    if field in NEW_INPUT_FIELDS:
        return True
    if field == 'sct_date':
        from omop_core.models import FieldConceptMapping
        # Revocation or missing vocabulary must not turn a contract-bound SCT
        # field back into a direct-storage edit. Legacy nonzero mappings remain.
        return FieldConceptMapping.objects.filter(field_name=field, concept_id=0).exists()
    return False


def validated_values(changes, *, for_write=True):
    from omop_core.models import PatientRecord

    if set(changes) - CLINICAL_INPUT_FIELDS:
        raise ValidationError('Unsupported clinical input field.')
    cleaned = {}
    for field, value in changes.items():
        kind = RECIPES[field][1]
        if value is not None:
            if kind == 'number':
                if isinstance(value, bool):
                    raise ValidationError({field: 'A ratio is not a boolean.'})
                try:
                    value = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    raise ValidationError({field: 'Expected a finite ratio.'}) from None
                if not value.is_finite():
                    raise ValidationError({field: 'Expected a finite ratio.'})
            elif kind == 'boolean' and type(value) is not bool:
                raise ValidationError({field: 'Expected an explicit boolean or null.'})
            elif kind == 'date':
                if isinstance(value, datetime) or not isinstance(value, (date, str)):
                    raise ValidationError({field: 'Expected an ISO date.'})
                if isinstance(value, str):
                    try:
                        value = date.fromisoformat(value)
                    except ValueError:
                        raise ValidationError({field: 'Expected an ISO date.'}) from None
        try:
            cleaned[field] = PatientRecord._meta.get_field(field).clean(value, None)
        except ValidationError as exc:
            raise ValidationError({field: exc.messages}) from None
        if (for_write and kind == 'date' and cleaned[field] is not None
                and cleaned[field] > timezone.localdate()):
            raise ValidationError({field: 'SCT date cannot be in the future.'})
    return cleaned


def _append_inputs(person, values, today):
    from omop_core.models import Observation
    from omop_core.services.pk import next_pk

    appended = {}
    for field, value in values.items():
        source, kind, unit = RECIPES[field]
        number, string = None, None
        if value is not None:
            if kind == 'number':
                number = value
            elif kind == 'boolean':
                string = 'true' if value else 'false'
            else:
                string = value.isoformat()
        row = Observation(
            observation_id=next_pk(Observation, 'observation_id'), person=person,
            observation_concept_id=0, observation_type_concept_id=32865,
            observation_date=today, observation_source_value=source,
            value_as_number=number, value_as_string=string,
            value_source_value=CLEAR_VALUE if value is None else None,
            unit_source_value=unit,
        )
        row._skip_patient_record_refresh = True
        row.save()
        appended[field] = row.pk
    return appended


def write_clinical_inputs(person, changes, *, today=None):
    """Append facts atomically; preserve imports and verify pending-aware readback.

    No direct-storage fallback, ownership reassignment or new authorization.
    Equal-value retries append evidence too. A failed round-trip rolls back.
    """
    from omop_core.models import Observation, PatientRecord, Person
    from omop_core.services.patient_record_service import refresh_patient_record

    values = validated_values(changes)
    with transaction.atomic():
        record, _ = PatientRecord.objects.get_or_create(person=person)
        record = PatientRecord.objects.select_for_update().get(pk=record.pk)
        Person.objects.select_for_update().get(pk=person.pk)
        available = capabilities(lock=True)
        errors = {field: available[field]['reason'] for field in values if not available[field]['writable']}
        if errors:
            raise ValidationError(errors)
        appended = _append_inputs(person, values, today or date.today())
        latest = {}
        for row in Observation.objects.filter(
            person=person, is_erroneous=False,
            observation_source_value__in=[RECIPES[field][0] for field in values],
        ).order_by('-observation_date', '-observation_id'):
            latest.setdefault(row.observation_source_value, row.pk)
        if any(latest.get(RECIPES[field][0]) != pk for field, pk in appended.items()):
            raise ValidationError('A newer clinical source prevents this input from becoming current.')
        # An explicit new command supersedes only its own older pending intent.
        # The source identity check above also protects clears/equal values from
        # being acknowledged against an unselected future import.
        other_pending = set(record.user_edited_fields or []) - values.keys()
        record.user_edited_fields = sorted(other_pending)
        record.save(update_fields=['user_edited_fields'])
        record = refresh_patient_record(person)
        if any(getattr(record, field) != value or field in (record.user_edited_fields or [])
               for field, value in values.items()):
            raise ValidationError('Clinical input did not round-trip through its OMOP source.')
        # Writing one source input does not attest to older pending edits for
        # other fields, even when a generic extractor echoes their stored value.
        record.user_edited_fields = sorted(set(record.user_edited_fields or []) | other_pending)
        record.save(update_fields=['user_edited_fields'])
        return record


def _decode_fact(field, fact):
    source, kind, unit = RECIPES[field]
    if (fact.observation_concept_id != 0 or fact.observation_type_concept_id != 32865
            or fact.unit_concept_id is not None or (fact.unit_source_value or '') != (unit or '')):
        raise ValidationError('Incompatible clinical input fact.')
    if fact.value_source_value == CLEAR_VALUE:
        if fact.value_as_number is not None or fact.value_as_string not in (None, '') or fact.value_as_concept_id is not None:
            raise ValidationError('Conflicting clear payload.')
        return None
    if fact.value_as_concept_id is not None:
        raise ValidationError('Unexpected coded answer.')
    if kind == 'number':
        if fact.value_as_number is None or fact.value_as_string not in (None, ''):
            raise ValidationError('Expected only a numeric answer.')
        return validated_values({field: fact.value_as_number}, for_write=False)[field]
    if fact.value_as_number is not None:
        raise ValidationError('Unexpected numeric answer.')
    if kind == 'boolean':
        raw = (fact.value_as_string or '').strip().lower()
        if raw not in ('true', 'false'):
            raise ValidationError('Expected a boolean answer.')
        return raw == 'true'
    if not fact.value_as_string:
        raise ValidationError('Missing date answer.')
    return validated_values({field: fact.value_as_string}, for_write=False)[field]


def _source_index(snapshot):
    raw = getattr(snapshot, 'clinical_input_observations', None)
    by_source = getattr(snapshot, 'obs_by_source', None) if raw is None else None
    if by_source is None:
        # The curated reader also accepts small in-memory snapshots; derive the
        # index from their already-fetched observations without another query.
        by_source = {}
        for fact in snapshot.observations if raw is None else raw:
            by_source.setdefault(fact.observation_source_value, []).append(fact)
    return by_source


def source_rows_from_snapshot(snapshot, field):
    """Shared source selection, including an explicitly empty raw snapshot."""
    return _source_index(snapshot).get(RECIPES[field][0], ())


def _snapshot_inputs(snapshot, *, selected_sources=None):
    """Read compatibility is independent of current write approval.

    New fields always use the published source keys. For SCT preserve the native
    decoder unless the latest row carries the SDK's exact 0/32865 provenance.
    An invalid latest SDK/source answer explicitly masks older values.
    """
    result, confirmed = {}, set()
    by_source = _source_index(snapshot)
    for field, (source, kind, unit) in RECIPES.items():
        rows = by_source.get(source, ())
        if not rows:
            continue
        fact = max(rows, key=lambda row: (row.observation_date or date.min, row.pk))
        if field == 'sct_date' and (fact.observation_concept_id != 0
                                   or fact.observation_type_concept_id != 32865):
            continue
        try:
            value = _decode_fact(field, fact)
            confirmed.add(field)
        except (ValueError, ValidationError, InvalidOperation):
            value = None
        result[field] = value
        if selected_sources is not None:
            # Identity remains useful when a malformed latest SDK fact masks
            # an older answer. It does not mean that pending intent is confirmed.
            selected_sources[field] = ('observation', fact.pk)
    return result, confirmed


def values_from_snapshot(snapshot, *, confirmed_fields=None, selected_sources=None):
    values, confirmed = _snapshot_inputs(snapshot, selected_sources=selected_sources)
    if confirmed_fields is not None:
        confirmed_fields.update(confirmed)
    return values


def confirmed_fields_from_snapshot(snapshot):
    """Only valid source answers (including tombstones) can resolve pending intent."""
    return _snapshot_inputs(snapshot)[1]


@dataclass(frozen=True)
class SctSourceSelection:
    has_value: bool = False
    value: date | None = None
    confirmed: bool = False
    selected_key: tuple[str, int] | None = None


def native_sct_source(snapshot):
    """The existing native parser over newest-first, generic-filtered rows."""
    row = next((row for row in snapshot.observations
                if row.observation_source_value == RECIPES['sct_date'][0]), None)
    if row is not None and row.value_as_string:
        try:
            value = datetime.strptime(row.value_as_string[:10], '%Y-%m-%d').date()
            return SctSourceSelection(True, value, False, ('observation', row.pk))
        except (ValueError, TypeError):
            pass
    return SctSourceSelection()


def select_sct_source(snapshot, mapping=None):
    """Pure SCT source selection shared by derivation and provenance.

    A mapping, when given, is the already-approved scalar SCT row from the
    caller's existing mapping query. Lists are generic-filtered/newest-first;
    raw canonical history lives separately on the snapshot. This preserves
    native -> strict SDK -> curated priority, not cross-source recency.
    """
    from omop_core.models import PatientRecord
    from omop_core.services.write_descriptor import mapping_target_for

    selected = native_sct_source(snapshot)
    confirmed, sources = set(), {}
    values = values_from_snapshot(snapshot, confirmed_fields=confirmed, selected_sources=sources)
    if 'sct_date' in values:
        selected = SctSourceSelection(True, values['sct_date'], 'sct_date' in confirmed, sources['sct_date'])
    if mapping is None:
        return selected
    source = mapping['source_value'] or mapping['concept__concept_code']
    if source == RECIPES['sct_date'][0] and source_rows_from_snapshot(snapshot, 'sct_date'):
        return selected
    target = mapping_target_for(mapping['omop_table'])
    if target not in ('observation', 'measurement'):
        return selected
    rows = snapshot.observations if target == 'observation' else snapshot.measurements
    row = next((row for row in rows
                if getattr(row, f'{target}_concept_id') == mapping['concept_id']
                and getattr(row, f'{target}_source_value') == source), None)
    if row is None:
        return selected
    value = row.value_as_number if row.value_as_number is not None else row.value_as_string
    if value is None:
        return selected
    try:
        value = PatientRecord._meta.get_field('sct_date').to_python(value)
    except (ValidationError, ValueError, TypeError):
        return selected
    return SctSourceSelection(True, value, True, (target, row.pk))
