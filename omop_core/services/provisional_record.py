"""The provisional picture of a patient record (docs/provisional_data_plan.md, #2531).

``patient_record`` holds approved data only. Beside it, ``patient_record_provisional``
holds the best-known picture, with the same column names, and
``patient_record_confidence`` holds one percentage per field whose value there
came from a proposed mapping rather than an approved one.

The pass runs after the real record is saved, in the same transaction:

1. Start from every column of the real record, so a reader needs one table.
2. Run the phase 1 readers (labs and blood counts, assertions, curated field
   mappings) over a snapshot made only of the person's provisional rows.
3. Keep a value only for a field the real record leaves empty and that is not
   a pending user edit. Real beats provisional, and an edit is real.
4. Record the confidence of each kept value: the lowest confidence among the
   source codes that decide it.

A reader sees the picture through ``overlay``: the normal patient-record read lays
each proposed value over the field the real record leaves empty and says which
fields those are, so no one asks for it separately.

Two composite readers also run, over provisional conditions and drug exposures
only (#2534), each with the same rule (a value only where the record is empty,
the lowest confidence among the rows that decide it):

- the disease fields (``disease``, ``disease_slug``, ``diagnosis_date``,
  ``diagnosis_date_precision``, ``condition_clinical_status``), from the cancer
  the real reader picks among the provisional conditions, and only when the
  record has no disease yet or names the same one;
- ``concomitant_medications``, from the provisional exposures, through the
  real reader's own ``recent_medications``.

The other composite readers are not run, and those columns equal
``patient_record``: therapy lines read persisted Episodes, which a proposal must
not create; staging, cytogenetics, SCT and cancer details read curator-written
observation keys, not proposal-fed concepts; and the fields of
``active_malignancies``, ``disease_slugs``, ``genetic_mutations`` and
``prior_procedures`` are JSON, which has no confidence column. Computed fields
(BMI, scores) are recomputed from a provisional input only for the inputs in
``COMPUTED_INPUTS``.
"""
from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict

from django.conf import settings
from django.db import transaction

from omop_core.models import (
    Observation, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
)

logger = logging.getLogger(__name__)

# Finding which source codes decide a field re-reads the provisional rows once
# per code group. A person with more groups than this gets the lowest
# confidence among all of them on every provisional field instead: never an
# over-statement, and the cost stays bounded.
MAX_ATTRIBUTION_GROUPS = 40


def _cloned_fields():
    return [f for f in PatientRecordProvisional._meta.concrete_fields if f.name != 'person']


def _confidence_names() -> frozenset[str]:
    return frozenset(f.name for f in PatientRecordConfidence._meta.concrete_fields if f.name != 'person')


def _view(snapshot, measurements, observations, lab_routing_cache=None):
    """A snapshot of only the given provisional rows, readable by the real extractors."""
    from omop_core.services.patient_record_service import _code_source_indexes
    meas_by_code, meas_by_source, obs_by_code, obs_by_source = _code_source_indexes(
        measurements, observations)
    return dataclasses.replace(
        snapshot,
        measurements=measurements, observations=observations,
        conditions=snapshot.provisional_conditions,
        drug_exposures=snapshot.provisional_drug_exposures,
        procedures=snapshot.provisional_procedures,
        death=None, death_date_assertion=None, cancer_detail_rows=[],
        cytogenetic_observations=[], breast_measurements=[], clinical_input_observations=[],
        meas_by_code=meas_by_code, meas_by_source=meas_by_source,
        obs_by_code=obs_by_code, obs_by_source=obs_by_source,
        # Per-refresh memos describe the real rows; these are a different set.
        # The lab routing is read from the curation tables once per pass and
        # shared by the re-reads below, which only ever drop rows from it.
        lab_routing_cache={} if lab_routing_cache is None else lab_routing_cache,
        genomics_cache={},
    )


def _read_values(person, record, view) -> dict:
    """The phase 1 readers over *view*, run exactly as the real refresh runs them.

    The readers and their order come from the real refresh's own list, and the
    curated-mapping step is the helper the refresh calls, so the two cannot
    drift apart (#2646).
    """
    from omop_core.services.patient_record_service import (
        _curated_values, _get_laboratory_data, provisional_section_readers,
    )
    values, lab_derived = {}, set()
    for section_fn in provisional_section_readers():
        section_values = section_fn(person, view)
        if section_fn is _get_laboratory_data:
            lab_derived = set(section_values)
        values.update(section_values)
    values.update(_curated_values(record, view, lab_derived, set()))
    return values


