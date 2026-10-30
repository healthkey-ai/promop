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
from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Q, When
from django.utils import timezone

from omop_core.models import (
    Concept,
    ConditionOccurrence,
    DrugExposure,
    Measurement,
    Observation,
    PatientRecord,
    PatientSourceCode,
    ProcedureOccurrence,
    ProvenanceRecord,
    SourceCodeConceptMapping,
)
from omop_core.services.pk import next_pk
from omop_core.mapping.therapy import (
    SOURCE_HEALTHKEY,
    _get_or_create_quarantine_concept,
    _slug,
)
from omop_core.signals import suppress_patient_record_refresh
from omop_core.services.provenance_attribution import ATTRIBUTION, exclusively_attributed, with_attribution
from omop_core.services.source_vocabularies import VOCABULARY_OID_ALIASES, canonical_source_vocabulary
from omop_core.services import source_vocabularies
from omop_core.data_migrations.snomed_relationships_v1 import SINGLE_ORIGIN, MULTIPLE_ORIGIN
from omop_core.services.snomed_identity import IDENTITY_ORIGIN, is_standard_snomed, promote_crossmap_identity
from omop_core.services.source_vocabularies import table_for_domain

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

# Fields needed to *create* a new clinical row, per table. Keyed on the
# canonical table name (same keys as CLINICAL_TABLES). Each tuple is:
#   (pk_field, concept_fk_field, source_value_field, date_field, type_concept_fk_field)
# where *_fk_field names the Django FK attribute (not the raw _id column).
_CREATE_FIELDS = {
    'measurement':   ('measurement_id',         'measurement_concept',  'measurement_source_value',  'measurement_date',         'measurement_type_concept'),
    'observation':   ('observation_id',          'observation_concept',  'observation_source_value',  'observation_date',          'observation_type_concept'),
    'condition':     ('condition_occurrence_id', 'condition_concept',    'condition_source_value',    'condition_start_date',      'condition_type_concept'),
    'drug_exposure': ('drug_exposure_id',        'drug_concept',         'drug_source_value',         'drug_exposure_start_date',  'drug_type_concept'),
    'procedure':     ('procedure_occurrence_id', 'procedure_concept',    'procedure_source_value',    'procedure_date',            'procedure_type_concept'),
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
_QUARANTINE_VOCABULARIES: frozenset[str] = frozenset(
    vocabulary_id for vocabulary_id, *_ in _QUARANTINE_TARGETS.values()
)


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


def _mapping_scope(source_vocabulary_id, source_code, organization=None):
    """Mappings eligible for this encounter, safest choice first.

    Hospital-local systems must never fall through to another institution's
    row.  With organization context they require an exact organization match;
    without it, legacy global rows remain available for backward compatibility.
    Non-hospital vocabularies remain global.
    """
    queryset = SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id=source_vocabulary_id or '',
        source_code__iexact=source_code[:SOURCE_CODE_MAX],
    )
    if organization is None:
        return queryset.filter(organization__isnull=True)
    if source_vocabularies.hospital_vendor(source_vocabulary_id):
        return queryset.filter(organization=organization)
    return queryset.filter(organization__isnull=True)


def approved_mapping_for(source_vocabulary_id, source_code, organization=None):
    """Return the approved mapping for this code, or None.

    Matched case-insensitively on the code: an uncoded source is a lab's or a
    clinician's free text, and 'M-Protein, Serum' and 'M-PROTEIN, SERUM' are
    the same test.
    """
    if not source_code:
        return None
    source_vocabulary_id = _resolution_vocabulary(source_vocabulary_id, source_code)
    return _mapping_scope(
        source_vocabulary_id, source_code, organization,
    ).filter(status='approved').select_related('target_concept').first()


def _resolution_vocabulary(vocabulary_id, source_code):
    """Accept OIDs while respecting legacy exceptions retained by migration."""
    canonical = canonical_source_vocabulary(vocabulary_id)
    if vocabulary_id in VOCABULARY_OID_ALIASES and SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id=vocabulary_id,
        source_code__iexact=source_code[:SOURCE_CODE_MAX],
    ).exists():
        return vocabulary_id
    return canonical


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


