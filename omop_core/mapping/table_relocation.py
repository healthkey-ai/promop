"""Move a source code's stored clinical rows from one OMOP table to another.

A curator who changes a mapping's Domain re-files the mapping under another
table (an observation that was really a lab result becomes a measurement).
The rows ingest already wrote for that code stay where ingest put them, so
the patient page -- which lists a code under the table its rows sit in --
keeps showing the code as Unmapped under the old table, with no mapping that
could ever match it there (#2386). The rows have to follow the mapping.

Only Measurement and Observation are moved, in either direction. They share
the fact shape (a value, a unit, a qualifier) and nothing else points at
their rows by id except provenance and ownership. A drug exposure composes a
therapy line, a procedure anchors an imaging document, an episode event names
a row by bare id: moving those would cascade or dangle, so a mapping that
changes table between any other pair keeps its rows where they are and the
save says so.

A row is copied column by column onto the destination table under a new id
from that table's sequence (``copy_patient`` allocates the same way), its
``ProvenanceRecord`` follows it, and the old row is deleted. Columns the
destination has no place for (a measurement's operator and range) are
dropped. The row keeps the concept it had: putting it on the mapping's
destination is the re-point's job, which also normalizes units, routes axis
variants and collapses duplicates, so the concepts moved are recorded in
``deferred_repoint_concept_ids`` for it.
"""
from __future__ import annotations

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Case, Q, Value, When

from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, _source_value_match, normalize_omop_table,
)
from omop_core.models import MeasurementOwnership, PatientRecord, PatientSourceCode, ProvenanceRecord
from omop_core.services.pk import next_pk_batch
from omop_core.services.provenance_attribution import exclusively_attributed
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

#: The tables whose rows may be moved into each other.
RELOCATABLE_TABLES = frozenset({'measurement', 'observation'})

# The columns both tables have under their own name, by role.
_CORE_COLUMNS = {
    'measurement': {
        'pk': 'measurement_id', 'concept': 'measurement_concept_id',
        'date': 'measurement_date', 'datetime': 'measurement_datetime',
        'type': 'measurement_type_concept_id', 'source_value': 'measurement_source_value',
        'source_concept': 'measurement_source_concept_id',
    },
    'observation': {
        'pk': 'observation_id', 'concept': 'observation_concept_id',
        'date': 'observation_date', 'datetime': 'observation_datetime',
        'type': 'observation_type_concept_id', 'source_value': 'observation_source_value',
        'source_concept': 'observation_source_concept_id',
    },
}
# Rows per transaction. Each batch commits on its own, so a task killed at
# its time limit resumes where it stopped rather than rolling the move back.
BATCH = 500


def relocatable(from_table, to_table):
    """Both are tables whose rows can be moved, and they differ."""
    source, target = normalize_omop_table(from_table), normalize_omop_table(to_table)
    return source in RELOCATABLE_TABLES and target in RELOCATABLE_TABLES and source != target


def _same_shape(left, right):
    return (type(left) is type(right)
            and getattr(left, 'max_length', None) == getattr(right, 'max_length', None)
            and getattr(left, 'max_digits', None) == getattr(right, 'max_digits', None)
            and getattr(left, 'decimal_places', None) == getattr(right, 'decimal_places', None))


def _shared_columns(from_model, to_model, from_core, to_core):
    """Attnames both models carry under the same name and shape, less the role columns.

    Same shape, not just same name: a wider or differently typed column would
    truncate the value or fail the move.
    """
    roles = set(from_core.values()) | set(to_core.values())
    fields = {f.attname: f for f in from_model._meta.concrete_fields}
    return [f.attname for f in to_model._meta.concrete_fields
            if f.attname in fields and f.attname not in roles and _same_shape(fields[f.attname], f)]