def _attribute(person, record, snapshot, kept, measurements, observations, routing) -> dict:
    """The confidence of each kept field: the lowest among the code groups deciding it."""
    from omop_core.services.patient_record_service import _derived_value_matches
    rows = measurements + observations
    lowest = min(row.confidence for row in rows)
    groups = defaultdict(list)
    for row in measurements:
        groups['m', row.measurement_source_value].append(row)
    for row in observations:
        groups['o', row.observation_source_value].append(row)
    if len(groups) > MAX_ATTRIBUTION_GROUPS:
        return {field: lowest for field in kept}
    confidence = {}
    for group in groups.values():
        gone = {id(row) for row in group}
        without = _read_values(person, record, _view(
            snapshot,
            [row for row in measurements if id(row) not in gone],
            [row for row in observations if id(row) not in gone],
            routing,
        ))
        level = min(row.confidence for row in group)
        for field, value in kept.items():
            # A field that changes when a group's rows go is a field they decide.
            if not _derived_value_matches(field, without.get(field), value):
                confidence[field] = min(confidence.get(field, 100), level)
    # Not decided by any one group (several codes agree): the lowest of all.
    for field in kept:
        confidence.setdefault(field, lowest)
    return confidence


# The calculated fields a provisional input can move, and the stored fields
# each is calculated from (#2661). A field is listed only when it has a
# confidence column; ``_compute_derived_fields`` and ``combine_b_symptoms``
# are the real record's own rules, run over a copy. Left for later: the
# fields calculated from a JSON input (no confidence column for it), the free
# light chain pair, which is read only from one lab event, the age, the
# therapy dates, and fields owned by an admin formula.
COMPUTED_INPUTS = {
    'bmi': ('weight', 'height', 'weight_units', 'height_units'),
    'tnbc_status': ('estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status'),
    'hr_status': ('estrogen_receptor_status', 'progesterone_receptor_status'),
    'metastatic_status': ('distant_metastasis_stage',),
    'renal_adequacy_status': ('egfr_ml_min_173m2', 'serum_creatinine_mg_dl'),
    'no_active_infection_status': ('active_infection_status',),
    'b_symptoms': ('b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss'),
}


def computed_proposals(record, proposals, marks, blocked=()) -> tuple[dict, dict]:
    """(values, confidences) of the calculated fields the *proposals* make possible.

    The real record's calculations run over a copy of *record* with the
    proposals laid on it, so nothing is saved and ``patient_record`` never
    holds a provisional value. A calculated field is provisional only when the
    record has no answer for it, nobody has typed one, a proposal is among its
    inputs, and no reader already proposed it (a measured BMI beats a
    calculated one). Its confidence is the lowest of its provisional inputs.
    """
    import copy
    from omop_core.models import FieldFormula
    from omop_core.services.patient_record_service import (
        _compute_derived_fields, _is_empty, combine_b_symptoms,
    )
    names = _confidence_names()
    wanted = [
        field for field, inputs in COMPUTED_INPUTS.items()
        if field in names and field not in blocked and field not in proposals
        and _is_empty(getattr(record, field, None)) and any(name in proposals for name in inputs)
    ]
    if not wanted:
        return {}, {}
    owned = set(FieldFormula.objects.filter(is_active=True, field_name__in=wanted)
                .values_list('field_name', flat=True))
    wanted = [field for field in wanted if field not in owned]
    if not wanted:
        return {}, {}
    view = copy.copy(record)
    for name, value in proposals.items():
        setattr(view, name, value)
    _compute_derived_fields(view, apply_formulas=False)
    symptoms = [getattr(view, name) for name in COMPUTED_INPUTS['b_symptoms']]
    if any(value is not None for value in symptoms):
        view.b_symptoms = combine_b_symptoms(symptoms)
    values, confidences = {}, {}
    for field in wanted:
        value = getattr(view, field)
        if _is_empty(value) or not _storable(field, value):
            continue
        values[field] = value
        confidences[field] = min(marks[name] for name in COMPUTED_INPUTS[field] if name in proposals)
    return values, confidences


