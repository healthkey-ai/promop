"""Source code -> destination OMOP concept resolution and re-pointing (#834).

Codes reach promop from FHIR bundles, parsed paper labs, and clinicians' notes.
They may be LOINC or SNOMED codes, ICD codes, a lab's in-house test name, or
free text.  All of them have to end up as an OMOP concept, and this module is
where that decision is made and recorded.

Resolution order — ``resolve_source_code``:

1. An **approved** mapping wins, over everything, including a direct concept
   lookup. SCCM is the governed source of source-code resolution, and an
   approved row is a curator's deliberate decision.
2. With no approved mapping, resolve a **LOINC or SNOMED** code directly to
   its Athena concept. These vocabularies retain their useful natural-key
   fallback, but never bypass SCCM.
3. With no approved mapping or direct concept, resolve as before (direct lookup on the source
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
from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Q, When
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


_DOMAIN_FOR_TABLE = {
    table: domain for table, (_v, domain, _c, _p) in _QUARANTINE_TARGETS.items()
}


def normalize_omop_table(omop_table):
    """Fold the aliases callers use onto the canonical table keys."""
    value = (omop_table or '').strip().lower()
    return {
        'condition_occurrence': 'condition',
        'drug': 'drug_exposure',
        'procedure_occurrence': 'procedure',
    }.get(value, value)


# source_code is CharField(100). A proposal stores the inbound value truncated
# to that, so every lookup has to truncate the same way -- otherwise a long
# free-text source is stored short, matched long, and the approved mapping never
# takes effect, which is the failure this whole feature exists to fix.
SOURCE_CODE_MAX = 100


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
        source_code__iexact=source_code[:SOURCE_CODE_MAX],
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
    source_code = source_code[:SOURCE_CODE_MAX]
    mapping = SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id=source_vocabulary_id or '',
        source_code__iexact=source_code,
    ).first()

    if mapping is None:
        try:
            # Savepointed: two ETL workers importing the same new code both miss
            # the filter above and both insert. Without this the loser's
            # IntegrityError escapes and fails the entire bundle with a 500 --
            # a normal condition for a parallel import, not an error.
            with transaction.atomic():
                return SourceCodeConceptMapping.objects.create(
                    source_vocabulary_id=source_vocabulary_id or '',
                    source_code=source_code,
                    source_code_description=(source_text or '')[:255],
                    # Without this a gap row carries no domain, and the UI has
                    # nothing to place it by -- it appeared in no tab at all.
                    domain_id=_DOMAIN_FOR_TABLE.get(omop_table, ''),
                    target_concept=concept,
                    destination_vocabulary_id=(concept.vocabulary_id or '') if concept else '',
                    omop_table=omop_table,
                    source=SOURCE_HEALTHKEY,
                    status='proposed',
                    origin='import',
                    origin_system=source_system,
                    occurrence_count=1,
                    first_seen=now,
                    last_seen=now,
                )
        except IntegrityError:
            mapping = SourceCodeConceptMapping.objects.filter(
                source_vocabulary_id=source_vocabulary_id or '',
                source_code__iexact=source_code,
            ).first()
            if mapping is None:
                raise

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

    # Rule 1 — SCCM is the primary resolver for every source vocabulary,
    # including LOINC and SNOMED. A curator-approved exception must not lose
    # to an automatic natural-key lookup.
    approved = approved_mapping_for(source_vocabulary_id, source_code)
    if approved is not None:
        return approved.target_concept, approved

    # Rule 2 — LOINC/SNOMED retain direct Athena lookup only as a fallback.
    if source_vocabulary_id in SELF_RESOLVING_VOCABULARIES:
        concept = _direct_concept(source_vocabulary_id, source_code)
        if concept is not None:
            return concept, None
        # ...unless that concept is not loaded on this deploy. Returning None
        # here would drop the code to concept 0 with nothing in the review
        # queue, and LOINC is the dominant source system for the labs this
        # feature is for -- so it would be the likeliest way a code goes
        # missing. Record the gap without minting: the right fix is a
        # vocabulary load, not a HealthKey concept shadowing a real LOINC one.
        # Returned, not discarded: the caller needs to know a queue entry now
        # exists for this code, and the ingest paths log against it.
        gap = _record_proposal(
            source_vocabulary_id=source_vocabulary_id,
            source_code=source_code,
            source_text=source_text,
            concept=None,
            omop_table=table,
            source_system=source_system,
        )
        return None, gap

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

def _source_value_match(model, source_col, mapping, include_description=True):
    """Match the clinical rows a mapping actually produced.

    A mapping's key and its rows' key are not always the same string. Ingest
    writes ``_source_text(codeable)`` into ``*_source_value`` -- the resource's
    display text -- while a proposal records the *code* where the resource
    carried one, because a code is stable across producers and display text is
    not. So a FHIR Observation coded ``SFLC-K`` with text
    ``SERUM FREE LIGHT CHAIN KAPPA`` yields a mapping keyed on the code and a
    measurement keyed on the text.

    Matching on the code alone found nothing, and an approval then reported
    success while moving no rows -- the silent failure this whole feature
    exists to prevent. Match on either, since ingest may have stored either.

    Also truncated to the stored column width: ingest caps source values at 50
    characters, so a curator who typed the full name would otherwise miss.
    """
    width = model._meta.get_field(source_col).max_length or 50
    candidates = {mapping.source_code[:width]} if mapping.source_code else set()
    # The description only joins the key for *import*-created proposals, where
    # it holds the display text ingest actually wrote. On a curator-created row
    # it is free prose -- "Glucose" against a GLU-3 code -- and using it as a
    # bulk-UPDATE key would re-point every unrelated producer's "Glucose" row in
    # the database and mark those patients stale.
    #
    # `include_description=False` narrows it further for the concept-0 sweep.
    # An import's *own* rows sit at the concept it minted, not at 0, so the
    # old-destination sweep is what moves them and it keeps the description.
    # The concept-0 sweep reaches rows the import never wrote -- pre-existing
    # unresolved data -- and matching those on a generic display string like
    # "Glucose" would claim every producer's unresolved Glucose row for this
    # one code.
    if include_description and mapping.origin == 'import' and mapping.source_code_description:
        candidates.add(mapping.source_code_description[:width])
    match = Q()
    for value in candidates:
        match |= Q(**{f'{source_col}__iexact': value})
    return match


def repoint_clinical_rows(*, mapping, old_concept_id, new_concept_id,
                          apply_changes=True, match_description=True):
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
    # person_ids is carried so a caller running more than one sweep can union
    # them; counting each sweep's total would report more affected patients
    # than exist when a person has rows in both.
    result = {'rows_updated': 0, 'persons_marked_stale': 0, 'rows_collapsed': 0,
              'person_ids': set()}
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

    match = _source_value_match(
        model, source_col, mapping, include_description=match_description)
    qs = model.objects.filter(match, **{concept_col: old_concept_id})
    person_ids = set(qs.values_list('person_id', flat=True).distinct())
    result['rows_updated'] = qs.count()
    if not apply_changes or not result['rows_updated']:
        result['person_ids'] = set(person_ids) if not apply_changes else set()
        result['persons_marked_stale'] = len(person_ids) if not apply_changes else 0
        return result

    with transaction.atomic():
        # Marked before the write: an abort mid-update would otherwise leave
        # rewritten rows whose PatientRecord is never selected for re-derivation,
        # because backfill_patient_records selects on derivation_version.
        result['person_ids'] = set(person_ids)
        result['persons_marked_stale'] = PatientRecord.objects.filter(
            person_id__in=person_ids,
        ).update(derivation_version=0)

        # post_delete receivers are live, and the collapse below deletes rows.
        with suppress_patient_record_refresh():
            qs.update(**{concept_col: new_concept_id})
            result['rows_collapsed'] = _collapse_duplicates(
                model, concept_col, source_col, match,
                new_concept_id, person_ids,
            )

    logger.info(
        'Mapping %s: re-pointed %s %s row(s) from concept %s to %s '
        '(%s collapsed, %s patient record(s) marked stale).',
        mapping.id, result['rows_updated'], table, old_concept_id,
        new_concept_id, result['rows_collapsed'], result['persons_marked_stale'],
    )
    return result


def _collapse_duplicates(model, concept_col, source_col, match,
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
        .filter(match, person_id__in=person_ids, **{concept_col: concept_id})
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