def _own_rows(from_model, from_concept_col, from_source_col, mapping):
    """The rows this mapping's code produced.

    The code matches by itself. The import description matches only rows at
    a concept this mapping has named (its destination, the concepts it is
    moving off, or none): an import's display text such as "Glucose" is
    shared by every producer's glucose row, and a re-point tolerates the
    description match only under the same concept constraint.
    """
    match = _source_value_match(from_model, from_source_col, mapping, include_description=False)
    if mapping.origin == 'import' and mapping.source_code_description:
        own = {NO_MATCHING_CONCEPT_ID, mapping.target_concept_id,
               *(mapping.deferred_repoint_concept_ids or []), *(mapping.pending_repoint_concept_ids or [])}
        match |= (_source_value_match(from_model, from_source_col, mapping)
                  & Q(**{f'{from_concept_col}__in': [c for c in own if c is not None]}))
    return from_model.objects.filter(match)


def relocate_clinical_rows(*, mapping, from_table, to_table, person_id=None, apply_changes=True):
    """Move the rows of *mapping*'s source code from *from_table* to *to_table*.

    *person_id* limits the move to one patient (the patient page's save);
    otherwise every patient's rows move. An organization-scoped mapping moves
    only rows its hospital wrote, the re-point rule. Each batch of ``BATCH``
    rows is one transaction.

    The rows keep their concept; the distinct concepts moved join
    ``deferred_repoint_concept_ids`` (with concept 0) so the mapping's
    re-point -- queued by an approval, or Generate OMOP for a patient-scoped
    save -- puts them on the destination in the new table. The per-patient
    inventory (``PatientSourceCode``) is re-filed under the new table too,
    merging into a row the patient already has there, so the Source Codes tab
    stops listing the code under the old one. Affected ``PatientRecord`` rows
    are marked stale for ``backfill_patient_records``.

    Returns ``{'rows_relocated', 'persons_marked_stale', 'source_codes_refiled',
    'concepts_moved', 'person_ids'}``.
    """
    result = {'rows_relocated': 0, 'persons_marked_stale': 0, 'source_codes_refiled': 0,
              'concepts_moved': [], 'person_ids': set()}
    source, target = normalize_omop_table(from_table), normalize_omop_table(to_table)
    if not relocatable(source, target):
        return result
    from_model, from_concept_col, from_source_col = CLINICAL_TABLES[source]
    to_model, _concept_col, _source_col = CLINICAL_TABLES[target]
    from_core, to_core = _CORE_COLUMNS[source], _CORE_COLUMNS[target]
    shared = _shared_columns(from_model, to_model, from_core, to_core)

    rows = _own_rows(from_model, from_concept_col, from_source_col, mapping)
    if person_id is not None:
        rows = rows.filter(person_id=person_id)
    if mapping.organization_id:
        rows = exclusively_attributed(rows, mapping.organization_id)
    rows = rows.order_by(from_core['pk'])
    person_ids = set(rows.values_list('person_id', flat=True).distinct())
    result['person_ids'] = set(person_ids)
    if not apply_changes:
        result['rows_relocated'] = rows.count()
        result['persons_marked_stale'] = len(person_ids)
        return result

    if person_ids:
        # Marked before the first move, as the re-point does: an abort between
        # batches must not leave moved rows whose record is never re-derived.
        with transaction.atomic():
            result['persons_marked_stale'] = PatientRecord.objects.filter(
                person_id__in=person_ids,
            ).update(derivation_version=0)
    from_type = ContentType.objects.get_for_model(from_model)
    to_type = ContentType.objects.get_for_model(to_model)
    concepts_moved = set()
    while True:
        with transaction.atomic(), suppress_patient_record_refresh():
            batch = list(rows[:BATCH])
            if not batch:
                break
            new_ids = next_pk_batch(to_model, to_core['pk'], len(batch))
            taken = _taken_source_keys(to_model, batch)
            created = []
            for row, new_id in zip(batch, new_ids):
                concepts_moved.add(getattr(row, from_concept_col))
                values = {column: getattr(row, column) for column in shared}
                values.update({
                    to_core['pk']: new_id,
                    to_core['concept']: getattr(row, from_concept_col),
                    to_core['date']: getattr(row, from_core['date']),
                    to_core['datetime']: getattr(row, from_core['datetime']),
                    to_core['type']: getattr(row, from_core['type']),
                    to_core['source_value']: getattr(row, from_core['source_value']),
                    to_core['source_concept']: getattr(row, from_core['source_concept']),
                })
                if (row.person_id, values.get('source_record_key')) in taken:
                    values['source_record_key'] = None
                created.append(to_model(**values))
            to_model.objects.bulk_create(created)
            old_ids = [getattr(row, from_core['pk']) for row in batch]
            ProvenanceRecord.objects.filter(content_type=from_type, object_id__in=old_ids).update(
                content_type=to_type,
                object_id=Case(*[When(object_id=old, then=Value(new)) for old, new in zip(old_ids, new_ids)]),
            )
            if source == 'measurement':
                # Ownership holds a bare measurement_id; the row is no longer a measurement.
                MeasurementOwnership.objects.filter(measurement_id__in=old_ids).delete()
            from_model.objects.filter(**{f"{from_core['pk']}__in": old_ids}).delete()
            result['rows_relocated'] += len(batch)
    with transaction.atomic():
        if concepts_moved:
            waiting = set(mapping.deferred_repoint_concept_ids or []) | concepts_moved | {NO_MATCHING_CONCEPT_ID}
            waiting.discard(mapping.target_concept_id)
            mapping.deferred_repoint_concept_ids = sorted(waiting)
            mapping.save(update_fields=['deferred_repoint_concept_ids'])
        result['source_codes_refiled'] = _refile_source_codes(mapping, source, target, person_id)
    result['concepts_moved'] = sorted(concepts_moved)
    if result['rows_relocated'] or result['source_codes_refiled']:
        logger.info(
            'Relocated %s row(s) of mapping %s from %s to %s (%s patient(s), %s inventory row(s)).',
            result['rows_relocated'], mapping.pk, source, target, len(person_ids),
            result['source_codes_refiled'],
        )
    return result