def _storable(name, value) -> bool:
    """Whether *value* fits column *name* (the real refresh drops what does not).

    A proposal is the likeliest source of a wrong-unit number or an overlong
    string, and one such value must not cost the person's other proposals.
    """
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
    from django.db import models
    field = PatientRecord._meta.get_field(name)
    if isinstance(field, models.DecimalField) and value is not None:
        try:
            stored = Decimal(str(value)).quantize(
                Decimal(1).scaleb(-field.decimal_places), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return False
        return stored.is_finite() and stored.adjusted() + 1 <= field.max_digits - field.decimal_places
    width = getattr(field, 'max_length', None)
    return not (width and isinstance(value, str) and len(value) > width)


# The vitals reader takes a weight as kilograms, a height as centimetres (or
# metres), a blood pressure as mmHg and a heart rate per minute. A real row has
# been converted to the mapping's unit; a proposal has not, so it is read only
# when it says it is already in one of those.
_VITAL_UNITS = {
    '29463-7': frozenset({'kg', 'kilogram', 'kilograms'}),
    '8302-2': frozenset({'cm', 'centimeter', 'centimeters', 'centimetre', 'centimetres',
                         'm', 'meter', 'meters', 'metre', 'metres'}),
    '8480-6': frozenset({'mm[hg]', 'mmhg', 'mm hg'}),
    '8462-4': frozenset({'mm[hg]', 'mmhg', 'mm hg'}),
    '8867-4': frozenset({'/min', 'bpm', 'beats/min', 'per minute', '{beats}/min'}),
}


def _vital_code(row):
    """The vital-sign code a measurement is, or None for any other measurement."""
    codes = {row.measurement_source_value, getattr(row.measurement_concept, 'concept_code', None)}
    found = codes & _VITAL_UNITS.keys()
    return next(iter(found), None)


def _usable_vitals(measurements):
    """The measurements, without a vital sign whose newest proposal is in the wrong unit.

    A wrong-unit proposal cannot be read, and an unshowable newest result
    blocks the older ones, as it does on the real path
    (``_read_values_in_source_units``): showing last year's weight as the
    current one would be worse than showing none. So the whole code goes, not
    just the one row. Rows arrive newest first.
    """
    newest_ok = {}
    for row in measurements:
        code = _vital_code(row)
        if code is not None and code not in newest_ok:
            newest_ok[code] = (row.unit_source_value or '').strip().lower() in _VITAL_UNITS[code]
    return [row for row in measurements
            if _vital_code(row) is None or newest_ok[_vital_code(row)]]


# A proposed value is laid beside the record's own unit for it. When the record
# already says it is in another unit, the number would be mislabelled.
_VALUE_UNITS = {'weight': 'weight_units', 'height': 'height_units'}


def _after_clears(snapshot, provisional_rows, target):
    """The provisional rows not hidden by a real clear marker.

    Derivation hides a result, and every older one, behind a later clear
    (``without_cleared_history``). The provisional rows are read apart from the
    real ones, so the real clear markers are laid among them first, newest
    first, and only the provisional rows still visible are kept. A proposal
    older than a user's clear therefore cannot refill it.
    """
    from omop_core.models import Measurement
    from omop_core.services.omop_projection import without_cleared_history
    if not provisional_rows:
        return []
    model = Measurement if target == 'measurement' else Observation
    date_field, id_field = f'{target}_date', f'{target}_id'
    raw = [row for row in snapshot.cancer_detail_rows if isinstance(row, model)]
    merged = sorted(raw + list(provisional_rows),
                    key=lambda row: (getattr(row, date_field), getattr(row, id_field)), reverse=True)
    visible = {id(row) for row in without_cleared_history(merged, target)}
    return [row for row in provisional_rows if id(row) in visible]


# The disease fields one provisional cancer condition decides together. They
# are read as a group: a diagnosis date or a status taken from a different
# cancer than the one the disease names would be a fact about another illness.
DISEASE_FIELDS = (
    'disease', 'disease_slug', 'diagnosis_date', 'diagnosis_date_precision',
    'condition_clinical_status',
)


def _disease_proposals(person, record, snapshot, blocked=()) -> tuple[dict, dict]:
    """(values, confidences) of the disease fields a provisional cancer decides.

    The real reader (``_get_disease_data``) runs over the provisional
    conditions only, and only when one of them is a cancer: with none, its
    fallbacks would take a diagnosis date from a symptom or a status from an
    unrelated condition. The group is offered only where it describes the
    person's own cancer: the record has no disease yet, or names the same one
    (same slug). Each field is then kept on its own rule, only where the record
    is empty. One row decides all of them, the cancer the real reader picks,
    so its confidence is theirs.

    Nothing is offered, and nothing is read, when a person typed the disease or
    its slug (the group would describe another cancer than the one they named),
    or when the record already answers every field of the group. The reader
    fails closed: a typed disease with no slug does not match any proposal's
    slug, so it blocks the group too. The primary cancer is the real ranking's
    choice (not currently resolved, has a line, not in remission, newest),
    whatever the confidence: a 55% row that wins on recency gives every disease
    field 55.
    """
    from omop_core.services.patient_record_service import (
        _cancer_conditions, _get_disease_data, _is_empty, _primary_cancer_condition,
    )
    if 'disease' in blocked or 'disease_slug' in blocked:
        return {}, {}
    if not any(_is_empty(getattr(record, field, None)) and field not in blocked
               for field in DISEASE_FIELDS):
        return {}, {}
    conditions = snapshot.provisional_conditions
    cancers = _cancer_conditions(conditions)
    if not cancers:
        return {}, {}
    # The real observations answer the disease-status fallback; a provisional
    # view has none of its own, so it gets none.
    view = dataclasses.replace(snapshot, conditions=conditions, obs_by_source={}, obs_by_code={})
    primary = _primary_cancer_condition(person, view, cancers)
    read = _get_disease_data(person, view)
    if primary is None or _is_empty(read.get('disease')):
        return {}, {}
    if not _is_empty(record.disease) and record.disease_slug != read.get('disease_slug'):
        return {}, {}
    values = {field: read[field] for field in DISEASE_FIELDS if not _is_empty(read.get(field))}
    return values, {field: primary.confidence for field in values}


def _medication_proposals(snapshot) -> tuple[dict, dict]:
    """(values, confidences) of ``concomitant_medications`` from provisional drug rows.

    The list is the real reader's own (``recent_medications``) over the
    provisional exposures, and carries the lowest confidence among the rows
    that are in it. Therapy lines are not proposed: they come from persisted
    Episodes, which nothing here may create (see the module docstring).
    """
    from omop_core.services.patient_record_service import recent_medications
    names, rows = recent_medications(snapshot.provisional_drug_exposures)
    if not names:
        return {}, {}
    return ({'concomitant_medications': ', '.join(names)},
            {'concomitant_medications': min(row.confidence for row in rows)})


def composite_proposals(person, record, snapshot, blocked=(), taken=()) -> tuple[dict, dict]:
    """(values, confidences) the composite readers add from provisional conditions and drugs.

    Each reader runs over provisional rows only, exactly as the real refresh
    runs it, and a field is kept only where the record has no answer, nobody
    typed one (*blocked*) and no earlier reader proposed it (*taken*: a curated
    mapping's value wins, as it does in the real refresh): real beats
    provisional. Each reader runs in its own savepoint, so one that raises, even
    on a database error, costs only its own fields.
    """
    from omop_core.services.patient_record_service import _is_empty
    names = _confidence_names()
    values, confidences = {}, {}
    for reader in (lambda: _disease_proposals(person, record, snapshot, blocked),
                   lambda: _medication_proposals(snapshot)):
        try:
            with transaction.atomic():
                found, marks = reader()
        except Exception:  # noqa: BLE001 -- one reader must not cost the person's other proposals
            logger.exception('Provisional composite reader failed for person_id=%s.', person.person_id)
            continue
        for field, value in found.items():
            if (field in names and field not in blocked and field not in values and field not in taken
                    and not _is_empty(value) and _is_empty(getattr(record, field, None))
                    and _storable(field, value)):
                values[field], confidences[field] = value, marks[field]
    return values, confidences


def provisional_values(person, record, snapshot, user_edited=()) -> tuple[dict, dict]:
    """(values, confidences) the provisional rows add to *record*, by field name."""
    from omop_core.services.patient_record_service import _is_empty
    measurements = _usable_vitals(_after_clears(snapshot, snapshot.provisional_measurements, 'measurement'))
    observations = _after_clears(snapshot, snapshot.provisional_observations, 'observation')
    confidence_names = _confidence_names()
    blocked = set(user_edited) | set(record.user_edited_fields or [])
    kept, confidences = {}, {}
    if measurements or observations:
        routing = {}
        values = _read_values(person, record, _view(snapshot, measurements, observations, routing))
        # A value is provisional only if its confidence can be recorded, and only
        # where the real record has no answer and nobody has typed one.
        kept = {
            field: value for field, value in values.items()
            if field in confidence_names and field not in blocked
            and not _is_empty(value) and _is_empty(getattr(record, field, None))
            and _storable(field, value)
        }
        # A proposed weight or height is dropped when the record's own unit for it
        # differs from the unit the proposal is in: laid together, the number would
        # read as the wrong unit (70 kg shown as 70 lb).
        for field, units_field in _VALUE_UNITS.items():
            own = (getattr(record, units_field, None) or '').strip().lower()
            proposed = str(values.get(units_field) or '').strip().lower()
            if field in kept and own and proposed and own != proposed:
                del kept[field]
        if kept:
            confidences = _attribute(person, record, snapshot, kept, measurements, observations, routing)
    composite, composite_marks = composite_proposals(person, record, snapshot, blocked, taken=kept)
    if not (kept or composite):
        return {}, {}
    try:
        computed, computed_marks = computed_proposals(record, kept, confidences, blocked)
    except Exception:  # noqa: BLE001 -- a calculation must not cost the person's other proposals
        logger.exception('Provisional calculation failed for person_id=%s.', person.person_id)
        computed, computed_marks = {}, {}
    return ({**kept, **composite, **computed},
            {**confidences, **composite_marks, **computed_marks})


def write_provisional_pair(person, snapshot, record, user_edited=()) -> None:
    """Write the person's provisional and confidence rows from the saved real *record*.

    A person with no provisional clinical rows and no pair already stored has
    nothing to say, so nothing is written (a reader gets the real record for them);
    one who has a stored pair gets it rewritten, which is how an approval or a
    revert takes a proposal back out. A failure is logged and leaves both rows
    as they were: the approved record is never lost to this pass.
    """
    try:
        with transaction.atomic():
            values, confidences = provisional_values(person, record, snapshot, user_edited)
            has_rows = any((snapshot.provisional_measurements, snapshot.provisional_observations,
                            snapshot.provisional_conditions, snapshot.provisional_drug_exposures,
                            snapshot.provisional_procedures))
            if not has_rows and not PatientRecordProvisional.objects.filter(person=person).exists():
                return
            row = {f.attname: getattr(record, f.attname) for f in _cloned_fields()}
            row.update(values)
            marks = {name: confidences.get(name) for name in _confidence_names()}
            for model, fields in ((PatientRecordProvisional, row), (PatientRecordConfidence, marks)):
                if not model.objects.filter(person=person).update(**fields):
                    model.objects.create(person=person, **fields)
    except Exception:  # noqa: BLE001 -- never let the provisional pass lose the real refresh
        logger.exception('Provisional pass failed for person_id=%s; the approved record is saved.',
                         person.person_id)


def mirror_user_edit(record, edited_fields) -> None:
    """Carry a direct edit to ``patient_record`` into the provisional pair.

    A user's edit is approved, so an edited field takes the record's value and
    loses its confidence. Every other field that is approved in the provisional
    row follows the record too (computed fields move with an edit); a field
    still waiting on a proposal keeps its proposal. A person with no
    provisional row yet has nothing to mirror.
    """
    if not settings.PROVISIONAL_DATA_ENABLED:
        return
    provisional = PatientRecordProvisional.objects.filter(person_id=record.person_id).first()
    if provisional is None:
        return
    confidence = PatientRecordConfidence.objects.filter(person_id=record.person_id).first()
    from omop_core.services.patient_record_service import _is_empty
    edited = set(edited_fields)
    # The disease fields describe one cancer together: editing the disease or its
    # slug makes the whole group the person's, so no field is left attributed to
    # the cancer a proposal named.
    if edited & {'disease', 'disease_slug'}:
        edited |= set(DISEASE_FIELDS)
    names = _confidence_names()
    try:
        with transaction.atomic():
            cleared = []
            for field in _cloned_fields():
                mark = getattr(confidence, field.name, None) if confidence is not None else None
                # A proposal stays only while the record has no answer of its own:
                # an edit, or an alias the edit filled in, makes the value approved.
                if mark is None or field.name in edited or not _is_empty(getattr(record, field.attname)):
                    setattr(provisional, field.attname, getattr(record, field.attname))
                    if mark is not None and field.name in names:
                        setattr(confidence, field.name, None)
                        cleared.append(field.name)
            if confidence is not None:
                try:
                    with transaction.atomic():
                        cleared += _settle_computed(record, provisional, confidence, edited)
                except Exception:  # noqa: BLE001 -- a calculation must not cost the edit's own mirror
                    logger.exception('Provisional recalculation failed for person_id=%s.',
                                     record.person_id)
            provisional.save()
            if cleared:
                confidence.save(update_fields=cleared)
    except Exception:  # noqa: BLE001 -- the user's edit is already saved; this is the side picture
        logger.exception('Provisional mirror failed for person_id=%s; the edit is saved.', record.person_id)


def _settle_computed(record, provisional, confidence, edited) -> list[str]:
    """Recalculate the provisional calculated fields after an edit; names touched.

    A calculated field that still waits on a proposal was worked out from
    inputs that an edit may have changed. It is recalculated over the proposals
    that remain; when that gives nothing and an input was edited, the stale
    proposal is dropped in favour of the record's own value. Returns the
    confidence columns written, for the caller's ``update_fields``.
    """
    names = _confidence_names()
    waiting = {f for f in COMPUTED_INPUTS if f in names and getattr(confidence, f) is not None}
    if not waiting:
        return []
    proposals = {f: getattr(provisional, f) for f in names
                 if f not in COMPUTED_INPUTS and getattr(confidence, f) is not None}
    marks = {f: getattr(confidence, f) for f in proposals}
    blocked = set(record.user_edited_fields or []) | edited
    values, level = computed_proposals(record, proposals, marks, blocked)
    touched = []
    for field in waiting:
        if field in values:
            setattr(provisional, field, values[field])
            setattr(confidence, field, level[field])
            touched.append(field)
        elif edited & set(COMPUTED_INPUTS[field]):
            setattr(provisional, field, getattr(record, field))
            setattr(confidence, field, None)
            touched.append(field)
    return touched


# Fields a proposal is never laid over, whether or not a mark exists for them:
# identity and contact, and every field the record serializer redacts or
# withholds for a reader who is not the account holder
# (PatientRecordSerializer.REDACTED_DEMOGRAPHIC_FIELDS and
# WITHHELD_IDENTIFIER_FIELDS; a test holds this set to them). The redaction runs
# on the serializer's output, so an overlaid value here would also dodge the
# consent flag it follows.
NEVER_OVERLAID = frozenset({
    'date_of_birth', 'date_of_birth_precision', 'age', 'patient_age', 'death_date',
    'country', 'region', 'city', 'postal_code', 'longitude', 'latitude', 'patient_name', 'name',
    'email', 'phone_number', 'race', 'ethnicity', 'insurance_type', 'facility_name', 'validated_by',
    'suppress_demographics_for_others', 'consent_capability',
})


def overlay(record):
    """The record as a reader sees it: ``(record, marks)``.

    With ``PROVISIONAL_DATA_ENABLED`` on, a field the live record leaves empty
    takes its proposed value from ``patient_record_provisional``, and ``marks``
    maps each such field to its confidence (a percentage). Everything else is the
    record's own. Only a field with a confidence mark is taken, and only while the
    live record still has no answer for it, so a stale provisional row can never
    override approved data. The record itself is not changed or saved: the caller
    gets a copy to serialize. With the switch off, or no provisional row, it is
    ``(record, {})``.

    Access is the real record's: whoever may read this patient may read what is
    proposed for them, so this adds no check of its own.
    """
    if not settings.PROVISIONAL_DATA_ENABLED:
        return record, {}
    import copy
    from django.core.exceptions import ObjectDoesNotExist
    from omop_core.services.patient_record_service import _is_empty
    person = record.person
    try:
        provisional = person.patient_record_provisional
        confidence = person.patient_record_confidence
    except ObjectDoesNotExist:
        return record, {}
    view, marks = copy.copy(record), {}
    for field in PatientRecordConfidence._meta.concrete_fields:
        if field.name == 'person' or field.name in NEVER_OVERLAID:
            continue
        mark = getattr(confidence, field.name)
        if mark is not None and _is_empty(getattr(record, field.attname, None)):
            setattr(view, field.attname, getattr(provisional, field.attname))
            marks[field.name] = mark
    return view, marks
