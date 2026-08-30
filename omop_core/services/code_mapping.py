"""Source code -> destination OMOP concept resolution and re-pointing (#834).

Codes reach promop from FHIR bundles, parsed paper labs, and clinicians' notes.
They may be LOINC or SNOMED codes, ICD codes, a lab's in-house test name, or
free text.  All of them have to end up as an OMOP concept, and this module is
where that decision is made and recorded.

Resolution order — ``resolve_source_code``:

1. A **LOINC or SNOMED** source code resolves directly to its Athena concept.
   No mapping row: the Athena concept *is* the code, which is the whole design
   of the vocabulary, so curating a row for it would be busywork that could
   only ever drift from Athena.
2. Otherwise an **approved** mapping wins, over everything, including a direct
   concept lookup.  An approved row is a curator's deliberate decision, and
   overriding a wrong automatic resolution is exactly what it is for.
3. With no approved mapping, resolve as before (direct lookup on the source
   vocabulary), and if that fails **mint** a concept under an ``HK-*``
   quarantine vocabulary and record a **proposed** mapping beside it.
4. A **proposed** mapping never overrides anything.  It is a review item, and
   an admin/curator/SME approves, edits, or rejects it in the Code Mapping UI.

So imports never block on a curator, never silently drop a code, and everything
they invent lands in a queue with the evidence attached.

``repoint_clinical_rows`` is the other half: approving a mapping has to rewrite
the rows already stored, or the decision only ever reaches data that happens to
arrive again.
"""
import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Case, IntegerField, When
from django.utils import timezone

from omop_core.models import (
    Concept,
    ConditionOccurrence,
    DrugExposure,
    Measurement,
    Observation,
    PatientRecord,
    ProcedureOccurrence,
    ProvenanceRecord,
    SourceCodeConceptMapping,
)
from omop_core.services.regimen_resolution import (
    SOURCE_HEALTHKEY,
    _get_or_create_quarantine_concept,
    _slug,
)
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

# Vocabularies whose codes are their own concepts. A source code in one of
# these resolves directly and never generates a mapping row (rule 1).
SELF_RESOLVING_VOCABULARIES = frozenset({'LOINC', 'SNOMED'})

# OMOP 'No matching concept'. Where an unresolved fact sits, and therefore the
# concept a first approval usually moves rows off.
NO_MATCHING_CONCEPT_ID = 0

# Clinical table -> (model, concept column, source-value column). The concept
# column is what a re-point rewrites; the source-value column is how a mapping
# finds its rows.
CLINICAL_TABLES = {
    'measurement': (Measurement, 'measurement_concept_id', 'measurement_source_value'),
    'observation': (Observation, 'observation_concept_id', 'observation_source_value'),
    'condition': (ConditionOccurrence, 'condition_concept_id', 'condition_source_value'),
    'drug_exposure': (DrugExposure, 'drug_concept_id', 'drug_source_value'),
    'procedure': (ProcedureOccurrence, 'procedure_concept_id', 'procedure_source_value'),
}

# Which HK-* vocabulary a minted destination lands in, per OMOP table, and the
# domain/class it carries there.
_QUARANTINE_TARGETS = {
    'measurement': ('HK-Labs', 'Measurement', 'Lab Test', 'hkl'),
    'observation': ('HK-Observation', 'Observation', 'Clinical Observation', 'hko'),
    'condition': ('HK-Condition', 'Condition', 'Clinical Finding', 'hkc'),
    'drug_exposure': ('HK-Drug', 'Drug', 'Drug', 'hkd'),
    'procedure': ('HK-Procedure', 'Procedure', 'Procedure', 'hkp'),
}


# What makes two rows the same event, per table. Mirrors the bulk write path's
# _UPSERT_KEYS. Measurement and Observation diverge from the other three on
# purpose: several distinct results for one analyte on one day are real, so the
# raw value columns have to separate them.
_COLLAPSE_IDENTITY = {
    'measurement_concept_id': (
        'measurement_date', 'measurement_datetime', 'value_as_number',
        'value_as_string',
    ),
    'observation_concept_id': (
        'observation_date', 'observation_datetime', 'value_as_number',
        'value_as_string', 'value_source_value',
    ),
    'condition_concept_id': ('condition_start_date',),
    'drug_concept_id': ('drug_exposure_start_date',),
    'procedure_concept_id': ('procedure_date',),
}


def normalize_omop_table(omop_table):
    """Fold the aliases callers use onto the canonical table keys."""
    value = (omop_table or '').strip().lower()
    return {
        'condition_occurrence': 'condition',
        'drug': 'drug_exposure',
        'procedure_occurrence': 'procedure',
    }.get(value, value)


