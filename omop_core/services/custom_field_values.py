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

A **dropdown** field (``field_type='choice'``) holds one of its answers: the
curated ``FieldChoice`` rows under the field's name, the same rows and editor a
built-in coded field uses. The value is the answer's *stored value* (its value,
or its label while it has none, and what it was when the label is renamed), a
write of anything else is refused, and OMOP gets the answer as ``value_as_string``
plus, where the answer has an approved concept, ``value_as_concept_id``
(the ``concept`` value kind of ``project_single_value``). A fact imported at the
field's concept is read back as the answer its text or its concept stands for;
one that matches no answer is not read (``AnswerIndex``).
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
_VALUE_KINDS = {'text': 'string', 'number': 'number', 'date': 'date', 'boolean': 'boolean',
                'choice': 'concept'}
# The OMOP tables a dropdown can be stored in: the two that carry a coded answer.
CHOICE_TABLES = ('measurement', 'observation')
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

    fields = CustomPatientField.objects.active().select_related('mapping__concept')
    if names is not None:
        fields = fields.filter(field_name__in=list(names))
    return {field.field_name: field for field in fields}


def _curated_choices(names, *, with_aliases=False):
    """The answers (``FieldChoice`` rows) of the given created fields, live and retired.

    A row holding decisions about a lookup's entries is not an answer.
    """
    from omop_core.models import FieldChoice

    rows = (FieldChoice.objects.filter(field_name__in=list(names))
            .exclude(context_key__startswith='lookup:')
            .order_by('field_name', 'sort_order', 'display', 'pk'))
    return rows.prefetch_related('aliases') if with_aliases else rows


def answer_values(names) -> dict[str, dict[str, str]]:
    """The values a write may carry, per dropdown: ``{name: {stored value: label}}``.

    Live answers only: a retired answer is no longer offered, though data
    already stored under it still reads (``AnswerIndex``).
    """
    result: dict[str, dict[str, str]] = {name: {} for name in names}
    for choice in _curated_choices(names):
        if choice.retired_at is None:
            result[choice.field_name].setdefault(choice.stored_value, choice.display)
    return result


def options_for(names) -> dict[str, list[dict]]:
    """The answers each dropdown offers, as built-in selects list theirs.

    ``{name: [{value, code, label?}]}``: ``value`` is what a save stores,
    ``code`` the code of the answer's concept (its approved ``value_as_concept``
    decision, else its primary code) or ``None``, ``label`` only where it
    differs from the value. Retired answers are left out.
    """
    from omop_core.models import FieldChoiceCode, FieldValueConceptMapping

    choices = [c for c in _curated_choices(names) if c.retired_at is None]
    codes: dict[int, str] = {}
    if choices:
        ids = [c.pk for c in choices]
        for choice_id, code in (FieldChoiceCode.objects.filter(choice_id__in=ids, is_primary=True)
                                .order_by('-pk').values_list('choice_id', 'code')):
            codes[choice_id] = code
        for choice_id, code in (FieldValueConceptMapping.objects
                                .filter(choice_id__in=ids, role='value_as_concept',
                                        status='approved', outcome='mapped')
                                .values_list('choice_id', 'concept_code')):
            codes[choice_id] = code
    result: dict[str, list[dict]] = {name: [] for name in names}
    seen: set[tuple[str, str]] = set()
    for choice in choices:
        stored = choice.stored_value
        if (choice.field_name, stored) in seen:
            continue
        seen.add((choice.field_name, stored))
        option = {'value': stored, 'code': codes.get(choice.pk)}
        if choice.display != stored:
            option['label'] = choice.display
        result[choice.field_name].append(option)
    return result


