"""Values of created PatientRecord fields: the edit path and its read-back (#2669).

A created field (``CustomPatientField``) has no column: its value lives in
``PatientRecord.custom_fields`` under its name. An edit follows the same
architecture as an edit to a column (docs/patient-record-first-writes.md):

1. The record PATCH carries ``custom_fields: {<name>: <value>}``. The value is
   written to ``PatientRecord.custom_fields`` at once.
2. It is tracked as a pending edit in ``user_edited_fields``, under
   ``custom_fields.<name>``, so a derivation cannot overwrite it.
3. It is projected into OMOP through the field's own mapping, under the
   mapping's source key (``custom:<name>``). A Measurement or Observation
   projection acknowledges the edit and the pending marker goes.
4. ``refresh_patient_record`` rebuilds ``custom_fields`` from OMOP and keeps a
   value still pending (an explicit clear included) until derivation
   represents it.

Only an approved, non-computed field takes a value: a computed one is its
formula's result. Whether a field belongs to the record's disease is not
enforced here (#2333).
"""
import logging
import math
import re
from datetime import date

from omop_core.services.mappings import CONCEPT_EHR_TYPE

logger = logging.getLogger(__name__)

# The key the record PATCH carries created-field values under, and the column.
CONTAINER = 'custom_fields'
# How a created field's pending edit is named in ``user_edited_fields``. No
# PatientRecord column has a dot in its name, so the two never collide.
PENDING_PREFIX = 'custom_fields.'

# The projection target of each OMOP table a mapping may name, however the
# mapping spells it (``Measurement``, ``condition_occurrence``, ...). A table
# not listed has no writer: its field's edits stay pending on the record.
_TARGETS = {
    'measurement': 'measurement',
    'observation': 'observation',
    'condition': 'condition', 'conditionoccurrence': 'condition',
    'drug': 'drug_exposure', 'drugexposure': 'drug_exposure',
    'procedure': 'procedure', 'procedureoccurrence': 'procedure',
}
_VALUE_KINDS = {'text': 'string', 'number': 'number', 'date': 'date', 'boolean': 'boolean'}
# Measurement and Observation hold a number to five decimal places, so a value
# and its read-back agree to that precision and no further.
_NUMBER_PLACES = 5


class CustomValueError(ValueError):
    """A created-field value the record PATCH refuses, by field name."""

    def __init__(self, field_name: str, message: str):
        super().__init__(message)
        self.field_name = field_name
        self.message = message


def target_for(omop_table) -> str | None:
    return _TARGETS.get(re.sub(r'[^a-z]', '', (omop_table or '').lower()))


def source_key(field) -> str:
    """The source value a field's entered facts are written and read under.

    The mapping's own, which creation sets to ``custom:<name>``; that default
    again when a later edit of the mapping left it blank, so the writer and
    the readers never look under different keys.
    """
    return field.mapping.source_value or f'custom:{field.field_name}'


def pending_key(field_name: str) -> str:
    return PENDING_PREFIX + field_name


def pending_names(user_edited_fields) -> set[str]:
    """The created fields named among a record's pending edits."""
    return {key[len(PENDING_PREFIX):] for key in (user_edited_fields or [])
            if isinstance(key, str) and key.startswith(PENDING_PREFIX)}


def is_empty(value) -> bool:
    return value is None or value == ''


def is_editable(field) -> bool:
    return field.mode == 'editable' and field.mapping.status == 'approved'


def definitions(names=None) -> dict:
    """Created fields by name, each with its mapping and concept loaded."""
    from omop_core.models import CustomPatientField

    fields = CustomPatientField.objects.select_related('mapping__concept')
    if names is not None:
        fields = fields.filter(field_name__in=list(names))
    return {field.field_name: field for field in fields}