def approved_mapping_for(source_vocabulary_id, source_code):
    """Return the approved mapping for this code, or None.

    Matched case-insensitively on the code: an uncoded source is a lab's or a
    clinician's free text, and 'M-Protein, Serum' and 'M-PROTEIN, SERUM' are
    the same test.
    """
    if not source_code:
        return None
    return SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id=source_vocabulary_id or '',
        source_code__iexact=source_code,
        status='approved',
    ).select_related('target_concept').first()


def _direct_concept(source_vocabulary_id, source_code):
    """Resolve a code against Athena.

    With no source code system this searches every vocabulary, which can match
    more than one concept -- an in-house numeric code can collide with a real
    one. Standard concepts win, then the lowest id, so the answer is at least
    stable run to run rather than whatever the planner returned first.
    """
    if not source_code:
        return None
    qs = Concept.objects.filter(concept_code=source_code)
    if source_vocabulary_id:
        qs = qs.filter(vocabulary_id=source_vocabulary_id)
    return qs.order_by(
        Case(When(standard_concept='S', then=0), default=1, output_field=IntegerField()),
        'concept_id',
    ).first()


def _record_proposal(*, source_vocabulary_id, source_code, source_text,
                     concept, omop_table, source_system):
    """Create or bump the proposed mapping for a code an import had to invent.

    Idempotent: the first sighting creates the row, later sightings bump
    ``occurrence_count`` and ``last_seen`` so the review queue can be ordered by
    how much a code actually matters.  Never touches an approved row — that
    would let an import quietly undo a curator.
    """
    now = timezone.now()
    # Truncated before the lookup as well as the create. Looking up the full
    # string but storing 100 chars means every later sighting of a longer source
    # text misses, re-enters the create branch, and trips the unique constraint.
    source_code = source_code[:100]
    mapping = SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id=source_vocabulary_id or '',
        source_code__iexact=source_code,
    ).first()

    if mapping is None:
        return SourceCodeConceptMapping.objects.create(
            source_vocabulary_id=source_vocabulary_id or '',
            source_code=source_code,
            source_code_description=(source_text or '')[:255],
            target_concept=concept,
            destination_vocabulary_id=concept.vocabulary_id or '',
            omop_table=omop_table,
            source=SOURCE_HEALTHKEY,
            status='proposed',
            origin='import',
            origin_system=source_system,
            occurrence_count=1,
            first_seen=now,
            last_seen=now,
        )

    if mapping.status == 'approved':
        return mapping

    mapping.occurrence_count = (mapping.occurrence_count or 0) + 1
    mapping.last_seen = now
    if mapping.first_seen is None:
        mapping.first_seen = now
    mapping.save(update_fields=['occurrence_count', 'last_seen', 'first_seen'])
    return mapping


def resolve_source_code(*, source_code, omop_table, source_vocabulary_id='',
                        source_text='', source_system='fhir-upload'):
    """Resolve an inbound source code to a destination concept.

    Implements the four rules in the module docstring. Returns
    ``(concept, mapping_or_None)``; ``concept`` is None only when there is
    nothing to resolve (no code at all).
    """
    source_code = (source_code or '').strip()
    if not source_code:
        return None, None
    table = normalize_omop_table(omop_table)

    # Rule 1 — a LOINC/SNOMED code is its own concept.
    if source_vocabulary_id in SELF_RESOLVING_VOCABULARIES:
        return _direct_concept(source_vocabulary_id, source_code), None

    # Rule 2 — an approved mapping beats everything else.
    approved = approved_mapping_for(source_vocabulary_id, source_code)
    if approved is not None:
        return approved.target_concept, approved

    # Rule 3a — resolve against Athena as before.
    concept = _direct_concept(source_vocabulary_id, source_code)
    if concept is not None:
        return concept, None

    # Rule 3b — mint, and propose the mapping for review.
    target = _QUARANTINE_TARGETS.get(table)
    if target is None:
        logger.warning(
            'No quarantine vocabulary for omop_table=%r; leaving %r unresolved.',
            omop_table, source_code,
        )
        return None, None
    vocabulary_id, domain_id, concept_class_id, prefix = target
    name = (source_text or source_code)[:255]
    concept = _get_or_create_quarantine_concept(
        vocabulary_id=vocabulary_id,
        domain_id=domain_id,
        concept_class_id=concept_class_id,
        concept_code=_slug(name, prefix),
        concept_name=name,
    )
    mapping = _record_proposal(
        source_vocabulary_id=source_vocabulary_id,
        source_code=source_code,
        source_text=source_text,
        concept=concept,
        omop_table=table,
        source_system=source_system,
    )
    return concept, mapping