def _description_for(source_vocabulary_id, source_code, source_text):
    """The display text for a new queue row.

    FHIR sync passes the code itself when a Coding has no display, and the
    resolve endpoint and crossmap importers pass nothing; either way the
    curator would meet a bare code (#1464). Ask the vocabularies before
    settling for that.
    """
    from omop_core.services.source_descriptions import describe_source_code

    text = (source_text or '')[:255]
    if text and text != source_code:
        return text
    return describe_source_code(source_vocabulary_id or '', source_code) or text


def _has_patient_rows(mapping, organization=None):
    """Whether this instance holds per-patient rows for the mapping's code:
    then its Seen count is theirs, not the lookups' (#2123). A hospital-scoped
    mapping asks about its hospital's patients."""
    rows = PatientSourceCode.objects.filter(
        source_vocabulary_id=mapping.source_vocabulary_id or '', source_value__iexact=mapping.source_code)
    if mapping.organization_id is not None:
        rows = rows.filter(person__patient_record__organization_id=mapping.organization_id)
    return rows.exists()


def _record_proposal(*, source_vocabulary_id, source_code, source_text,
                     concept, omop_table, source_system, organization=None,
                     notes='', is_suggestion=False):
    """Create or bump the proposed mapping for a code an import had to invent.

    Idempotent: the first sighting creates the row, later sightings bump
    ``last_seen``, and ``occurrence_count`` while the code has no per-patient
    rows on this instance, so the review queue can be ordered by how much a
    code actually matters before its patients' source codes are uploaded.
    That count is provisional: a lookup is one ask per code per batch, not
    one per record, and once the per-patient rows exist they are the count
    (``axis_variants.rebuild_seen_counts`` after each upload and in the
    rebuild walk, #2123), so a sighting no longer adds to it. Never touches
    an approved row — that would let an import quietly undo a curator.
    """
    now = timezone.now()
    # Truncated before the lookup as well as the create. Looking up the full
    # string but storing 100 chars means every later sighting of a longer source
    # text misses, re-enters the create branch, and trips the unique constraint.
    source_code = source_code[:SOURCE_CODE_MAX]
    mapping = _mapping_scope(
        source_vocabulary_id, source_code, organization,
    ).filter(organization=organization).first()

    if mapping is None:
        try:
            # Savepointed: two ETL workers importing the same new code both miss
            # the filter above and both insert. Without this the loser's
            # IntegrityError escapes and fails the entire bundle with a 500 --
            # a normal condition for a parallel import, not an error.
            with transaction.atomic():
                return SourceCodeConceptMapping.objects.create(
                    organization=organization,
                    source_vocabulary_id=source_vocabulary_id or '',
                    source_code=source_code,
                    source_code_description=_description_for(
                        source_vocabulary_id, source_code, source_text,
                    ),
                    # Without this a gap row carries no domain, and the UI has
                    # nothing to place it by -- it appeared in no tab at all.
                    domain_id=_DOMAIN_FOR_TABLE.get(omop_table, ''),
                    target_concept=concept,
                    suggested_target_concept=concept if is_suggestion else None,
                    destination_vocabulary_id=(concept.vocabulary_id or '') if concept else '',
                    omop_table=omop_table,
                    source=SOURCE_HEALTHKEY,
                    status='proposed',
                    origin='import',
                    origin_system=source_system,
                    occurrence_count=1,
                    first_seen=now,
                    last_seen=now,
                    notes=notes,
                )
        except IntegrityError:
            mapping = _mapping_scope(
                source_vocabulary_id, source_code, organization,
            ).filter(organization=organization).first()
            if mapping is None:
                raise

    if mapping.status == 'approved':
        return mapping

    # This is called by concurrent import workers.  Reading, adding one in
    # Python, and saving loses sightings under load; keep Seen a real counter.
    updates = {'last_seen': now}
    if not _has_patient_rows(mapping, organization):
        updates['occurrence_count'] = F('occurrence_count') + 1
    if mapping.first_seen is None:
        updates['first_seen'] = now
    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(**updates)
    mapping.refresh_from_db(fields=['occurrence_count', 'first_seen', 'last_seen'])
    return mapping


