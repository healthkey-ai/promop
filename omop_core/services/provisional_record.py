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
observation keys, not proposal-fed concepts. Computed fields (BMI, scores) are
recomputed from a provisional input only for the inputs in ``COMPUTED_INPUTS``.

The list fields (``PROVISIONAL_JSON_FIELDS``: ``disease_slugs``,
``active_malignancies``, ``prior_procedures``, ``genetic_mutations`` and the
``genomics_*`` markers) follow a rule of their own, because the record may
already hold some of the items: real items are never replaced, a provisional item
is added when no real item says the same, and the provisional column stores only
the added items (see ``list_proposals`` and ``overlay``).

The created fields (``custom_fields``, one value per ``CustomPatientField``) follow the
scalar rule key by key (#2857): a provisional row answers a created field the approved
record leaves empty and nobody typed, and ``patient_record_confidence.custom_fields`` is
``{field_name: percent}``. See ``custom_proposals`` and ``custom_field_values.PENDING_PREFIX``.

A user can reject a proposal on a scalar field by clearing it (#2671): the pass
then leaves the field empty while every row that decides it is one that was
rejected (``ProposalRejection``, ``proposal_clears``, ``reject_proposals``,
``held_rejections``).
"""
from __future__ import annotations

import dataclasses
import json
import logging
from collections import defaultdict

from django.conf import settings
from django.db import transaction

from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS, PROVISIONAL_OBJECT_FIELDS
from omop_core.services import custom_field_values
from omop_core.models import (
    Measurement, Observation, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
    ProposalRejection,
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


def _origin(table, source_value, concept_id) -> tuple:
    """One provisional row's identity for a rejection: its table, source code and concept."""
    return (table, source_value or '', concept_id or 0)


def _group_origins(rows, table, source_field, concept_field) -> set:
    return {_origin(table, getattr(row, source_field), getattr(row, concept_field)) for row in rows}


def _row_origins(rows) -> set:
    """``_origin`` triples of provisional measurement and observation rows."""
    return (_group_origins([r for r in rows if isinstance(r, Measurement)], 'measurement',
                           'measurement_source_value', 'measurement_concept_id')
            | _group_origins([r for r in rows if isinstance(r, Observation)], 'observation',
                             'observation_source_value', 'observation_concept_id'))


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


# A rejection is held against the rows that feed its field, found only for the fields
# that have one. The search reads the rows again per code group (about two queries each,
# on every derivation while the hold applies and the person has codes beyond those it was
# made against), so it is bounded like the confidence above: past this many groups the
# fallback is every measurement and observation row, which can only lapse more.
MAX_ORIGIN_GROUPS = MAX_ATTRIBUTION_GROUPS


def _field_origins(person, record, snapshot, kept, fields, measurements, observations, routing) -> dict:
    """``_origin`` triples of the provisional rows that feed each of *fields* (kept values).

    A group of rows (one source code) feeds a field when the field changes without it
    (it decides it) or, when no single group decides it (several codes agree), when that
    group alone gives the same value. Rows of other codes, and of other fields, are not
    among them, so a rejection is not undone by an unrelated proposal. A field no such
    group explains, or a person with more than ``MAX_ORIGIN_GROUPS`` groups, falls back
    to every provisional measurement and observation row: it can only lapse more.
    """
    from omop_core.services.patient_record_service import _derived_value_matches
    fields = [field for field in fields if field in kept]
    if not fields:
        return {}
    groups = defaultdict(list)
    for row in measurements:
        groups['m', row.measurement_source_value].append(row)
    for row in observations:
        groups['o', row.observation_source_value].append(row)
    everything = _row_origins(measurements + observations)
    if len(groups) > MAX_ORIGIN_GROUPS:
        return {field: set(everything) for field in fields}
    origins = {}
    for group in groups.values():
        gone = {id(row) for row in group}
        without = _read_values(person, record, _view(
            snapshot,
            [row for row in measurements if id(row) not in gone],
            [row for row in observations if id(row) not in gone],
            routing,
        ))
        for field in fields:
            if not _derived_value_matches(field, without.get(field), kept[field]):
                origins.setdefault(field, set()).update(_row_origins(group))
    undecided = [field for field in fields if field not in origins]
    if undecided:
        for group in groups.values():
            alone = _read_values(person, record, _view(
                snapshot,
                [row for row in group if isinstance(row, Measurement)],
                [row for row in group if isinstance(row, Observation)],
                routing,
            ))
            for field in undecided:
                if _derived_value_matches(field, alone.get(field), kept[field]):
                    origins.setdefault(field, set()).update(_row_origins(group))
    return {field: origins.get(field) or set(everything) for field in fields}


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


# A value that says "no answer": the activity trend's "insufficient_data" is
# what derivation writes when it has readings and no trend. It is not a
# proposal worth showing, and an approved one is not an answer a proposed
# trend has to stay behind (#2835).
PLACEHOLDERS = {'activity_trend_30d': 'insufficient_data'}


def _no_answer(field, value) -> bool:
    from omop_core.services.patient_record_service import _is_empty
    return _is_empty(value) or PLACEHOLDERS.get(field, object()) == value


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


def _provisional_disease(person, snapshot, shared):
    """``(cancers, primary, read)`` of the provisional cancers, or None when there is none.

    The real reader runs over the provisional conditions only. The scalar
    disease group and the ``disease_slugs`` list both need it, so one pass of the
    refresh reads it once (*shared* is that pass's memo).
    """
    if shared is not None and 'disease' in shared:
        return shared['disease']
    from omop_core.services.patient_record_service import (
        _cancer_conditions, _get_disease_data, _primary_cancer_condition,
    )
    found = None
    cancers = _cancer_conditions(snapshot.provisional_conditions)
    if cancers:
        # The real observations answer the disease-status fallback; a provisional
        # view has none of its own, so it gets none.
        view = dataclasses.replace(snapshot, conditions=snapshot.provisional_conditions,
                                   obs_by_source={}, obs_by_code={})
        found = (cancers, _primary_cancer_condition(person, view, cancers),
                 _get_disease_data(person, view))
    if shared is not None:
        shared['disease'] = found
    return found


def _disease_proposals(person, record, snapshot, blocked=(), shared=None, origins=None) -> tuple[dict, dict]:
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
    from omop_core.services.patient_record_service import _is_empty
    if 'disease' in blocked or 'disease_slug' in blocked:
        return {}, {}
    if not any(_is_empty(getattr(record, field, None)) and field not in blocked
               for field in DISEASE_FIELDS):
        return {}, {}
    disease = _provisional_disease(person, snapshot, shared)
    if disease is None:
        return {}, {}
    _, primary, read = disease
    if primary is None or _is_empty(read.get('disease')):
        return {}, {}
    if not _is_empty(record.disease) and record.disease_slug != read.get('disease_slug'):
        return {}, {}
    values = {field: read[field] for field in DISEASE_FIELDS if not _is_empty(read.get(field))}
    if origins is not None:
        row = _origin('condition_occurrence', primary.condition_source_value, primary.condition_concept_id)
        origins.update({field: {row} for field in values})
    return values, {field: primary.confidence for field in values}


def _medication_proposals(snapshot, origins=None) -> tuple[dict, dict]:
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
    if origins is not None:
        origins['concomitant_medications'] = _group_origins(
            rows, 'drug_exposure', 'drug_source_value', 'drug_concept_id')
    return ({'concomitant_medications': ', '.join(names)},
            {'concomitant_medications': min(row.confidence for row in rows)})


def composite_proposals(person, record, snapshot, blocked=(), taken=(), shared=None,
                        origins=None) -> tuple[dict, dict]:
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
    for reader in (lambda: _disease_proposals(person, record, snapshot, blocked, shared, origins),
                   lambda: _medication_proposals(snapshot, origins)):
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


# --- List fields (#2534) ------------------------------------------------------------
#
# A list field (prior_procedures, genetic_mutations, ...) is not "empty or not":
# the real record may hold some items and the provisional rows add others. So
# real items are never replaced; a provisional item is added to the list, and
# only when no real item says the same thing (``_item_key``). In
# ``patient_record_provisional`` such a column holds only the *added* items,
# never a copy of the real ones, so it can never go stale against the record: the
# reader lays the live items first and the stored ones after
# (``overlay``), and an item the record has since gained stops being provisional
# by itself. The one confidence is the lowest among the added items; it is not
# stored per item, because the record shows one indicator per field and the
# lowest is never an over-statement. A list a person edited (``blocked``) is
# never added to.
_PROCEDURE_IDENTITY = ('concept_id', 'procedure', 'date')
_VARIANT_IDENTITY = ('gene', 'variant', 'genomic_feature', 'marker_key', 'test_date')


def _fold(value):
    """Text compares without case or edge whitespace (the reader writes ``tp53``, an editor ``TP53``)."""
    return value.strip().casefold() if isinstance(value, str) else value


def _item_key(field, item) -> str:
    """What makes two items of list *field* the same item, whoever asserted them."""
    if isinstance(item, dict):
        if field == 'prior_procedures':
            ident = (item.get('concept_id') or _fold(item.get('procedure')), item.get('date'))
        else:
            ident = tuple(_fold(item.get(name)) for name in _VARIANT_IDENTITY)
        return json.dumps(ident, default=str)
    return str(item).strip().casefold()


def _unseen(field, live, stored) -> list:
    """The *stored* items of list *field* that the *live* list does not already hold."""
    if not isinstance(stored, list):
        return []
    have = {_item_key(field, item) for item in (live if isinstance(live, list) else ())}
    unseen = []
    for item in stored:
        key = _item_key(field, item)
        if key not in have:
            have.add(key)
            unseen.append(item)
    return unseen


def _added_items(field, live, candidates) -> tuple[list, int | None]:
    """(items to add, the lowest of their confidences) for list *field*.

    *candidates* are ``(item, confidence)`` pairs; an item the record already
    holds is dropped, and one given by several rows keeps the lowest.
    """
    have = {_item_key(field, item) for item in (live if isinstance(live, list) else ())}
    levels, added = {}, {}
    for item, level in candidates:
        key = _item_key(field, item)
        if key in have:
            continue
        added.setdefault(key, item)
        levels[key] = min(levels.get(key, 100), level)
    return list(added.values()), (min(levels.values()) if levels else None)


def without_proposed(field, incoming, proposed_items):
    """*incoming*, a list a client sent back, without the items that are proposals for *field*.

    A client that saves the whole record sends the merged list it read; the
    proposed items in it must not become the record's own.
    """
    if not isinstance(incoming, list) or not proposed_items:
        return incoming
    proposed = {_item_key(field, item) for item in proposed_items}
    return [item for item in incoming if _item_key(field, item) not in proposed]


def _disease_list_candidates(person, snapshot, blocked=(), shared=None):
    """``disease_slugs``: the real reader over provisional conditions, one slug per cancer.

    Like the scalar disease group, nothing is offered when the person typed the
    disease or its slug (the list would name cancers beside a disease they chose).
    """
    from omop_core.services.patient_record_service import _condition_slug
    if 'disease' in blocked or 'disease_slug' in blocked:
        return {}
    disease = _provisional_disease(person, snapshot, shared)
    if disease is None:
        return {}
    cancers, primary, read = disease
    level = {}
    for condition in cancers:
        slug = _condition_slug(condition)
        level[slug] = min(level.get(slug, 100), condition.confidence)
    return {'disease_slugs': [
        (slug, level.get(slug, primary.confidence if primary is not None else min(level.values())))
        for slug in read.get('disease_slugs', ())
    ]}


def _malignancy_candidates(person, snapshot, blocked=(), shared=None):
    """``active_malignancies``: the real reader's current-condition rule over provisional conditions."""
    from django.utils import timezone
    from omop_core.services.patient_record_service import _condition_is_current, malignancy_rows
    today = timezone.localdate()
    current = [c for c in snapshot.provisional_conditions if _condition_is_current(c, today)]
    named = malignancy_rows(current) if current else None
    if not named:
        return {}
    return {'active_malignancies': [
        (name, min(row.confidence for row in rows)) for name, rows in sorted(named.items())
    ]}


def _procedure_candidates(person, snapshot, blocked=(), shared=None):
    """``prior_procedures``: the real reader's item for each provisional procedure."""
    from omop_core.services.patient_record_service import procedure_item
    return {'prior_procedures': [
        (procedure_item(proc), proc.confidence) for proc in snapshot.provisional_procedures
    ]}


def _genomic_candidates(person, snapshot, blocked=(), shared=None):
    """``genetic_mutations`` and the ``genomics_*`` markers from provisional findings.

    The real reader runs over the provisional measurements and observations
    alone. A finding's confidence is the lowest among its own row and the
    component rows linked to it (allele fraction, interpretation, ...). A
    marker the reader *derives* from findings is not offered: a derivation over
    only the proposed parents says nothing about the others.
    """
    from omop_core.services.patient_record_service import _get_genetic_mutations
    measurements = snapshot.provisional_measurements
    if not measurements:
        return {}
    view = _view(snapshot, measurements, snapshot.provisional_observations)
    read = _get_genetic_mutations(person, view)
    level = {row.measurement_id: row.confidence for row in measurements}
    for row in measurements:
        parent = row.measurement_event_id
        if parent in level:
            level[parent] = min(level[parent], row.confidence)
    for row in snapshot.provisional_observations:
        parent = row.observation_event_id
        if parent in level:
            level[parent] = min(level[parent], row.confidence)
    candidates = {}
    for field in PROVISIONAL_JSON_FIELDS:
        found = [(item, level[item['id']]) for item in read.get(field, ())
                 if isinstance(item, dict) and item.get('provenance') != 'derived' and item.get('id') in level]
        if found:
            candidates[field] = found
    return candidates


_LIST_READERS = (_disease_list_candidates, _malignancy_candidates, _procedure_candidates,
                 _genomic_candidates)


def list_proposals(person, record, snapshot, blocked=(), shared=None) -> tuple[dict, dict]:
    """(values, confidences) of the list fields the provisional rows add to *record*.

    Each reader runs over provisional rows only, in its own savepoint, so one
    that raises, even on a database error, costs only its own fields. A value is
    the items to add (see above), never the whole list.
    """
    names = _confidence_names()
    values, confidences = {}, {}
    for reader in _LIST_READERS:
        try:
            with transaction.atomic():
                candidates = reader(person, snapshot, blocked, shared)
        except Exception:  # noqa: BLE001 -- one reader must not cost the person's other proposals
            logger.exception('Provisional list reader %s failed for person_id=%s.',
                             reader.__name__, person.person_id)
            continue
        for field, found in candidates.items():
            if field not in names or field in blocked or field in values:
                continue
            added, level = _added_items(field, getattr(record, field, None), found)
            if added:
                values[field], confidences[field] = added, level
    return values, confidences


# --- Created fields (#2857) ----------------------------------------------------------
#
# A created field (``CustomPatientField``) has no column, so its confidence cannot
# be one either: ``patient_record_confidence.custom_fields`` is an object,
# ``{field_name: percent}``, for the person's provisional created values, and
# ``patient_record_provisional.custom_fields`` is the picture of the values (the
# approved ones, and a proposal's value where only a proposal answers). A created
# field is a scalar, so it follows the scalar rule key by key: a proposal fills a
# field the approved record leaves empty and nobody typed (``user_edited_fields``
# ``custom_fields.<name>``, an explicit clear included), real beats provisional, and
# the newest fact answers (``patient_record_service.custom_field_answers``, the
# reader the approved record uses, run over provisional rows only). A computed field
# is its formula's result, and a field whose mapping is not approved has no reader,
# so neither is proposed. The record read reports a created field's mark in
# ``provisional_fields`` as ``custom_fields.<name>``, the name a pending edit has in
# ``user_edited_fields``. A user rejects such a proposal as they reject a column's
# (#2671): the rejection is kept under the same name (``ProposalRejection.field_name``
# ``custom_fields.<name>``), with the origin of the row that answered, and held by
# ``custom_proposals`` until a different code or concept answers.


def _object_marks(value) -> dict:
    """A stored confidence object (``custom_fields``) as a dict; anything else is no marks."""
    return value if isinstance(value, dict) else {}


def _row_origin(row) -> tuple:
    """``_origin`` of any provisional clinical row: its table, source code and concept."""
    for table, source, concept in (
            ('measurement', 'measurement_source_value', 'measurement_concept_id'),
            ('observation', 'observation_source_value', 'observation_concept_id'),
            ('condition_occurrence', 'condition_source_value', 'condition_concept_id'),
            ('drug_exposure', 'drug_source_value', 'drug_concept_id'),
            ('procedure_occurrence', 'procedure_source_value', 'procedure_concept_id')):
        if hasattr(row, concept):
            return _origin(table, getattr(row, source, None), getattr(row, concept))
    return _origin('', None, None)


def custom_proposals(person, record, snapshot, blocked=(), measurements=None, observations=None,
                     rejections=None, origins=None):
    """(values, confidences) of the created fields the provisional rows answer.

    ``values`` is ``{'custom_fields': {name: value}}`` and ``confidences`` is
    ``{'custom_fields': {name: percent}}``: only the proposals, never a copy of the
    approved values (the caller lays them over the record's own). A name is
    proposed only when the approved record has no value for it, nobody has typed one
    or cleared it (*blocked* holds the pending edits, ``custom_fields.<name>``), and
    a provisional row answers it; its confidence is that row's.

    *rejections* maps ``custom_fields.<name>`` to the ``_origin`` triples a user
    rejected it against (``ProposalRejection``): a name is left out while the row that
    answers it now is one of them. *origins*, when a dict, is filled with the answering
    row's origin per ``custom_fields.<name>``, which is what a rejection is made against.
    """
    from omop_core.services.patient_record_service import custom_field_answers
    if measurements is None:
        measurements = _after_clears(snapshot, snapshot.provisional_measurements, 'measurement')
    if observations is None:
        observations = _after_clears(snapshot, snapshot.provisional_observations, 'observation')
    answers = custom_field_answers(_view(snapshot, measurements, observations), person)
    approved = record.custom_fields or {}
    values, confidences = {}, {}
    for name, (value, row) in answers.items():
        if (custom_field_values.pending_key(name) in blocked
                or not custom_field_values.is_empty(approved.get(name))
                or custom_field_values.is_empty(value) or row.confidence is None):
            continue
        mark = custom_field_values.pending_key(name)
        origin = _row_origin(row)
        if origins is not None:
            origins[mark] = {origin}
        if rejections and mark in rejections and origin in rejections[mark]:
            continue
        values[name], confidences[name] = value, row.confidence
    if not values:
        return {}, {}
    container = custom_field_values.CONTAINER
    return {container: values}, {container: confidences}


def provisional_values(person, record, snapshot, user_edited=(), origins=None,
                       hold_rejections=True, origin_fields=()) -> tuple[dict, dict]:
    """(values, confidences) the provisional rows add to *record*, by field name.

    A field the user rejected (``ProposalRejection``) is left out while the
    rejection holds (``held_rejections``); ``hold_rejections=False`` reads the
    proposals as if none had been rejected, which is how a rejection finds what it
    is made against. *origins*, when a dict, is filled with the rows that feed the
    proposed fields (``_origin`` triples): the composite fields always, the others only
    those of *origin_fields* and of a field the person has rejected, since finding them
    reads the rows again per code group.
    """
    from omop_core.services.patient_record_service import canonical_disease_names
    if not _has_provisional_rows(snapshot):
        return {}, {}
    from omop_core.services.patient_record_service import _usable_concept_name
    names = {name for c in snapshot.provisional_conditions
             for name in (_usable_concept_name(c.condition_concept) if c.condition_concept_id else None,
                          c.condition_source_value)}
    with canonical_disease_names(names):
        return _provisional_values(person, record, snapshot, user_edited, origins, hold_rejections,
                                   origin_fields)


# Fields that go with a rejected one: a rejected weight leaves no unit to show, and
# the disease and its slug describe one cancer, so rejecting either rejects the
# group (the same grouping a typed edit has, ``mirror_user_edit``).
REJECTION_FOLLOWERS = {
    'weight': ('weight_units',),
    'height': ('height_units',),
    'disease': (*DISEASE_FIELDS, 'disease_slugs'),
    'disease_slug': (*DISEASE_FIELDS, 'disease_slugs'),
    # A date and its precision are one answer (``_PRECISION``).
    'diagnosis_date': ('diagnosis_date_precision',),
    'sct_date': ('sct_date_precision',),
    'dlbcl_transformation_date': ('dlbcl_transformation_date_precision',),
}

_PRECISION = '_precision'


def _date_of(precision_field):
    """The date a ``<date>_precision`` field describes, or None for any other field."""
    if precision_field.endswith(_PRECISION) and precision_field[:-len(_PRECISION)] in _confidence_names():
        return precision_field[:-len(_PRECISION)]
    return None


def rejection_group(fields) -> set[str]:
    """*fields* and every field that goes with a rejection of them.

    Besides ``REJECTION_FOLLOWERS``, a lab's aliases and their unit follow the
    canonical field (the page shows ``hemoglobin_level`` beside
    ``hemoglobin_g_dl``, marked as the same proposal).
    """
    from omop_core.services.patient_record_service import _LAB_FIELD_ALIASES, _lab_alias_units
    group = set(fields)
    for field in list(group):
        group.update(REJECTION_FOLLOWERS.get(field, ()))
        group.update(_LAB_FIELD_ALIASES.get(field, ()))
        group.update(_lab_alias_units(field, 1))
    return group


def held_rejections(rejections, origins) -> set[str]:
    """The fields the user rejected whose rejection still holds: leave them empty.

    *rejections* maps a rejected field to the ``_origin`` triples that fed it then;
    *origins* maps a field to those that feed it now. A rejection holds while every
    row that feeds the field now is one that fed it when it was rejected: the same
    source code under the same concept. A new reading of that code stays rejected. A
    code now mapped to another concept, or another code that now feeds the field, is a
    new proposal and shows again. A field nothing proposes now has nothing to hold. The
    fields that go with a held one are held too.
    """
    held = {field for field, stored in rejections.items()
            if field in origins and origins[field] <= stored}
    return rejection_group(held)


def _provisional_values(person, record, snapshot, user_edited=(), origins=None,
                        hold_rejections=True, origin_fields=()) -> tuple[dict, dict]:
    from omop_core.services.patient_record_service import _is_empty
    shared = {}
    origins = {} if origins is None else origins
    rejections = {}
    if hold_rejections and settings.PROVISIONAL_DATA_ENABLED:
        rejections = {r.field_name: {tuple(origin) for origin in r.origins or []}
                      for r in ProposalRejection.objects.filter(person=person)}
    origin_fields = set(origin_fields) | set(rejections)
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
            and not _no_answer(field, value) and _no_answer(field, getattr(record, field, None))
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
            need = origin_fields & kept.keys()
            if need:
                everything = _row_origins(measurements + observations)
                # A held rejection costs nothing when every provisional row the person has
                # is one it was made against: whatever feeds the field is among them. Only
                # a person with other codes pays for the search for the feeding rows.
                settled = {f for f in need if f in rejections and everything <= rejections[f]}
                origins.update({f: set(everything) for f in settled})
                if need - settled:
                    origins.update(_field_origins(person, record, snapshot, kept, need - settled,
                                                  measurements, observations, routing))
    composite, composite_marks = composite_proposals(person, record, snapshot, blocked, taken=kept,
                                                     shared=shared, origins=origins)
    if rejections:
        # A field the user rejected stays empty while the rejection holds (#2671).
        # Dropping it here, before the lists and calculated fields are read, takes
        # a calculation made from it (BMI from a rejected weight) with it.
        held = held_rejections(rejections, {f: o for f, o in origins.items() if f in kept or f in composite})
        for field in held:
            for proposals, marks in ((kept, confidences), (composite, composite_marks)):
                proposals.pop(field, None)
                marks.pop(field, None)
        blocked = blocked | held
    listed, listed_marks = list_proposals(person, record, snapshot, blocked, shared)
    try:
        with transaction.atomic():
            created, created_marks = custom_proposals(
                person, record, snapshot, blocked, measurements, observations,
                rejections=rejections, origins=origins)
    except Exception:  # noqa: BLE001 -- the created fields must not cost the person's other proposals
        logger.exception('Provisional created-field reader failed for person_id=%s.', person.person_id)
        created, created_marks = {}, {}
    if not (kept or composite or listed or created):
        return {}, {}
    try:
        computed, computed_marks = computed_proposals(record, kept, confidences, blocked)
    except Exception:  # noqa: BLE001 -- a calculation must not cost the person's other proposals
        logger.exception('Provisional calculation failed for person_id=%s.', person.person_id)
        computed, computed_marks = {}, {}
    return ({**kept, **composite, **computed, **listed, **created},
            {**confidences, **composite_marks, **computed_marks, **listed_marks, **created_marks})


def _has_provisional_rows(snapshot) -> bool:
    """Whether the person has any provisional clinical row (nothing to read when not)."""
    return any((snapshot.provisional_measurements, snapshot.provisional_observations,
                snapshot.provisional_conditions, snapshot.provisional_drug_exposures,
                snapshot.provisional_procedures))


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
            has_rows = _has_provisional_rows(snapshot)
            if not has_rows and not PatientRecordProvisional.objects.filter(person=person).exists():
                return
            row = {f.attname: getattr(record, f.attname) for f in _cloned_fields()}
            row.update({field: value for field, value in values.items()
                        if field not in PROVISIONAL_OBJECT_FIELDS})
            for field in PROVISIONAL_OBJECT_FIELDS:
                # The picture of an object field is the approved object with the
                # proposed keys added; the confidence says which keys those are.
                if field in values:
                    row[field] = {**(getattr(record, field, None) or {}), **values[field]}
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
        edited |= {*DISEASE_FIELDS, 'disease_slugs'}
    names = _confidence_names()
    # A date and its precision are one answer: editing the date takes the precision's
    # proposal back too, or a proposed "year" would be shown against the new date.
    edited |= {f'{field}{_PRECISION}' for field in list(edited) if f'{field}{_PRECISION}' in names}
    try:
        with transaction.atomic():
            cleared = []
            for field in _cloned_fields():
                mark = getattr(confidence, field.name, None) if confidence is not None else None
                if field.name in PROVISIONAL_OBJECT_FIELDS:
                    if _mirror_created_fields(record, provisional, confidence, field.name, edited):
                        cleared.append(field.name)
                    continue
                if mark is not None and field.name in PROVISIONAL_JSON_FIELDS and field.name not in edited:
                    # The added items stay until the field is edited; the reader
                    # drops any the record now holds (``overlay``).
                    continue
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


def _mirror_created_fields(record, provisional, confidence, name, edited) -> bool:
    """Carry the record's created-field values into the provisional pair after an edit.

    The picture is the approved object, with a proposal's value kept for each key
    that still waits on one: the record has no value for it and the edit did not name
    it (``custom_fields.<key>`` in *edited*, which is how a PATCH of that key arrives).
    An edited key takes the record's value, which is the person's own answer, and
    loses its mark. Returns whether the confidence object changed.
    """
    from omop_core.services import custom_field_values
    live = getattr(record, name, None)
    live = live if isinstance(live, dict) else {}
    marks = _object_marks(getattr(confidence, name, None)) if confidence is not None else {}
    stored = getattr(provisional, name, None)
    stored = stored if isinstance(stored, dict) else {}
    picture, kept = dict(live), {}
    for key, percent in marks.items():
        if (custom_field_values.pending_key(key) in edited
                or not custom_field_values.is_empty(live.get(key))
                or custom_field_values.is_empty(stored.get(key))):
            continue
        picture[key], kept[key] = stored[key], percent
    setattr(provisional, name, picture)
    if confidence is None or kept == marks:
        return False
    setattr(confidence, name, kept or None)
    return True


def _is_blank(value) -> bool:
    """An explicit clear: null, or text with nothing in it (never 0, False or a list)."""
    return value is None or (isinstance(value, str) and not value.strip())


# Fields whose empty string is a recorded answer, not a blank: a checklist saved with
# nothing ticked ("Record no GELF criteria" is Not Met, "Record no risk factors" is a
# FLIPI of 0), against null, "Mark not assessed". For these only null rejects a proposal;
# '' stays a value the user records, as it was before #2671. A new control that sends
# '' on purpose belongs here.
RECORDED_EMPTY_FIELDS = frozenset({'gelf_criteria_options', 'flipi_score_options'})


def _is_rejection(field, value) -> bool:
    return value is None if field in RECORDED_EMPTY_FIELDS else _is_blank(value)


def proposal_clears(record, patch_data, declared) -> list[str]:
    """The fields of a PATCH that explicitly reject a proposal the record read shows (#2671).

    A proposal is laid over a field the real record leaves empty, so a client that
    clears the displayed value sends a blank for a field the record already holds empty.
    That is no edit, and it is not an echo either: the editor sends back what it
    displays, which for such a field is the proposed value. A field is rejected only
    when all of these hold, so nothing a client sends by accident or for another reason
    can reject a proposal the person never saw:

    - the client *declared* it (``rejected_proposals`` in the body: the fields whose
      shown proposal it cleared), so a stale client that never saw the proposal, or one
      that sends a whole representation, rejects nothing;
    - its value is blank: null, or text with nothing in it, except for a field in
      ``RECORDED_EMPTY_FIELDS`` whose empty string is a recorded answer and only null
      counts;
    - the overlay marks it, and the real record leaves it empty.

    Calculated fields clear with their inputs, list fields are never rejected (their
    proposed items are not the user's to write, ``views._drop_proposed_list_items``),
    and neither is a unit (a picker that goes with its value, ``rejection_group``).
    Who may reject is the caller's rule (``views.apply_patient_record_patch``,
    ``allow_reject``). Nothing is read when no declared field is blank, and nothing is
    marked with ``PROVISIONAL_DATA_ENABLED`` off.
    """
    candidates = [field for field in declared
                  if field in patch_data and _is_rejection(field, patch_data[field])]
    if not candidates:
        return []
    marks = overlay(record, lists=False)[1]
    if not marks:
        return []
    from omop_core.services.patient_record_service import _is_empty
    return sorted(
        field for field in set(candidates)
        if field in marks and field not in COMPUTED_INPUTS and field not in PROVISIONAL_JSON_FIELDS
        and not field.endswith('_units') and _is_empty(getattr(record, field, None))
    )


def created_proposal_clears(record, custom_payload, declared) -> list[str]:
    """The created fields a PATCH explicitly rejects the proposal of (#2857): ``custom_fields.<name>``.

    The rule of ``proposal_clears``, for the keys of ``custom_fields``: the client
    *declared* the name in ``rejected_proposals``, sent a blank for that key (null, or
    text with nothing in it; ``false`` and ``0`` are values, so flipping a boolean is an
    edit), and the record read shows a proposal there (the overlay marks it, so the
    approved record has no value and no pending edit). The field must also be defined,
    approved and editable, as a field a patient can write. A name that is none of these is
    ignored, as an undeclared or unmarked column is.
    """
    prefix = custom_field_values.PENDING_PREFIX
    if not isinstance(custom_payload, dict):
        return []
    candidates = [name for name in declared if name.startswith(prefix)
                  and name[len(prefix):] in custom_payload
                  and _is_blank(custom_payload[name[len(prefix):]])]
    if not candidates:
        return []
    marks = overlay(record, lists=False)[1]
    candidates = [name for name in set(candidates) if name in marks]
    if not candidates:
        return []
    editable = custom_field_values.definitions([name[len(prefix):] for name in candidates])
    return sorted(name for name in candidates
                  if name[len(prefix):] in editable and custom_field_values.is_editable(editable[name[len(prefix):]]))


def reject_proposals(person, record, fields, actor=None) -> set[str]:
    """Keep the user's rejection of the proposals now on *fields*; every field to take out of view.

    Each field is remembered with the provisional rows that feed it today
    (``ProposalRejection``), so the pass leaves it empty from now on, until a
    different code or concept proposes it (``held_rejections``), and with who rejected
    it (*actor*'s Identity id) and when. Nothing is deleted or approved. The caller
    mirrors the returned fields out of the provisional picture (``mirror_user_edit``),
    which also drops a calculation made from them. A field no proposal feeds any more
    has nothing to hold; it is still returned, so a stale displayed value goes.
    """
    from django.utils import timezone
    from omop_core.services.patient_record_service import _build_snapshot
    origins = {}
    provisional_values(person, record, _build_snapshot(person), origins=origins,
                       hold_rejections=False, origin_fields=fields)
    by = str(actor.pk) if getattr(actor, 'pk', None) is not None else ''
    for field in fields:
        found = origins.get(field)
        if found:
            ProposalRejection.objects.update_or_create(
                person=person, field_name=field,
                defaults={'origins': [list(origin) for origin in sorted(found)],
                          'rejected_by': by, 'rejected_at': timezone.now()})
    return rejection_group(fields)


def proposal_dates(person, record, fields, values=None) -> dict:
    """``{field: (date, datetime or None)}``: when the reading behind each proposed field was taken.

    A professional confirming a proposal (#2870) approves a reading, so the approved
    fact is dated when it was measured, not the day it was confirmed (the typed edit's
    "today's fact" would put a 2024 result on today's date and distort "latest wins"
    and every trend). The rows are the ones that feed the field (``origins``); of them
    the newest whose number is the proposed value (*values*, field to value) is the
    reading the page shows, else the newest. A field no row explains is left out, and the
    write falls back to today as a typed edit does. The search runs once for all the
    fields and reads the rows again per code group, so its cost follows the number of
    provisional codes the person has (bounded by ``MAX_ORIGIN_GROUPS``), not the fields.
    """
    from decimal import Decimal, InvalidOperation

    def number(value):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    from omop_core.services.patient_record_service import _build_snapshot
    snapshot = _build_snapshot(person)
    origins = {}
    provisional_values(person, record, snapshot, origins=origins, origin_fields=fields)
    sources = (
        ('measurement', snapshot.provisional_measurements, 'measurement_source_value',
         'measurement_concept_id', 'measurement_date', 'measurement_datetime'),
        ('observation', snapshot.provisional_observations, 'observation_source_value',
         'observation_concept_id', 'observation_date', 'observation_datetime'),
        ('condition_occurrence', snapshot.provisional_conditions, 'condition_source_value',
         'condition_concept_id', 'condition_start_date', 'condition_start_datetime'),
    )
    found = {}
    for field in fields:
        wanted = origins.get(field)
        if not wanted:
            continue
        target = number((values or {}).get(field))
        newest = {True: None, False: None}     # keyed by "this row's number is the proposed value"
        for table, rows, source_field, concept_field, date_field, datetime_field in sources:
            for row in rows:
                if _origin(table, getattr(row, source_field), getattr(row, concept_field)) not in wanted:
                    continue
                when = getattr(row, date_field, None)
                if when is None:
                    continue
                exact = target is not None and number(getattr(row, 'value_as_number', None)) == target
                if newest[exact] is None or when >= newest[exact][0]:
                    newest[exact] = (when, getattr(row, datetime_field, None))
        if newest[True] or newest[False]:
            found[field] = newest[True] or newest[False]
    return found


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
                 if f not in COMPUTED_INPUTS and f not in PROVISIONAL_JSON_FIELDS
                 and f not in PROVISIONAL_OBJECT_FIELDS and getattr(confidence, f) is not None}
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


def overlay(record, lists=True, custom=True):
    """The record as a reader sees it: ``(record, marks)``.

    With ``PROVISIONAL_DATA_ENABLED`` on, a field the live record leaves empty
    takes its proposed value from ``patient_record_provisional``, and ``marks``
    maps each such field to its confidence (a percentage). Everything else is the
    record's own. Only a field with a confidence mark is taken, and only while the
    live record still has no answer for it, so a stale provisional row can never
    override approved data. The record itself is not changed or saved: the caller
    gets a copy to serialize. With the switch off, or no provisional row, it is
    ``(record, {})``.

    The list fields (``PROVISIONAL_JSON_FIELDS``) are merged and reported in
    ``view.provisional_items`` only with ``lists=True``, the record read. A reader
    that shows a summary or a section from the real rows (the patient list, the
    PHR) passes ``lists=False`` and sees the real list.

    The created fields (``custom_fields``, #2857) are scalars: each proposed value
    is laid in the object's key the live record leaves empty and has no pending
    edit on, and is marked ``custom_fields.<name>``. ``custom=False`` leaves them
    out, for a reader (the patient list) that shows none.

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
    view, marks, added = copy.copy(record), {}, {}
    for field in PatientRecordConfidence._meta.concrete_fields:
        if field.name == 'person' or field.name in NEVER_OVERLAID:
            continue
        mark = getattr(confidence, field.name)
        if mark is None:
            continue
        if field.name in PROVISIONAL_OBJECT_FIELDS:
            if custom:
                _overlay_created_fields(view, record, provisional, mark, marks, field.name)
            continue
        live = getattr(record, field.attname, None)
        if field.name in PROVISIONAL_JSON_FIELDS:
            if not lists:
                continue
            # A list: the live items first, then the stored proposals the record
            # does not already hold. Reading again over the result adds nothing.
            new = _unseen(field.name, live, getattr(provisional, field.attname))
            if new:
                setattr(view, field.attname, [*(live or []), *new])
                marks[field.name] = mark
                added[field.name] = new
        elif _no_answer(field.name, live):
            setattr(view, field.attname, getattr(provisional, field.attname))
            marks[field.name] = mark
    # A precision describes its date: a proposed one is shown only with the proposed date
    # (an approved 2020-05-12 with a proposed "year" must not read as "2020").
    for field_name in [name for name in marks if _date_of(name) and _date_of(name) not in marks]:
        setattr(view, field_name, getattr(record, field_name, None))
        del marks[field_name]
    if added:
        # Which items of each list are proposals; the serializer carries it out.
        view.provisional_items = added
    return view, marks


def _overlay_created_fields(view, record, provisional, stored_marks, marks,
                            container=custom_field_values.CONTAINER) -> None:
    """Lay the proposed created-field values on *view*, marking each in *marks*.

    A key is shown from a proposal only while the live record has no value for it
    and no pending edit (a clear included) on it, so a stale mark never overrides
    approved data or a user's answer.
    """
    live = getattr(record, container, None)
    live = live if isinstance(live, dict) else {}
    stored = getattr(provisional, container, None)
    stored = stored if isinstance(stored, dict) else {}
    pending = set(record.user_edited_fields or [])
    shown = dict(live)
    for name, percent in _object_marks(stored_marks).items():
        if (not custom_field_values.is_empty(live.get(name))
                or custom_field_values.is_empty(stored.get(name))
                or custom_field_values.pending_key(name) in pending):
            continue
        shown[name] = stored[name]
        marks[custom_field_values.pending_key(name)] = percent
    if shown != live:
        setattr(view, container, shown)


def _custom_blockers(shown_record, marks) -> list[str]:
    """The created fields' marks that stop a patient attesting: ``custom_fields.<name>``.

    Created fields are runtime data, so no generated file can list the ones a patient
    tab shows; their definitions decide (``custom_field_values.shown_to_patient``),
    and ``attestationBlockers`` in ``PatientInfo/provisional.ts`` applies the same
    rule to the definitions the page loaded. A mark blocks when the field is defined,
    an approved editable field (a computed one is its formula's result, and a
    patient's PATCH cannot write it), placed on a tab a patient sees, part of the
    record's Disease Category, and on a disease tab the record has. A field placed on
    no tab (``UNPLACED_TAB``) or one the patient cannot edit never blocks: it could
    not be lifted.
    """
    prefix = custom_field_values.PENDING_PREFIX
    names = [key[len(prefix):] for key in marks if key.startswith(prefix)]
    if not names:
        return []
    return sorted(
        custom_field_values.pending_key(name) for name in custom_field_values.shown_to_patient(
            names, getattr(shown_record, 'disease', None))
    )


def attestation_blockers(record) -> list[str]:
    """The fields that stop a patient attesting their record (#2761).

    A patient's "Confirm my record" says what they saw is accurate. While the
    page lays a proposal over a field (``overlay``), part of what they see is
    unapproved, so the attestation is refused until each marked field is entered
    again, which saves it as approved data, or rejected (``proposal_clears``), which
    takes its mark off. The answer is the scalar fields the overlay marks that a
    patient tab renders (``patient_visible_fields``: the always-shown tabs, plus the
    disease tab's section for the disease the page shows) and the patient can edit:
    a field no tab shows is marked in the record read but not on screen, so blocking
    on it could never be lifted, and the list fields (``PROVISIONAL_JSON_FIELDS``)
    are likewise left out (the module says why). A displayed field with no write
    path of its own (a calculated or mirrored one, a unit picker) clears with the
    field it follows, so it does not block either; a displayed field the write
    descriptor does not know does.

    A created field (``custom_fields.<name>``) blocks by the rule in
    ``_custom_blockers``, from its definition.

    Empty with ``PROVISIONAL_DATA_ENABLED`` off, or no proposal on display. The
    frontend applies the same rule to the marks it holds
    (``attestationBlockers`` in ``PatientInfo/provisional.ts``), so the banner and
    the endpoint agree.
    """
    shown_record, marks = overlay(record, lists=False)
    created = _custom_blockers(shown_record, marks)
    from omop_core.services.patient_visible_fields import on_screen
    on_page = on_screen(getattr(shown_record, 'disease', None))
    shown = [field for field in marks if field in on_page]
    if not shown:
        return created
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    descriptor = build_writable_field_descriptor()
    return sorted(created + [
        field for field in shown
        if field not in descriptor or descriptor[field].get('writable')
    ])