# --------------------------------------------------------------------------
# Re-pointing rows already stored
# --------------------------------------------------------------------------

def repoint_clinical_rows(*, mapping, old_concept_id, new_concept_id,
                          apply_changes=True):
    """Move stored clinical rows from one destination concept to another.

    Approving a mapping that a curator re-pointed has to rewrite the rows
    already in the database. Without this the decision only reaches data that
    happens to be imported again, and a patient whose bundle is never re-sent
    keeps the minted HK-* concept forever.

    Rows are matched on the mapping's source value **and** the old destination,
    never on the source value alone: a row whose concept somebody already
    corrected by hand must not be clobbered by a later approval.

    Derivation is deliberately not run here. The rewrite is a bulk UPDATE and
    is fast; re-deriving PatientRecord is 12-32s per bulk-loaded patient. So
    affected records are marked stale (``derivation_version=0``) and left for
    ``backfill_patient_records``, the same trade ``remap_shadow_concepts``
    makes.

    Returns ``{'rows_updated', 'persons_marked_stale', 'rows_collapsed'}``.
    """
    result = {'rows_updated': 0, 'persons_marked_stale': 0, 'rows_collapsed': 0}
    # `is None`, not falsiness: concept 0 is OMOP's "No matching concept" and is
    # the single most common value a re-point moves rows *off*.
    if old_concept_id is None or not new_concept_id or old_concept_id == new_concept_id:
        return result

    table = normalize_omop_table(mapping.omop_table)
    entry = CLINICAL_TABLES.get(table)
    if entry is None:
        logger.warning(
            'Mapping %s has no usable omop_table (%r); no rows re-pointed.',
            mapping.id, mapping.omop_table,
        )
        return result
    model, concept_col, source_col = entry

    # Ingest truncates every *_source_value to 50 chars, so a curator who typed
    # the full name would match nothing and the approval would silently move no
    # rows. Match on what was actually stored.
    stored_width = model._meta.get_field(source_col).max_length or 50
    qs = model.objects.filter(**{
        f'{source_col}__iexact': mapping.source_code[:stored_width],
        concept_col: old_concept_id,
    })
    person_ids = set(qs.values_list('person_id', flat=True).distinct())
    result['rows_updated'] = qs.count()
    if not apply_changes or not result['rows_updated']:
        result['persons_marked_stale'] = len(person_ids) if not apply_changes else 0
        return result

    with transaction.atomic():
        # Marked before the write: an abort mid-update would otherwise leave
        # rewritten rows whose PatientRecord is never selected for re-derivation,
        # because backfill_patient_records selects on derivation_version.
        result['persons_marked_stale'] = PatientRecord.objects.filter(
            person_id__in=person_ids,
        ).update(derivation_version=0)

        # post_delete receivers are live, and the collapse below deletes rows.
        with suppress_patient_record_refresh():
            qs.update(**{concept_col: new_concept_id})
            result['rows_collapsed'] = _collapse_duplicates(
                model, concept_col, source_col, mapping.source_code,
                new_concept_id, person_ids,
            )

    logger.info(
        'Mapping %s: re-pointed %s %s row(s) from concept %s to %s '
        '(%s collapsed, %s patient record(s) marked stale).',
        mapping.id, result['rows_updated'], table, old_concept_id,
        new_concept_id, result['rows_collapsed'], result['persons_marked_stale'],
    )
    return result


def _collapse_duplicates(model, concept_col, source_col, source_code,
                         concept_id, person_ids):
    """Collapse rows the re-point just made identical.

    "Identical" is the event identity CLAUDE.md documents for the bulk write
    path, not just (person, date). Measurement and Observation carry more of it
    on purpose: a patient legitimately has several distinct results for one
    analyte on one day, and keying on the date alone would delete real results
    rather than dedupe a re-point.
    """
    identity_cols = _COLLAPSE_IDENTITY[concept_col]
    pk_col = model._meta.pk.name

    rows = (
        model.objects
        .filter(person_id__in=person_ids,
                **{f'{source_col}__iexact': source_code, concept_col: concept_id})
        .order_by(pk_col)
        .values_list(pk_col, 'person_id', *identity_cols)
    )

    seen, doomed = set(), []
    for row in rows:
        pk, key = row[0], row[1:]
        if key in seen:
            doomed.append(pk)
        else:
            seen.add(key)
    if doomed:
        # Provenance points at these by object_id; leaving it behind would
        # strand rows referring to facts that no longer exist.
        content_type = ContentType.objects.get_for_model(model)
        ProvenanceRecord.objects.filter(
            content_type=content_type, object_id__in=doomed,
        ).delete()
        model.objects.filter(**{f'{pk_col}__in': doomed}).delete()
    return len(doomed)
