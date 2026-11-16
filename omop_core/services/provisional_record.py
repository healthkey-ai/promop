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
    PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
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


def _view(snapshot, measurements, observations):
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
        lab_routing_cache={}, genomics_cache={},
    )


def _read_values(person, record, view) -> dict:
    """The phase 1 readers over *view*, in the order the real refresh runs them."""
    from omop_core.services.omop_projection import curated_values_from_snapshot
    from omop_core.services.patient_record_service import (
        _ANSWER_LINE_FIELDS, _answer_line, _fits_column, _get_assertion_data,
        _get_laboratory_data,
    )
    values = dict(_get_assertion_data(person, view))
    lab = _get_laboratory_data(person, view)
    values.update(lab)
    for field, value in curated_values_from_snapshot(view, confirmed_clinical_fields=set()).items():
        if field in _ANSWER_LINE_FIELDS and value is not None:
            value = _answer_line(field, value)
        if field not in lab and _fits_column(record, field, value):
            values[field] = value
    return values


def _attribute(person, record, snapshot, kept, measurements, observations) -> dict:
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


def provisional_values(person, record, snapshot, user_edited=()) -> tuple[dict, dict]:
    """(values, confidences) the provisional rows add to *record*, by field name."""
    from omop_core.services.patient_record_service import _is_empty
    measurements = list(snapshot.provisional_measurements)
    observations = list(snapshot.provisional_observations)
    if not (measurements or observations):
        return {}, {}
    confidence_names = _confidence_names()
    blocked = set(user_edited) | set(record.user_edited_fields or [])
    values = _read_values(person, record, _view(snapshot, measurements, observations))
    # A value is provisional only if its confidence can be recorded, and only
    # where the real record has no answer and nobody has typed one.
    kept = {
        field: value for field, value in values.items()
        if field in confidence_names and field not in blocked
        and not _is_empty(value) and _is_empty(getattr(record, field, None))
    }
    if not kept:
        return {}, {}
    return kept, _attribute(person, record, snapshot, kept, measurements, observations)


def write_provisional_pair(person, snapshot, record, user_edited=()) -> None:
    """Upsert the person's provisional and confidence rows from the saved real *record*.

    A failure here is logged and leaves both rows as they were: the approved
    record is the one that must not be lost to the provisional pass.
    """
    try:
        with transaction.atomic():
            values, confidences = provisional_values(person, record, snapshot, user_edited)
            row = {f.attname: getattr(record, f.attname) for f in _cloned_fields()}
            row.update(values)
            PatientRecordProvisional.objects.update_or_create(person=person, defaults=row)
            PatientRecordConfidence.objects.update_or_create(
                person=person, defaults={name: confidences.get(name) for name in _confidence_names()},
            )
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
    edited = set(edited_fields)
    with transaction.atomic():
        for field in _cloned_fields():
            if field.name in edited or confidence is None or getattr(confidence, field.name, None) is None:
                setattr(provisional, field.attname, getattr(record, field.attname))
        provisional.save()
        if confidence is not None:
            cleared = [name for name in edited
                       if name in _confidence_names() and getattr(confidence, name) is not None]
            for name in cleared:
                setattr(confidence, name, None)
            if cleared:
                confidence.save(update_fields=cleared)