def _effective_direct_mapping(*, source_vocabulary_id, source_code, concept, omop_table):
    """Cache an authoritative direct vocabulary hit in SCCM.

    ``approved`` remains the effective state for backwards compatibility, but
    ``origin_system`` makes this a refreshable Athena-derived cache entry, not
    a curator sign-off.  A concurrent creator, or any existing curator row,
    always wins rather than being overwritten.
    """
    source_code = source_code[:SOURCE_CODE_MAX]
    existing = SourceCodeConceptMapping.objects.filter(
        organization__isnull=True, source_vocabulary_id=source_vocabulary_id or '',
        source_code__iexact=source_code,
    ).select_related('target_concept').first()
    if existing is not None:
        return existing
    standard_identity = source_vocabulary_id == 'SNOMED' and is_standard_snomed(concept)
    try:
        with transaction.atomic():
            return SourceCodeConceptMapping.objects.create(
                organization=None,
                source_vocabulary_id=source_vocabulary_id or '',
                source_code=source_code,
                source_code_description=(concept.concept_name or '')[:255],
                source_concept=concept,
                target_concept=concept,
                destination_vocabulary_id=concept.vocabulary_id or '',
                domain_id=concept.domain_id or _DOMAIN_FOR_TABLE.get(omop_table, ''),
                omop_table=table_for_domain(concept.domain_id) if standard_identity else omop_table,
                source='Athena', status='approved', origin='import',
                origin_system=IDENTITY_ORIGIN if standard_identity else 'athena-direct', occurrence_count=0,
            )
    except IntegrityError:
        return SourceCodeConceptMapping.objects.filter(
            organization__isnull=True, source_vocabulary_id=source_vocabulary_id or '',
            source_code__iexact=source_code,
        ).select_related('target_concept').get()


def _promote_if_loaded(
    pending: SourceCodeConceptMapping, source_vocabulary_id: str, source_code: str,
) -> SourceCodeConceptMapping | None:
    """Promote a proposal that was queued only because its vocabulary was missing.

    Import writes a placeholder or an empty gap when the vocabulary is not
    loaded. Without this the queue row blocks the direct lookup forever, so a
    later vocabulary load never reaches the code. Returns the promoted mapping,
    or None when the row must stay in the queue.
    """
    if not source_vocabulary_id or not _waiting_for_vocabulary(pending):
        return None
    concept = _direct_concept(source_vocabulary_id, source_code)
    if not _usable_destination(concept, pending.omop_table):
        return None
    with transaction.atomic():
        mapping = (
            SourceCodeConceptMapping.objects.select_for_update(of=('self',))
            .select_related('target_concept').filter(pk=pending.pk).first()
        )
        # A curator may have rejected or edited the row since it was read.
        if mapping is None or not _waiting_for_vocabulary(mapping):
            return None
        mapping.source_concept = concept
        mapping.target_concept = concept
        mapping.destination_vocabulary_id = concept.vocabulary_id or ''
        mapping.domain_id = concept.domain_id
        mapping.source = 'Athena'
        mapping.status = 'approved'
        mapping.origin_system = 'athena-direct'
        mapping.save(update_fields=[
            'source_concept', 'target_concept', 'destination_vocabulary_id',
            'domain_id', 'source', 'status', 'origin_system', 'updated_at',
        ])
    return mapping


def _waiting_for_vocabulary(mapping: SourceCodeConceptMapping) -> bool:
    """True for an untouched import proposal holding only a placeholder or a gap."""
    if mapping.origin_system == MULTIPLE_ORIGIN or mapping.status != 'proposed' or mapping.origin != 'import':
        return False
    if mapping.reviewer_id is not None or mapping.suggested_target_concept_id is not None:
        return False
    target = mapping.target_concept
    return target is None or (
        target.source == SOURCE_HEALTHKEY and target.vocabulary_id in _QUARANTINE_VOCABULARIES
    )