def _taken_source_keys(to_model, batch):
    """(person, source_record_key) pairs of *batch* the destination already holds.

    The key travels with a moved row (#2453), but a producer that wrote the
    same record to the destination table meanwhile owns it there; the copy
    then goes without its key rather than failing the batch on the unique
    index, which would stall the move on that row for good.
    """
    pairs = {(row.person_id, row.source_record_key) for row in batch
             if getattr(row, 'source_record_key', None)}
    if not pairs:
        return set()
    held = to_model.objects.filter(
        person_id__in={p for p, _ in pairs},
        source_record_key__in={k for _, k in pairs},
    ).values_list('person_id', 'source_record_key')
    return pairs & set(held)


def _refile_source_codes(mapping, source, target, person_id):
    """Put the per-patient inventory rows of the code under the new table.

    The unique key is (person, value, vocabulary, table), so a patient who
    already has the code under the new table (ingest wrote some rows to each)
    gets the counts merged and the old row removed.
    """
    rows = PatientSourceCode.objects.filter(
        source_value__iexact=mapping.source_code, omop_table=source,
    )
    if mapping.source_vocabulary_id:
        rows = rows.filter(source_vocabulary_id=mapping.source_vocabulary_id)
    if person_id is not None:
        rows = rows.filter(person_id=person_id)
    moved = 0
    for row in rows:
        existing = PatientSourceCode.objects.filter(
            person_id=row.person_id, source_value=row.source_value,
            source_vocabulary_id=row.source_vocabulary_id, omop_table=target,
        ).first()
        if existing is None:
            row.omop_table = target
            row.save(update_fields=['omop_table'])
        else:
            existing.occurrence_count = (existing.occurrence_count or 0) + (row.occurrence_count or 0)
            existing.save(update_fields=['occurrence_count'])
            row.delete()
        moved += 1
    return moved