def coerce(field_type: str, raw):
    """A submitted value as the field's type stores it; ``None`` clears.

    Raises ``ValueError`` with a message for the editor.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if field_type == 'boolean':
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ('true', 'false'):
            return raw.strip().lower() == 'true'
        raise ValueError('Enter true or false.')
    if field_type == 'number':
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError('Enter a number.')
        try:
            number = float(raw)
        except ValueError:
            raise ValueError('Enter a number.') from None
        if not math.isfinite(number):
            raise ValueError('Enter a number.')
        return int(number) if number.is_integer() else number
    if field_type == 'date':
        if not isinstance(raw, str):
            raise ValueError('Enter a date as YYYY-MM-DD.')
        try:
            return date.fromisoformat(raw.strip()).isoformat()
        except ValueError:
            raise ValueError('Enter a date as YYYY-MM-DD.') from None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError('Enter text.')
    return str(raw).strip()


def values_match(field_type: str, left, right) -> bool:
    """Whether two stored values of a field are the same answer."""
    if is_empty(left) or is_empty(right):
        return is_empty(left) and is_empty(right)
    if field_type == 'number':
        try:
            return round(float(left), _NUMBER_PLACES) == round(float(right), _NUMBER_PLACES)
        except (TypeError, ValueError):
            return False
    return left == right


def changed_values(record, payload, fields=None) -> dict:
    """The edits a PATCH's ``custom_fields`` asks for: ``{name: value}``.

    Partial by key: a key the payload does not name is untouched, and a key
    whose value is what the record already holds is not an edit. A key that
    is not an approved, non-computed created field is ignored, as the record
    PATCH ignores a read-only column: a client that sends back the object it
    read also sends computed results, which may have moved since. The
    writable-fields descriptor is where a client learns what it may write.
    Raises ``CustomValueError`` for a value an editable field cannot hold.
    """
    if not isinstance(payload, dict):
        raise CustomValueError(CONTAINER, 'Send an object mapping each field name to its value.')
    stored = record.custom_fields or {}
    if fields is None:
        fields = definitions(payload) if payload else {}
    edits = {}
    for name, raw in payload.items():
        field = fields.get(name)
        if field is None or not is_editable(field):
            continue
        try:
            value = coerce(field.field_type, raw)
        except ValueError as error:
            raise CustomValueError(name, str(error)) from None
        if not values_match(field.field_type, stored.get(name), value):
            edits[name] = value
    return edits


def store(record, edits: dict) -> dict:
    """Write edits onto the record and mark each pending. Returns the old values.

    Sets ``custom_fields`` and ``user_edited_fields`` on the instance; the
    caller saves. A cleared value leaves no key, as derivation leaves none
    for a field with no fact.
    """
    values = dict(record.custom_fields or {})
    previous = {name: values.get(name) for name in edits}
    for name, value in edits.items():
        if is_empty(value):
            values.pop(name, None)
        else:
            values[name] = value
    record.custom_fields = values
    record.user_edited_fields = sorted(
        set(record.user_edited_fields or []) | {pending_key(name) for name in edits})
    return previous


def projection_for(field, unit_concept_ids=None) -> dict | None:
    """The OMOP recipe a created field's value is written with, or ``None``.

    The same shape ``project_single_value`` takes from the writable descriptor.
    ``None`` when the mapping's table has no writer, it has no concept, or its
    source key does not fit the table's source column: truncating the key
    would break the exact match derivation reads it back by.
    """
    from omop_core.services.write_descriptor import _MAPPING_SOURCE_FIELDS

    mapping = field.mapping
    target = target_for(mapping.omop_table)
    source_value = source_key(field)
    if target is None or mapping.concept_id is None:
        return None
    width = _MAPPING_SOURCE_FIELDS[target].max_length
    if width is not None and len(source_value) > width:
        return None
    projection = {
        'omop_table': target,
        'concept_id': mapping.concept_id,
        'concept_code': mapping.concept_code,
        'type_concept_id': mapping.type_concept_id or CONCEPT_EHR_TYPE,
        'source_value': source_value,
        'value_kind': _VALUE_KINDS.get(field.field_type, 'string'),
    }
    if mapping.unit:
        projection['unit'] = mapping.unit
        projection['unit_concept_id'] = (unit_concept_ids or {}).get(mapping.unit)
    return projection


def _unit_concept_ids(fields) -> dict:
    from omop_core.services.write_descriptor import _resolve_concept_ids

    return _resolve_concept_ids({f.mapping.unit for f in fields if f.mapping.unit}, 'UCUM')


def project(person, edits: dict, fields: dict, **authorship) -> set[str]:
    """Write each edit's OMOP fact. Returns the names OMOP now represents.

    Best effort, as for a column: a field with no recipe, a failed write, and
    an occurrence (which cannot hold a cleared or negative answer, nor be read
    back as the value entered) leave the edit pending on the record.
    """
    from omop_core.services.omop_projection import project_single_value

    unit_ids = _unit_concept_ids([fields[name] for name in edits if name in fields])
    projected = set()
    for name, value in edits.items():
        field = fields.get(name)
        projection = projection_for(field, unit_ids) if field is not None else None
        if projection is None:
            continue
        answers = projection['omop_table'] in ('measurement', 'observation')
        # An occurrence says only that something happened, on the day it is
        # written: a yes can be recorded that way, a number, a date or text
        # cannot, and would put a condition or a drug on the record that
        # nobody reported.
        if not answers and field.field_type != 'boolean':
            continue
        if project_single_value(person, name, value, projection, acknowledge_existing=True,
                                **authorship) and answers:
            projected.add(name)
    return projected


def acknowledge(record, names) -> None:
    """Drop the pending marker of edits OMOP now represents."""
    record.user_edited_fields = sorted(
        set(record.user_edited_fields or []) - {pending_key(name) for name in names})


def pending_values(record) -> dict:
    """The record's pending created-field edits: ``{name: value or None}``."""
    values = record.custom_fields or {}
    return {name: values.get(name) for name in pending_names(record.user_edited_fields)}


