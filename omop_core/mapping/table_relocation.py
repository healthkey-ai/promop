"""Move a source code's stored clinical rows from one OMOP table to another.

A curator who changes a mapping's Domain re-files the mapping under another
table (an observation that was really a lab result becomes a measurement).
The rows ingest already wrote for that code stay where ingest put them, so
the patient page -- which lists a code under the table its rows sit in --
keeps showing the code as Unmapped under the old table, with no mapping that
could ever match it there (#2386). The rows have to follow the mapping.

Every clinical table carries the same core: who, when, how it was recorded,
the source text and its source concept, the provider and visit. Measurement
and Observation also share the value columns. A row is copied column by
column onto the destination table under a new id from that table's sequence
(``copy_patient`` allocates the same way), its ``ProvenanceRecord`` follows it,
and the old row is deleted. Columns the destination has no place for (a
measurement's operator and range) are dropped.
"""
from __future__ import annotations

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, _source_value_match, normalize_omop_table,
)
from omop_core.models import MeasurementOwnership, PatientRecord, PatientSourceCode, ProvenanceRecord
from omop_core.services.pk import next_pk_batch
from omop_core.services.provenance_attribution import exclusively_attributed
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

# The columns every clinical table has under its own name, by role. Keyed on
# the canonical table names of CLINICAL_TABLES; values are attnames.
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
    'condition': {
        'pk': 'condition_occurrence_id', 'concept': 'condition_concept_id',
        'date': 'condition_start_date', 'datetime': 'condition_start_datetime',
        'type': 'condition_type_concept_id', 'source_value': 'condition_source_value',
        'source_concept': 'condition_source_concept_id',
    },
    'drug_exposure': {
        'pk': 'drug_exposure_id', 'concept': 'drug_concept_id',
        'date': 'drug_exposure_start_date', 'datetime': 'drug_exposure_start_datetime',
        'type': 'drug_type_concept_id', 'source_value': 'drug_source_value',
        'source_concept': 'drug_source_concept_id',
    },
    'procedure': {
        'pk': 'procedure_occurrence_id', 'concept': 'procedure_concept_id',
        'date': 'procedure_date', 'datetime': 'procedure_datetime',
        'type': 'procedure_type_concept_id', 'source_value': 'procedure_source_value',
        'source_concept': 'procedure_source_concept_id',
    },
}
_BATCH = 500


def relocatable(from_table, to_table):
    """Both are clinical tables and they differ."""
    source, target = normalize_omop_table(from_table), normalize_omop_table(to_table)
    return source in CLINICAL_TABLES and target in CLINICAL_TABLES and source != target


def _shared_columns(from_model, to_model, from_core, to_core):
    """Attnames both models carry under the same name, less the role columns."""
    roles = set(from_core.values()) | set(to_core.values())
    names = {f.attname for f in from_model._meta.concrete_fields}
    return [f.attname for f in to_model._meta.concrete_fields
            if f.attname in names and f.attname not in roles]