def _usable_destination(concept: Concept | None, omop_table: str) -> bool:
    """A standard valid concept in the domain of the table the rows are written to.

    Non-standard hits like an RxNorm Brand Name and cross-domain hits like a
    LOINC Observation code for a measurement stay with a curator.
    """
    return (
        concept is not None
        and concept.standard_concept == 'S'
        and not concept.invalid_reason
        and concept.domain_id == _DOMAIN_FOR_TABLE.get(normalize_omop_table(omop_table))
    )


def _identity_for_table(mapping, table):
    """An automatic identity is only usable in its real clinical domain."""
    target = mapping.target_concept
    if (mapping.status != 'approved' or not _usable_destination(target, table)
            or not target.valid_start_date <= date.today() <= target.valid_end_date):
        return None, mapping
    return target, mapping


def resolve_source_code(*, source_code, omop_table, source_vocabulary_id='',
                        source_text='', source_system='fhir-upload', organization=None,
                        source_unit=None):
    """Resolve an inbound source code to a destination concept.

    Implements the four rules in the module docstring. Returns
    ``(concept, mapping_or_None)``; ``concept`` is None only when there is
    nothing to resolve (no code at all).

    *source_unit* is the record's unit. A code whose units imply more than one
    set of LOINC axes has an approved variant per set, and a record in one of
    those units resolves to that variant's concept instead of the mapping's
    (:mod:`omop_core.mapping.axis_variants`).
    """
    concept, mapping, _variant = resolve_source_code_variant(
        source_code=source_code, omop_table=omop_table,
        source_vocabulary_id=source_vocabulary_id, source_text=source_text,
        source_system=source_system, organization=organization,
        source_unit=source_unit,
    )
    return concept, mapping


def resolve_source_code_variant(*, source_code, omop_table, source_vocabulary_id='',
                                source_text='', source_system='fhir-upload',
                                organization=None, source_unit=None, metadata=None,
                                mint=True):
    """:func:`resolve_source_code`, also returning the axis variant that
    routed the record (``None`` when the mapping's own concept applies).

    *metadata* is what the caller knows about the record (unit, value,
    specimen) in the shape :mod:`loinc_axes` reads; it steers the suggestion
    made for a new code. *mint* False queues a code nothing matched with no
    destination instead of a placeholder concept: right for a document term
    (:mod:`text_terms`), where a placeholder per phrasing would be noise.
    """
    concept, mapping = _resolve_source_code(
        source_code=source_code, omop_table=omop_table,
        source_vocabulary_id=source_vocabulary_id, source_text=source_text,
        source_system=source_system, organization=organization,
        metadata=metadata, mint=mint,
    )
    if mapping is not None and source_unit:
        from omop_core.mapping.axis_variants import variant_for
        variant = variant_for(mapping, source_unit)
        if variant is not None:
            return variant.target_concept, mapping, variant
    return concept, mapping, None