def restore_pending(derived: dict, pending: dict) -> tuple[dict, set[str]]:
    """Derived values with pending edits put back over them.

    Returns the values and the pending markers to keep. A pending edit wins
    until derivation produces the same answer; a pending clear keeps the field
    empty while an older fact would fill it. An edit to a field since removed
    or made computed has nothing to protect and is dropped.
    """
    if not pending:
        return derived, set()
    fields = definitions(pending)
    values = dict(derived)
    keep = set()
    for name, saved in pending.items():
        field = fields.get(name)
        if field is None or not is_editable(field):
            continue
        if values_match(field.field_type, values.get(name), saved):
            continue
        if is_empty(saved):
            values.pop(name, None)
        else:
            values[name] = saved
        keep.add(pending_key(name))
    return values, keep


def read_value(field_type: str, row, *, entered: bool):
    """A created field's value from the OMOP row that answers it.

    ``entered`` says the row was written by the record editor under the
    field's own source key, so it carries the answer in its value columns
    whatever the type. An imported row answers a boolean by existing and a
    date by its own date. Values are JSON-ready: a number, an ISO date, text.
    """
    number = getattr(row, 'value_as_number', None)
    text = getattr(row, 'value_as_string', None)
    if field_type == 'number':
        if number is None:
            return None
        number = float(number)
        return int(number) if number.is_integer() else number
    if field_type == 'boolean':
        if entered and number is not None:
            return bool(number)
        return True
    if field_type == 'date':
        if entered and text:
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError:
                pass
        row_date = _row_date(row)
        return row_date.isoformat() if row_date else None
    if text:
        return text
    for concept in ('value_as_concept', 'measurement_concept', 'observation_concept',
                    'condition_concept', 'drug_concept', 'procedure_concept'):
        name = getattr(getattr(row, concept, None), 'concept_name', None)
        if name:
            return name
    return None


def _row_date(row):
    for attribute in ('measurement_date', 'observation_date', 'condition_start_date',
                      'drug_exposure_start_date', 'procedure_date'):
        value = getattr(row, attribute, None)
        if value is not None:
            return value
    return None


def latest_row(entered_rows, concept_rows, cleared_on=None):
    """The row that answers a field, and whether the editor wrote it.

    The latest date wins, so an import newer than an entered value replaces
    it; on the same day the entered value wins, being the correction.
    ``cleared_on`` is the day the field was last cleared in the editor: an
    imported fact of that day or before no longer answers it, a later one
    does.
    """
    best = None
    for entered, rows in ((True, entered_rows), (False, concept_rows)):
        for row in rows:
            day = _row_date(row) or date.min
            # Entered rows are already ordered against a clear by the
            # snapshot, row by row: one entered after it, that day included
            # and by another author included, still answers. Only a fact
            # under another source key needs the day to be held against.
            if not entered and cleared_on is not None and day <= cleared_on:
                continue
            key = (day, entered, row.pk)
            if best is None or key > best[0]:
                best = (key, row, entered)
    return (best[1], best[2]) if best else (None, False)