class AnswerIndex:
    """What a stored text or concept means as an answer, per dropdown.

    The derive-back of a dropdown: ``value(name, text, concept_id)`` is the
    stored value of the one answer a fact's text (its label, value or an alias,
    case and spacing aside) or, failing that, its ``value_as_concept_id``
    stands for, or ``None``. A spelling or a concept that two answers of the field
    share is not guessed, and a live answer speaks over a retired one: stored data
    still reads under an answer that has since been retired (``live_only`` leaves
    those out, for a proposal, which is approved by saving its value).
    """

    def __init__(self, names):
        from omop_core.services import field_choices
        from omop_core.services.field_value_mappings import answer_values_by_concept

        self._fold = field_choices.fold
        live: dict[str, dict[str, set]] = {}
        retired: dict[str, dict[str, set]] = {}
        self._live_values: dict[str, set] = {name: set() for name in names}
        self._stored: dict[str, set] = {name: set() for name in names}
        for choice in _curated_choices(names, with_aliases=True):
            tier = retired if choice.retired_at else live
            self._stored[choice.field_name].add(choice.stored_value)
            if not choice.retired_at:
                self._live_values[choice.field_name].add(choice.stored_value)
            spellings = {choice.display, choice.stored_value,
                         *(alias.alias for alias in choice.aliases.all())}
            for text in spellings:
                tier.setdefault(choice.field_name, {}).setdefault(
                    self._fold(text), set()).add(choice.stored_value)
        self._text: dict[str, dict[str, str]] = {}
        for name in names:
            merged = {**retired.get(name, {}), **live.get(name, {})}
            self._text[name] = {key: next(iter(values)) for key, values in merged.items()
                                if len(values) == 1}
        self._concept = answer_values_by_concept(list(names))

    def value(self, name, text=None, concept_id=None, *, live_only=False):
        found = None
        if text and text in self._stored.get(name, ()):
            # What a save stores is exact, whatever another answer's label spells.
            found = text
        elif text:
            found = self._text.get(name, {}).get(self._fold(text))
        if found is None and concept_id:
            found = self._concept.get(name, {}).get(concept_id)
        if found is not None and live_only and found not in self._live_values.get(name, ()):
            # A proposal is approved by saving its value, and only a live answer can be saved.
            return None
        return found


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
    answers = None   # read once, and only when a dropdown is edited
    for name, raw in payload.items():
        field = fields.get(name)
        if field is None or not is_editable(field):
            continue
        try:
            value = coerce(field.field_type, raw)
        except ValueError as error:
            raise CustomValueError(name, str(error)) from None
        if not values_match(field.field_type, stored.get(name), value):
            if field.field_type == 'choice' and not is_empty(value):
                if answers is None:
                    answers = answer_values(
                        [n for n, f in fields.items() if f.field_type == 'choice'])
                if value not in answers.get(name, {}):
                    raise CustomValueError(name, _not_an_answer(answers.get(name, {})))
            edits[name] = value
    return edits


def _not_an_answer(offered: dict) -> str:
    shown = ', '.join(sorted(offered)[:12]) + (', ...' if len(offered) > 12 else '')
    return f'Choose one of the answers: {shown}.' if offered else 'This dropdown has no answers to choose.'


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


def projection_for(field, unit_concept_ids=None, concept_maps=None) -> dict | None:
    """The OMOP recipe a created field's value is written with, or ``None``.

    The same shape ``project_single_value`` takes from the writable descriptor.
    A dropdown's carries ``value_concept_map``, ``{spelling: concept id}`` from the
    answers' approved decisions (``concept_maps`` when the caller read them for
    several fields at once), and has no recipe in a table that holds no coded answer.
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
    if field.field_type == 'choice':
        if target not in CHOICE_TABLES:
            return None
        projection['value_concept_map'] = dict(
            (concept_maps if concept_maps is not None else _answer_concepts([field.field_name]))
            .get(field.field_name, {}))
    return projection


def _answer_concepts(names) -> dict:
    from omop_core.services.field_value_mappings import answer_concept_ids

    return answer_concept_ids(list(names)) if names else {}


def _choice_names(fields) -> list[str]:
    return [f.field_name for f in fields if f.field_type == 'choice']


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

    edited = [fields[name] for name in edits if name in fields]
    unit_ids = _unit_concept_ids(edited)
    concept_maps = _answer_concepts(_choice_names(edited))
    projected = set()
    for name, value in edits.items():
        field = fields.get(name)
        projection = projection_for(field, unit_ids, concept_maps) if field is not None else None
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


def read_value(field_type: str, row, *, entered: bool, answers=None, name=None):
    """A created field's value from the OMOP row that answers it.

    ``entered`` says the row was written by the record editor under the
    field's own source key, so it carries the answer in its value columns
    whatever the type. An imported row answers a boolean by existing and a
    date by its own date. A dropdown reads as the stored value of the answer
    the row's text or concept stands for (``answers``, an ``AnswerIndex``, and
    the field's ``name``), and as nothing when it stands for none. Values are
    JSON-ready: a number, an ISO date, text.
    """
    number = getattr(row, 'value_as_number', None)
    text = getattr(row, 'value_as_string', None)
    if field_type == 'choice':
        # Only an answer the field has: the row's text, else its answer concept. A row the
        # editor wrote carried a live answer when it was saved; if the answer was deleted
        # since, the text it stored is still what was entered, and shows as it is.
        if answers is None:
            return None
        found = answers.value(
            name, text, getattr(row, 'value_as_concept_id', None),
            live_only=getattr(row, 'confidence', None) is not None)
        if found is None and entered and text:
            return text
        return found
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
    choice_names = _choice_names(fields.values())
    concept_maps = _answer_concepts(choice_names)
    options = options_for(choice_names) if choice_names else {}
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
        if field.field_type == 'choice':
            entry['options'] = options.get(name, [])
            entry['multiple'] = False
        projection = projection_for(field, unit_ids, concept_maps)
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
                    or CustomPatientField.objects.active().filter(
                        tab='disease', mapping__status='approved',
                        field_name__in=[n for n, codes in scopes.items() if category in codes],
                    ).exists())
            if not disease_tab:
                continue
        shown.append(name)
    return sorted(shown)
