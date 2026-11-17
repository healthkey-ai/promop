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

Readers that combine many rows (therapy lines, staging, genomics, ...) are not
run: those columns equal ``patient_record``. Computed fields (BMI, scores) are
not recomputed from a provisional input either, so they equal the real record.
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


def provisional_values(person, record, snapshot, user_edited=()) -> tuple[dict, dict]:
    """(values, confidences) the provisional rows add to *record*, by field name."""
    from omop_core.services.patient_record_service import _is_empty
    measurements = _after_clears(snapshot, snapshot.provisional_measurements, 'measurement')
    observations = _after_clears(snapshot, snapshot.provisional_observations, 'observation')
    if not (measurements or observations):
        return {}, {}
    confidence_names = _confidence_names()
    blocked = set(user_edited) | set(record.user_edited_fields or [])
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
    if not kept:
        return {}, {}
    return kept, _attribute(person, record, snapshot, kept, measurements, observations, routing)


def write_provisional_pair(person, snapshot, record, user_edited=()) -> None:
    """Write the person's provisional and confidence rows from the saved real *record*.

    A person with no provisional clinical rows and no pair already stored has
    nothing to say, so nothing is written (the endpoint serves the real record for them);
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
            provisional.save()
            if cleared:
                confidence.save(update_fields=cleared)
    except Exception:  # noqa: BLE001 -- the user's edit is already saved; this is the side picture
        logger.exception('Provisional mirror failed for person_id=%s; the edit is saved.', record.person_id)