def cleared_days(person, fields) -> dict:
    """The day each field was last cleared in the editor: ``{(target, source): date}``.

    The snapshot a refresh reads has already dropped a cleared row and the
    earlier rows under its own source key. An imported fact carries another
    source key, so the clear has to be read here to hold against it. One query
    per answer table that a created field uses, none when there is none.
    """
    from omop_core.services.omop_projection import CLEAR_VALUE, _TARGET_CONFIG

    sources: dict[str, set] = {}
    for field in fields:
        target = target_for(field.mapping.omop_table)
        if target in ('measurement', 'observation'):
            sources.setdefault(target, set()).add(source_key(field))
    days = {}
    for target, wanted in sources.items():
        model, _pk, _concept, date_field, _type, source_field, _num, _str = _TARGET_CONFIG[target]
        rows = model.objects.filter(
            person=person, is_erroneous=False, value_source_value=CLEAR_VALUE,
            **{f'{source_field}__in': wanted},
        ).values_list(source_field, date_field)
        for source, day in rows:
            key = (target, source)
            if day is not None and (key not in days or day > days[key]):
                days[key] = day
    return days


def descriptor_entries() -> dict:
    """Writable-descriptor entries for the created fields.

    They ride the record PATCH inside ``custom_fields`` rather than as their
    own key, which ``container`` says. The projection is present when the
    mapping has a writer, as for a column.
    """
    fields = definitions()
    unit_ids = _unit_concept_ids(fields.values())
    entries = {}
    for name, field in sorted(fields.items()):
        value_kind = _VALUE_KINDS.get(field.field_type, 'string')
        if field.mode == 'computed':
            entries[name] = {
                'kind': 'computed', 'writable': False, 'custom': True,
                'value_kind': value_kind,
                'reason': 'Computed from its formula; not editable here.',
            }
            continue
        if field.mapping.status != 'approved':
            entries[name] = {
                'kind': 'direct', 'writable': False, 'custom': True,
                'value_kind': value_kind,
                'reason': 'This field has no approved mapping yet.',
            }
            continue
        entry = {
            'kind': 'direct', 'writable': True, 'target': 'patient_record',
            'container': CONTAINER, 'custom': True, 'value_kind': value_kind,
        }
        if field.mapping.unit:
            entry['unit'] = field.mapping.unit
        projection = projection_for(field, unit_ids)
        if projection is not None:
            entry['projection'] = {key: value for key, value in projection.items()
                                   if key != 'value_kind'}
        else:
            entry['reason'] = 'Written to the patient record. Its mapping has no OMOP writer.'
        entries[name] = entry
    return entries


def shown_to_patient(names, disease) -> list[str]:
    """Which of the created fields *names* a patient can see on their page and edit (#2857).

    The rule the patient editor applies to render a created field
    (``CustomPatientFields``), run from the definitions, since created fields are
    runtime data and no generated file can list them. The frontend applies the same
    rule to the definitions the page loaded (``attestationBlockers`` in
    ``PatientInfo/provisional.ts``); ``custom_field_tabs`` in
    ``patientVisibleFields.json`` is the one list of placeable tabs they share. A
    field is shown when it is

    - defined, approved and editable (a computed field is its formula's result and a
      patient's PATCH cannot write it; a field without an approved mapping is read-only);
    - placed on a tab a patient page has, which is every tab but ``UNPLACED_TAB``;
    - part of the record: no scope (base), or a Disease Category that is the record's;
    - on the disease tab only when the record has one: a Disease Category, with a
      hand-authored section or a field placed on the disease tab for that category.
    """
    from omop_core.models import CustomPatientField
    from omop_core.services import disease_profiles
    from omop_core.services import patient_visible_fields as visible

    editable = {name: field for name, field in definitions(names).items() if is_editable(field)}
    if not editable:
        return []
    tabs = visible.custom_field_tabs()
    category = disease_profiles.record_category(disease)
    scopes = disease_profiles.memberships()
    disease_tab = None
    shown = []
    for name, field in editable.items():
        diseases = scopes.get(name, ())
        if field.tab not in tabs or (diseases and category not in diseases):
            continue
        if field.tab == 'disease':
            if disease_tab is None:
                disease_tab = bool(category) and (
                    category in visible.DISEASE_TYPE_CATEGORY.values()
                    or CustomPatientField.objects.filter(
                        tab='disease', mapping__status='approved',
                        field_name__in=[n for n, codes in scopes.items() if category in codes],
                    ).exists())
            if not disease_tab:
                continue
        shown.append(name)
    return sorted(shown)