def relocate_clinical_rows(*, mapping, from_table, to_table, person_id=None, apply_changes=True):
    """Move the rows of *mapping*'s source code from *from_table* to *to_table*.

    An approved mapping's rows land on its destination concept; a proposed
    mapping's keep the concept they had, and those concepts are recorded in
    ``deferred_repoint_concept_ids`` so the approval that follows sweeps them,
    as it would any row this code left behind. *person_id* limits the move to
    one patient (the patient page's save); otherwise every patient's rows
    move. An organization-scoped mapping moves only rows its hospital wrote,
    the re-point rule.

    The per-patient source-code inventory (``PatientSourceCode``) is re-filed
    under the new table too, merging into a row the patient already has there,
    so the Source Codes tab stops listing the code under the old one.

    Affected ``PatientRecord`` rows are marked stale for ``backfill_patient_records``
    rather than re-derived here, as ``repoint_clinical_rows`` does.

    Returns ``{'rows_relocated', 'persons_marked_stale', 'source_codes_refiled',
    'person_ids'}``.
    """
    result = {'rows_relocated': 0, 'persons_marked_stale': 0, 'source_codes_refiled': 0,
              'person_ids': set()}
    source, target = normalize_omop_table(from_table), normalize_omop_table(to_table)
    if not relocatable(source, target):
        return result
    from_model, from_concept_col, from_source_col = CLINICAL_TABLES[source]
    to_model, _to_concept_col, _to_source_col = CLINICAL_TABLES[target]
    from_core, to_core = _CORE_COLUMNS[source], _CORE_COLUMNS[target]
    shared = _shared_columns(from_model, to_model, from_core, to_core)

    rows = from_model.objects.filter(_source_value_match(from_model, from_source_col, mapping))
    if person_id is not None:
        rows = rows.filter(person_id=person_id)
    if mapping.organization_id:
        rows = exclusively_attributed(rows, mapping.organization_id)
    rows = rows.order_by(from_core['pk'])
    person_ids = set(rows.values_list('person_id', flat=True).distinct())
    result['rows_relocated'] = rows.count()
    result['person_ids'] = set(person_ids)
    if not apply_changes:
        result['persons_marked_stale'] = len(person_ids)
        return result
    if not result['rows_relocated']:
        result['source_codes_refiled'] = _refile_source_codes(mapping, source, target, person_id)
        return result

    approved = mapping.status == 'approved' and mapping.target_concept_id is not None
    with transaction.atomic():
        # Marked before the write, as the re-point does: an abort mid-move must
        # not leave moved rows whose record is never selected for re-derivation.
        result['persons_marked_stale'] = PatientRecord.objects.filter(
            person_id__in=person_ids,
        ).update(derivation_version=0)
        from_type = ContentType.objects.get_for_model(from_model)
        to_type = ContentType.objects.get_for_model(to_model)
        old_concepts = set()
        with suppress_patient_record_refresh():
            while True:
                batch = list(rows[:_BATCH])
                if not batch:
                    break
                new_ids = next_pk_batch(to_model, to_core['pk'], len(batch))
                created = []
                for row, new_id in zip(batch, new_ids):
                    old_concept = getattr(row, from_concept_col)
                    old_concepts.add(old_concept)
                    values = {column: getattr(row, column) for column in shared}
                    values.update({
                        to_core['pk']: new_id,
                        to_core['concept']: mapping.target_concept_id if approved else old_concept,
                        to_core['date']: getattr(row, from_core['date']),
                        to_core['datetime']: getattr(row, from_core['datetime']),
                        to_core['type']: getattr(row, from_core['type']),
                        to_core['source_value']: getattr(row, from_core['source_value']),
                        to_core['source_concept']: getattr(row, from_core['source_concept']),
                    })
                    created.append(to_model(**values))
                to_model.objects.bulk_create(created)
                old_ids = [getattr(row, from_core['pk']) for row in batch]
                for old_id, new_id in zip(old_ids, new_ids):
                    ProvenanceRecord.objects.filter(content_type=from_type, object_id=old_id).update(
                        content_type=to_type, object_id=new_id,
                    )
                if source == 'measurement':
                    # Ownership holds a bare measurement_id; the row is no
                    # longer a measurement.
                    MeasurementOwnership.objects.filter(measurement_id__in=old_ids).delete()
                from_model.objects.filter(**{f"{from_core['pk']}__in": old_ids}).delete()
        if not approved:
            waiting = set(mapping.deferred_repoint_concept_ids or []) | old_concepts
            waiting.discard(mapping.target_concept_id)
            waiting.add(NO_MATCHING_CONCEPT_ID)
            mapping.deferred_repoint_concept_ids = sorted(waiting)
            mapping.save(update_fields=['deferred_repoint_concept_ids'])
        result['source_codes_refiled'] = _refile_source_codes(mapping, source, target, person_id)
    logger.info(
        'Relocated %s row(s) of mapping %s from %s to %s (%s patient(s), %s inventory row(s)).',
        result['rows_relocated'], mapping.pk, source, target, len(person_ids),
        result['source_codes_refiled'],
    )
    return result


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