def _resolve_source_code(*, source_code, omop_table, source_vocabulary_id,
                         source_text, source_system, organization,
                         metadata=None, mint=True):
    source_code = (source_code or '').strip()
    if not source_code:
        return None, None
    source_vocabulary_id = _resolution_vocabulary(source_vocabulary_id, source_code)
    table = normalize_omop_table(omop_table)
    mapping_organization = (
        organization if source_vocabularies.hospital_vendor(source_vocabulary_id) else None
    )

    # Rule 1 — SCCM is the primary resolver for every source vocabulary,
    # including LOINC, SNOMED, and CPT4. A curator-approved exception must not
    # lose to an automatic natural-key lookup.
    approved = approved_mapping_for(
        source_vocabulary_id, source_code, mapping_organization,
    )
    if approved is not None:
        if approved.origin_system in (IDENTITY_ORIGIN, SINGLE_ORIGIN):
            return _identity_for_table(approved, table)
        return approved.target_concept, approved

    # A proposed target is a review aid, never an effective mapping.  Once a
    # code is in the queue, every ingest caller receives the same unresolved
    # answer and the proposal's Seen count records that encounter.
    pending = _mapping_scope(
        source_vocabulary_id, source_code, mapping_organization,
    ).filter(status='proposed').select_related('target_concept').first()
    if pending is not None:
        identity = promote_crossmap_identity(pending)
        if identity is not None:
            return _identity_for_table(identity, table)
        promoted = _promote_if_loaded(pending, source_vocabulary_id, source_code)
        if promoted is not None:
            return promoted.target_concept, promoted
        return None, _record_proposal(
            source_vocabulary_id=source_vocabulary_id,
            source_code=source_code,
            source_text=source_text,
            concept=pending.target_concept,
            omop_table=table,
            source_system=source_system,
            organization=mapping_organization,
        )

    # Rule 2 — LOINC/SNOMED retain direct Athena lookup only as a fallback.
    if source_vocabulary_id in SELF_RESOLVING_VOCABULARIES:
        concept = _direct_concept(source_vocabulary_id, source_code)
        if source_vocabulary_id == 'SNOMED' and not is_standard_snomed(concept):
            concept = None
        if concept is not None:
            mapping = _effective_direct_mapping(
                source_vocabulary_id=source_vocabulary_id,
                source_code=source_code, concept=concept, omop_table=table,
            )
            if source_vocabulary_id == 'SNOMED':
                return _identity_for_table(mapping, table)
            return concept, mapping
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
            organization=mapping_organization,
        )
        return None, gap

    # Rule 3a — resolve against Athena as before.
    concept = _direct_concept(source_vocabulary_id, source_code)
    if concept is not None:
        return concept, _effective_direct_mapping(
            source_vocabulary_id=source_vocabulary_id,
            source_code=source_code, concept=concept, omop_table=table,
        )

    # Rule 4 — the suggestion service owns every retrieval/ranking strategy.
    # Its returned target is useful evidence for a curator but never an answer
    # an importer may use before approval.
    from omop_core.mapping.suggestions import suggest_source_code
    suggested, note = suggest_source_code(
        source_vocabulary_id=source_vocabulary_id,
        source_code=source_code,
        source_text=source_text,
        omop_table=table,
        metadata=metadata,
    )
    if suggested is not None:
        mapping = _record_proposal(
            source_vocabulary_id=source_vocabulary_id,
            source_code=source_code,
            source_text=source_text,
            concept=suggested,
            omop_table=table,
            source_system=source_system,
            organization=mapping_organization,
            notes=note,
            is_suggestion=True,
        )
        return None, mapping

    # Rule 5 — mint only after direct resolution and suggestion both fail.
    if not mint:
        return None, _record_proposal(
            source_vocabulary_id=source_vocabulary_id,
            source_code=source_code,
            source_text=source_text,
            concept=None,
            omop_table=table,
            source_system=source_system,
            organization=mapping_organization,
            notes=note,
        )
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
        organization=mapping_organization,
        notes=note,
    )
    return None, mapping


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
                          apply_changes=True, match_description=True,
                          person_id=None, units=None):
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

    *units* limits the move to rows whose ``unit_source_value`` normalizes to
    one of them -- an axis variant (``mapping.axis_variants``) owns only the
    records in its units. Tables without a unit column move nothing then.

    Returns ``{'rows_updated', 'persons_marked_stale', 'rows_collapsed'}``.
    """
    # person_ids is carried so a caller running more than one sweep can union
    # them; counting each sweep's total would report more affected patients
    # than exist when a person has rows in both.
    result = {'rows_updated': 0, 'persons_marked_stale': 0, 'rows_collapsed': 0,
              'person_ids': set()}
    # `is None`, not falsiness: concept 0 is OMOP's "No matching concept" and is
    # the single most common value a re-point moves rows *off*.
    if old_concept_id is None or new_concept_id is None or old_concept_id == new_concept_id:
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
    if person_id is not None:
        qs = qs.filter(person_id=person_id)
    if units is not None:
        if not any(f.name == 'unit_source_value' for f in model._meta.concrete_fields):
            return result
        from omop_core.services.canonical_units import unit_code
        wanted = set(units)
        spellings = [
            raw for raw in qs.values_list('unit_source_value', flat=True).distinct()
            if raw and unit_code(raw) in wanted
        ]
        qs = qs.filter(unit_source_value__in=spellings)
    if mapping.organization_id:
        # A shared Epic flowsheet code can mean different things at two
        # hospitals.  Only rows whose write provenance names this mapping's
        # organization are safe to move; historical rows with no attribution
        # deliberately remain unresolved rather than being guessed.
        qs = exclusively_attributed(qs, mapping.organization_id)
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
            # Snapshot PKs before the concept update: the lazy queryset filters
            # on old_concept_id, so after .update() it would find zero rows.
            repointed_pks = set(qs.values_list('pk', flat=True)) if model is Measurement else None
            qs.update(**{concept_col: new_concept_id})
            result['rows_collapsed'] = _collapse_duplicates(
                model, concept_col, source_col, match,
                new_concept_id, person_ids,
                organization_id=mapping.organization_id,
            )

            # Normalize units on repointed Measurement rows (#2088).
            # When units is set this is a variant approval: the variant's
            # target concept (new_concept_id) differs from the mapping's,
            # and the mapping's destination_unit_concept was chosen for its
            # own LOINC, not the variant's.
            if repointed_pks:
                try:
                    from omop_core.services.canonical_units import (
                        normalize_measurement_units,
                    )
                    norm_qs = model.objects.filter(pk__in=repointed_pks)
                    variant_concept = new_concept_id if units is not None else None
                    result['rows_normalized'] = normalize_measurement_units(
                        norm_qs, mapping,
                        target_concept_id=variant_concept)
                    # Re-collapse: normalization may have made previously-
                    # distinct values identical (e.g. 100.4 and 100.6 umol/L
                    # both round to 0.1 mmol/L).
                    if result['rows_normalized']:
                        result['rows_collapsed'] += _collapse_duplicates(
                            model, concept_col, source_col, match,
                            new_concept_id, person_ids,
                            organization_id=mapping.organization_id,
                        )
                except Exception:
                    logger.exception(
                        'Unit normalization failed for mapping %s',
                        mapping.id,
                    )

    logger.info(
        'Mapping %s: re-pointed %s %s row(s) from concept %s to %s '
        '(%s collapsed, %s normalized, %s patient record(s) marked stale).',
        mapping.id, result['rows_updated'], table, old_concept_id,
        new_concept_id, result['rows_collapsed'],
        result.get('rows_normalized', 0), result['persons_marked_stale'],
    )
    return result


def _collapse_duplicates(model, concept_col, source_col, match,
                         concept_id, person_ids, organization_id=None):
    """Collapse rows the re-point just made identical.

    "Identical" is the event identity CLAUDE.md documents for the bulk write
    path, not just (person, date). Measurement and Observation carry more of it
    on purpose: a patient legitimately has several distinct results for one
    analyte on one day, and keying on the date alone would delete real results
    rather than dedupe a re-point.
    """
    identity_cols = _COLLAPSE_IDENTITY[concept_col]
    pk_col = model._meta.pk.name

    candidates = model.objects.filter(
        match, person_id__in=person_ids, **{concept_col: concept_id},
    )
    if organization_id:
        candidates = exclusively_attributed(candidates, organization_id)
    rows = (
        with_attribution(candidates)
        .order_by(pk_col)
        .values_list(pk_col, ATTRIBUTION, 'person_id', *identity_cols)
    )

    seen, doomed = set(), []
    for row in rows:
        pk, key = row[0], (tuple(row[1]),) + row[2:]
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


# ------------------------------------------------------------------
# Create clinical rows for source codes that have no OMOP row yet
# ------------------------------------------------------------------

# EHR type concept (concept_id 32817). Used as the type concept for rows
# created by Generate OMOP, matching what the FHIR sync writes.
_EHR_TYPE_CONCEPT_ID = 32817


def _create_missing_clinical_rows(person, *, omop_tables=None, source_values=None):
    """Create clinical rows for PatientSourceCode entries with no existing row.

    Only creates rows where an approved SCCM mapping exists and no clinical row
    with the same source value exists for this person in the target table.

    *source_values*: optional list of source strings to limit creation to.
    *omop_tables*: optional list of table keys (e.g. ``['measurement']``).

    Returns ``(created_count, detail_list)`` where *detail_list* contains one
    dict per processed PSC entry.
    """
    psc_qs = PatientSourceCode.objects.filter(person=person)
    if source_values:
        psc_qs = psc_qs.filter(source_value__in=source_values)
    if not psc_qs.exists():
        return 0, []

    try:
        ehr_type_concept = Concept.objects.get(concept_id=_EHR_TYPE_CONCEPT_ID)
    except Concept.DoesNotExist:
        logger.warning(
            'Concept %d (EHR type) not loaded — cannot create clinical rows',
            _EHR_TYPE_CONCEPT_ID,
        )
        return 0, []

    created = 0
    details: list[dict] = []

    with transaction.atomic():
        for psc in psc_qs.iterator():
            table_key = normalize_omop_table(psc.omop_table)
            if table_key not in _CREATE_FIELDS:
                continue
            if omop_tables and table_key not in omop_tables:
                continue

            pk_field, concept_fk, source_col, date_col, type_fk = _CREATE_FIELDS[table_key]
            model, _concept_id_col, _sv_col = CLINICAL_TABLES[table_key]

            sv = psc.source_value

            # Look up the approved SCCM mapping.
            mapping = (
                SourceCodeConceptMapping.objects
                .filter(
                    source_code__iexact=sv[:SOURCE_CODE_MAX],
                    omop_table=table_key,
                    status='approved',
                )
                .select_related('target_concept')
                .first()
            )
            if not mapping or not mapping.target_concept_id:
                continue

            # Skip if any clinical row with this source value already exists
            # for this person (the repoint pass handles those).
            if model.objects.filter(person=person, **{f'{source_col}__iexact': sv}).exists():
                continue

            # Allocate PK and create the row.
            row_pk = next_pk(model, pk_field)
            row_date = psc.first_seen.date() if psc.first_seen else date.today()
            model.objects.create(**{
                pk_field: row_pk,
                'person': person,
                concept_fk: mapping.target_concept,
                source_col: sv,
                date_col: row_date,
                type_fk: ehr_type_concept,
            })
            created += 1
            details.append({
                'source_value': sv,
                'table': table_key,
                'rows': 1,
                'outcome': 'created',
                'target': mapping.target_concept.concept_name or str(mapping.target_concept_id),
                'target_concept_id': mapping.target_concept_id,
            })

    return created, details


# ------------------------------------------------------------------
# Per-patient source code resolution
# ------------------------------------------------------------------

def resolve_person_source_codes(person, *, source_values=None, omop_tables=None):
    """Re-resolve clinical rows for *person* with unresolved or prior targets.

    Uses the current approved SCCM mappings.  Returns a dict with counts:
    ``{'resolved': N, 'skipped': N, 'already_resolved': N}``.

    *source_values*: optional list of source strings to limit resolution to.
    *omop_tables*: optional list of table keys (e.g. ``['measurement']``).
    """
    from django.db.models import Count

    resolved = 0
    skipped = 0
    already_resolved = 0
    code_details: list[dict] = []
    person_ids_touched: set[int] = set()

    # Concept IDs in quarantine vocabularies (HK-*).
    quarantine_concept_ids = frozenset(
        Concept.objects.filter(vocabulary_id__in=_QUARANTINE_VOCABULARIES)
        .values_list('concept_id', flat=True)
    ) if _QUARANTINE_VOCABULARIES else frozenset()

    tables = CLINICAL_TABLES
    if omop_tables:
        tables = {k: v for k, v in tables.items() if k in omop_tables}

    for table_key, (model, concept_col, source_col) in tables.items():
        qs = model.objects.filter(person=person).exclude(
            **{f'{source_col}__isnull': True},
        ).exclude(**{source_col: ''})

        if source_values:
            qs = qs.filter(**{f'{source_col}__in': source_values})

        # Group by source_value to avoid per-row mapping lookups.
        groups = list(
            qs.values(source_col)
            .annotate(row_count=Count('pk'))
            .order_by(source_col)
        )

        for group in groups:
            sv = group[source_col]

            # Find an approved mapping for this source value.
            # Query SCCM directly rather than going through
            # approved_mapping_for() which requires a vocabulary_id
            # that clinical rows don't store.
            mapping = (
                SourceCodeConceptMapping.objects
                .filter(
                    source_code__iexact=sv,
                    omop_table=table_key,
                    status='approved',
                )
                .select_related('target_concept', 'destination_unit_concept')
                .first()
            )
            if mapping is None:
                skipped += group['row_count']
                code_details.append({
                    'source_value': sv, 'table': table_key,
                    'rows': group['row_count'], 'outcome': 'skipped',
                    'reason': 'no approved mapping',
                })
                continue

            target_concept_id = mapping.target_concept_id
            if not target_concept_id:
                skipped += group['row_count']
                code_details.append({
                    'source_value': sv, 'table': table_key,
                    'rows': group['row_count'], 'outcome': 'skipped',
                    'reason': 'mapping has no target concept',
                })
                continue

            # Patient-scoped approvals leave prior targets for Generate OMOP.
            # This includes standard-to-standard remaps that a 0/HK-only
            # resolver would otherwise never pick up for other patients.
            unresolved_q = Q(**{concept_col: NO_MATCHING_CONCEPT_ID})
            if quarantine_concept_ids:
                unresolved_q |= Q(**{f'{concept_col}__in': quarantine_concept_ids})
            if mapping.deferred_repoint_concept_ids:
                unresolved_q |= Q(**{f'{concept_col}__in': mapping.deferred_repoint_concept_ids})

            rows_to_update = qs.filter(**{source_col: sv}).filter(unresolved_q)

            count = rows_to_update.count()
            if count == 0:
                already_resolved += group['row_count']
                code_details.append({
                    'source_value': sv, 'table': table_key,
                    'rows': group['row_count'], 'outcome': 'already_resolved',
                    'target': mapping.target_concept.concept_name if mapping.target_concept else str(target_concept_id),
                })
                continue

            with transaction.atomic():
                with suppress_patient_record_refresh():
                    # Snapshot PKs before the concept update: the lazy queryset
                    # filters on unresolved_q, so after .update() it finds zero.
                    resolved_pks = (
                        set(rows_to_update.values_list('pk', flat=True))
                        if model is Measurement else None
                    )
                    rows_to_update.update(**{concept_col: target_concept_id})
                    # Normalize units on resolved Measurement rows (#2088).
                    # Wrapped in try/except so a normalization failure (e.g.
                    # numeric overflow in bulk_update) does not roll back the
                    # concept update or abort resolution for remaining groups.
                    if resolved_pks:
                        try:
                            from omop_core.services.canonical_units import (
                                normalize_measurement_units,
                            )
                            norm_qs = model.objects.filter(pk__in=resolved_pks)
                            normalize_measurement_units(norm_qs, mapping)
                        except Exception:
                            logger.exception(
                                'Unit normalization failed for %s (mapping %s)',
                                sv, mapping.id,
                            )
                person_ids_touched.add(person.person_id)
                resolved += count
                already_resolved += group['row_count'] - count
                target_name = mapping.target_concept.concept_name if mapping.target_concept else str(target_concept_id)
                code_details.append({
                    'source_value': sv, 'table': table_key,
                    'rows': count, 'outcome': 'resolved',
                    'target': target_name,
                    'target_concept_id': target_concept_id,
                })

    # Create clinical rows for PSC entries that have no existing row.
    with suppress_patient_record_refresh():
        rows_created, creation_details = _create_missing_clinical_rows(
            person, omop_tables=omop_tables, source_values=source_values,
        )
    if rows_created:
        person_ids_touched.add(person.person_id)
    code_details.extend(creation_details)

    # Mark PatientRecord stale and dispatch derivation asynchronously.
    # The mapping updates above are fast; the derivation (12-32s on bulk-loaded
    # patients) runs on Celery when a broker is configured, or inline with a
    # statement_timeout otherwise — either way, outside this request.
    if person_ids_touched:
        PatientRecord.objects.filter(
            person_id__in=person_ids_touched,
        ).update(derivation_version=0)
        from omop_core.services.derivation_jobs import get_dispatcher
        get_dispatcher().dispatch(person)

    return {
        'resolved': resolved,
        'created': rows_created,
        'skipped': skipped,
        'already_resolved': already_resolved,
        'code_details': code_details,
    }
