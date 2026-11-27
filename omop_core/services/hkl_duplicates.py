"""Remove the auto-created HK-Labs (``hkl:``) concepts (#2922).

``hkl:<slug>`` concepts are local, non-standard concepts that were created
automatically for a lab name with no LOINC match (see docs/vocabularies.md).
They polluted concept search (``hkl:psa`` ahead of LOINC 2857-1) and invited
mappings to the wrong concept. A concept that has no standard equivalent is
minted by a curator through ``code-mappings/mint-destination/``, never created
by an importer, so none of them is kept. This module holds

* the table of the eighteen that duplicate a standard serum/plasma LOINC test
  (:data:`RETIRED_HKL_DUPLICATES`); a reference to one of those is repointed;
* the cleanup itself (:func:`build_plan`, :func:`apply_plan`), shared by the
  read-only ``audit_hkl_duplicates`` command and migration 0380 so the audit
  cannot disagree with what the migration does. A reference to any other
  concept is cleared, never guessed: a measurement's source concept becomes 0,
  a mapping loses its destination and returns to the curators' queue (an
  approved mapping is demoted to proposed, with a note);
* the guards that stop one being created again
  (:func:`is_removed_hkl_concept`, :func:`retired_standard_concept`,
  :func:`find_standard_duplicate`).

The cleanup functions take a model registry (``django.apps.apps`` or a
migration's ``apps``) and import no model themselves: a migration must run
against the schema of its own point in history. They use no other promop code.
A later change to this module does not re-run the migration on a database that
already applied it, and on a fresh database there is nothing to remove.

What counts as a reference is found by looking at the models, not listed: every
foreign key to ``Concept`` on every model. Columns that hold a concept id or a
concept code without a foreign key are listed in :data:`ID_COLUMNS`,
:data:`JSON_ID_LISTS` and :data:`CODE_COLUMNS`, which a test holds to the real
models.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from django.db import DatabaseError, connections, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

HK_LABS_VOCABULARY = 'HK-Labs'
STANDARD_VOCABULARY = 'LOINC'
HKL_PREFIX = 'hkl:'
# ``hkl:fallback-<id>`` concepts stand in for a standard visit or type concept
# when Athena is not loaded (lab_results/sync.py, fhir/sync.py): not lab concepts.
HKL_FALLBACK_PREFIX = 'hkl:fallback-'
# The OHDSI range for local concepts; Athena never allocates there.
LOCAL_CONCEPT_MIN_ID = 2_000_000_000
# A LOINC code filed under HK-Labs (``hkl:85319-5``): matched to the standard
# concept with that code when one is loaded.
_LOINC_CODE_TAIL = re.compile(r'^hkl:(\d{1,7}-\d)$')

# hkl: concept_code -> LOINC code of the standard concept it duplicates.
# Every target is a serum/plasma test (or a calculation or a presence test with
# no specimen in its name) that was checked to exist, active and standard, on
# staging. The urine/24 h panels and the genuinely local concepts are not here:
# their references are cleared, not guessed at.
RETIRED_HKL_DUPLICATES: dict[str, str] = {
    'hkl:psa': '2857-1',
    'hkl:testosterone': '2986-8',
    'hkl:testosterone-total': '2986-8',
    'hkl:testosterone-free-calculation': '103227-5',
    'hkl:progesterone': '2839-9',
    'hkl:prolactin': '2842-3',
    'hkl:follicle-stimulating-hormone': '15067-2',
    'hkl:luteinizing-hormone': '10501-5',
    'hkl:shbg': '13967-5',
    'hkl:igf-1': '2484-4',
    'hkl:dhea-s': '2191-5',
    'hkl:dheas': '2191-5',
    'hkl:estradiol-e2': '2243-4',
    'hkl:estriol-e3': '2251-7',
    'hkl:estrone-e1': '2258-2',
    'hkl:cortisol': '2143-6',
    'hkl:tpoab': '8099-4',
    'hkl:sars-cov-2-naa': '94309-2',
}

# --- how a reference is handled ---------------------------------------------

# (model label, column): rows that exist only for the concept. They go with it.
DEPENDENT_COLUMNS: frozenset[tuple[str, str]] = frozenset({
    ('omop_core.ConceptAncestor', 'ancestor_concept'),
    ('omop_core.ConceptAncestor', 'descendant_concept'),
    ('omop_core.ConceptRelationship', 'concept_1'),
    ('omop_core.ConceptRelationship', 'concept_2'),
    ('omop_core.ConceptSynonym', 'concept'),
    ('omop_core.SuggestSynonymTerm', 'concept'),
    ('omop_core.ConceptEmbedding', 'concept'),
    ('omop_core.ConceptMappingPriority', 'concept'),
    ('omop_core.ConceptMappingDone', 'concept'),
    ('omop_core.ConceptMappingLock', 'concept'),
    ('omop_core.ReverseSuggestDecline', 'concept'),
    # A candidate destination for a mapping: with the concept gone it is no candidate.
    ('omop_core.MappingDestinationCandidate', 'target_concept'),
})

# One row per concept: it moves to the standard concept when that has none.
MOVE_OR_DROP_COLUMNS: frozenset[tuple[str, str]] = frozenset({
    ('omop_core.ConceptMappingNote', 'concept'),
})

# A reference to a concept with no standard equivalent is cleared. These two are
# mappings: they lose the destination and go back to the curators' queue, and an
# approved one is demoted to proposed with a note.
CLEAR_AS_MAPPING: frozenset[tuple[str, str]] = frozenset({
    ('omop_core.SourceCodeConceptMapping', 'target_concept'),
    ('omop_core.FieldConceptMapping', 'concept'),
})
# A field answer mapped to the concept has no equivalent state to be cleared to
# (outcome 'mapped' needs its target): it stops the concept being removed.
CLEAR_BLOCKED: frozenset[tuple[str, str]] = frozenset({
    ('omop_core.FieldValueConceptMapping', 'target_concept'),
    ('omop_core.FieldValueConceptMapping', 'question_concept'),
})

# A foreign key's text twins, written beside it and updated with it.
# (model label, fk name) -> (vocabulary column, code column)
TWIN_COLUMNS: dict[tuple[str, str], tuple[str, str]] = {
    ('omop_core.FieldConceptMapping', 'concept'): ('vocabulary_id', 'concept_code'),
    ('omop_core.FieldValueConceptMapping', 'target_concept'): ('vocabulary_id', 'concept_code'),
    ('omop_core.FieldValueConceptMapping', 'question_concept'): (
        'question_vocabulary_id', 'question_concept_code'),
    ('omop_core.SourceCodeConceptMapping', 'target_concept'): ('destination_vocabulary_id', ''),
    ('omop_core.SourceToConceptMap', 'target_concept'): ('target_vocabulary_id', ''),
}

# A concept id held as a plain integer: repointed to the standard concept.
ID_COLUMNS: tuple[tuple[str, str], ...] = (
    ('omop_core.MappingSuggestionReview', 'suggested_target_concept_id'),
    ('omop_core.RelevanceSignal', 'concept_id'),
)

# Concept ids held in a JSON list of ids to move rows off: the id is removed,
# since the rows it described are moved by this cleanup itself.
JSON_ID_LISTS: tuple[tuple[str, str], ...] = (
    ('omop_core.SourceCodeConceptMapping', 'pending_repoint_concept_ids'),
    ('omop_core.SourceCodeConceptMapping', 'deferred_repoint_concept_ids'),
)

# A concept named by (vocabulary, code) text: a reference this cleanup does not
# rewrite, so one that exists stops the concept being deleted.
CODE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ('omop_core.FieldChoiceCode', 'vocabulary_id', 'code'),
)

# Where the concept is only recorded, never read to decide anything: left as
# they are and reported. (A vote names the destination it was cast on and is
# marked stale once the mapping moves; the revision log is an audit trail.)
INFORMATIONAL_ID_COLUMNS: tuple[tuple[str, str], ...] = (
    ('omop_core.MappingVote', 'concept_id'),
    ('omop_core.FieldValueConceptMappingRevision', 'concept_id'),
    ('omop_core.FieldValueConceptMappingRevision', 'question_concept_id'),
)
# A source code keyed on the HK-Labs vocabulary is the source's identity, not a
# reference to the concept: deleting the concept leaves it, and it resolves
# afresh. Reported only.
INFORMATIONAL_CODE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ('omop_core.SourceCodeConceptMapping', 'source_vocabulary_id', 'source_code'),
)

# --- what is not serum or plasma ---------------------------------------------

# A referencing row that says it is something else is left alone: repointing
# its concept to a serum code would be wrong. Text of the row's own columns is
# read (a measurement's unit and source value, a mapping's description, notes,
# source metadata and LOINC axes).
_NON_SERUM = re.compile(
    r'\b(?:urine|urinary|saliva\w*|stool|fecal|faecal|csf|cerebrospinal|breast\s*milk)\b'
    r'|\b24[\s-]*h(?:(?:ou)?rs?)?\b'
    r'|/\s*24\s*h'
    r'|/\s*(?:g|mg)\s*creat'
    # Mass per mass (ng/mg): a creatinine-normalised or tissue figure, never a serum concentration.
    r'|\b(?:pg|ng|ug|\u00b5g|\u03bcg|mcg|mg)\s*/\s*m?g\b'
    r'|\bper\s+day\b|/\s*day\b'
    r'|\b(?:random|spot)\s+ur',
    re.IGNORECASE,
)
# LOINC's system axis for urine; case matters, or any "ur" would match.
_URINE_AXIS = re.compile(r'\bUr\b')
_SKIPPED_TEXT_SUFFIXES = ('vocabulary_id', 'concept_code')


def non_serum_reason(text: str) -> str:
    """What in *text* says the specimen is not serum or plasma, or ''."""
    match = _NON_SERUM.search(text) or _URINE_AXIS.search(text)
    return match.group(0) if match else ''


# --- reading references --------------------------------------------------------

def _label(model) -> str:
    return model._meta.label


def concept_fk_columns(registry, using: str = 'default') -> list[tuple[object, object]]:
    """Every foreign key to Concept on every model whose table exists: ``[(model, field)]``.

    A model with no table here is skipped: ``ConceptEmbedding`` has none on a
    server without pgvector.
    """
    from django.db import connections
    tables = set(connections[using].introspection.table_names())
    found = []
    for model in registry.get_models():
        if model._meta.proxy or not model._meta.managed or model._meta.db_table not in tables:
            continue
        for fld in model._meta.concrete_fields:
            if fld.is_relation and fld.related_model is not None \
                    and _label(fld.related_model) == 'omop_core.Concept':
                found.append((model, fld))
    return sorted(found, key=lambda pair: (_label(pair[0]), pair[1].name))


def _class_of(cache: dict[str, str], label: str) -> str:
    if label not in cache:
        cache[label] = _instance_class(label)
    return cache[label]


def _instance_class(label: str) -> str:
    """``reference``, ``patient``, ``system`` or ``unclassified``: instance_data.py's word."""
    try:
        from omop_core.services import instance_data
    except Exception:  # pragma: no cover - a migration run before the app is importable
        return 'unclassified'
    if label in instance_data.PATIENT:
        return 'patient'
    if label in instance_data.reference():
        return 'reference'
    if label in instance_data.SYSTEM:
        return 'system'
    return 'unclassified'


@dataclass
class Reference:
    """The rows of one column that point at a retired concept."""
    model_label: str
    column: str
    count: int
    action: str            # repoint | delete | move_or_drop | remove_from_list | blocks | info
    instance_class: str
    kind: str              # fk | id | json | code

    def describe(self) -> str:
        return f'{self.model_label}.{self.column}={self.count} [{self.instance_class}; {self.action}]'


@dataclass
class ConceptPlan:
    """What the cleanup does with one retired concept present on this instance."""
    hkl_code: str
    concept_id: int
    concept_name: str
    target_code: str
    target_id: int | None
    target_name: str
    target_state: str      # ok | missing | inactive | not_standard
    action: str = 'clear'  # repoint | clear
    kind: str = ''         # table (a listed duplicate) | same_code | '' (no standard equivalent)
    zero_exists: bool = True   # concept 0 is here, so a source concept can be cleared to it
    demotions: list[str] = field(default_factory=list)   # approved mappings that lose their target
    references: list[Reference] = field(default_factory=list)
    non_serum: list[str] = field(default_factory=list)   # 'Model pk=1: urine', at most 20
    non_serum_total: int = 0
    skip: dict = field(default_factory=dict)   # (model label, column) -> pks left alone
    blockers: list[str] = field(default_factory=list)    # references the cleanup does not rewrite
    outcome: str = ''      # delete | skip_target | skip_unhandled | failed
    detail: str = ''

    @property
    def deletable(self) -> bool:
        return self.outcome == 'delete'


def target_state(concept) -> str:
    """Whether a standard LOINC concept can take the references over."""
    if concept is None:
        return 'missing'
    if concept.invalid_reason not in (None, ''):
        return 'inactive'
    if concept.standard_concept != 'S':
        return 'not_standard'
    return 'ok'


def _text_of_row(pk_name: str, values: dict) -> str:
    parts = []
    for name, value in values.items():
        if name == pk_name or value in (None, ''):
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, default=str))
    return ' | '.join(parts)


def _text_columns(model) -> list[str]:
    names = []
    for fld in model._meta.concrete_fields:
        if fld.primary_key or fld.is_relation:
            continue
        if fld.get_internal_type() not in ('CharField', 'TextField', 'JSONField'):
            continue
        if fld.name.endswith(_SKIPPED_TEXT_SUFFIXES):
            continue
        names.append(fld.name)
    return names


def standard_target_code(hkl_code: str) -> tuple[str, str]:
    """``(LOINC code, kind)`` an hkl: concept is repointed to, or ``('', '')`` to clear it.

    ``table`` is one of :data:`RETIRED_HKL_DUPLICATES`; ``same_code`` is a LOINC
    code filed under HK-Labs (``hkl:85319-5``), which means the standard concept
    with that code once it is loaded.
    """
    if hkl_code in RETIRED_HKL_DUPLICATES:
        return RETIRED_HKL_DUPLICATES[hkl_code], 'table'
    match = _LOINC_CODE_TAIL.match(hkl_code)
    return (match.group(1), 'same_code') if match else ('', '')


def build_plan(registry, using: str = 'default') -> list[ConceptPlan]:
    """One :class:`ConceptPlan` per auto-created HK-Labs concept present on this database."""
    Concept = registry.get_model('omop_core', 'Concept')
    present = {
        c.concept_code: c for c in Concept.objects.using(using).filter(
            vocabulary_id=HK_LABS_VOCABULARY, concept_code__startswith=HKL_PREFIX,
            concept_id__gte=LOCAL_CONCEPT_MIN_ID,
        ).exclude(concept_code__startswith=HKL_FALLBACK_PREFIX)
    }
    if not present:
        return []
    wanted = {standard_target_code(code)[0] for code in present} - {''}
    targets = {
        c.concept_code: c for c in Concept.objects.using(using).filter(
            vocabulary_id=STANDARD_VOCABULARY, concept_code__in=wanted)
    }
    zero_exists = Concept.objects.using(using).filter(concept_id=0).exists()
    plans = {}
    for hkl_code in sorted(present):
        concept = present[hkl_code]
        target_code, kind = standard_target_code(hkl_code)
        target = targets.get(target_code)
        state = target_state(target) if target_code else ''
        plan = ConceptPlan(
            hkl_code=hkl_code, concept_id=concept.concept_id, concept_name=concept.concept_name,
            target_code=target_code, target_id=target.concept_id if target else None,
            target_name=target.concept_name if target else '', target_state=state,
            kind=kind, zero_exists=zero_exists,
        )
        # A listed duplicate whose standard concept is unusable here is kept, and
        # reported. A LOINC code with no usable standard concept is cleared.
        plan.action = 'repoint' if (kind and state == 'ok') or kind == 'table' else 'clear'
        plans[concept.concept_id] = plan
    _collect_references(registry, using, plans)
    for plan in plans.values():
        _decide(plan)
    return list(plans.values())


def _grouped_counts(qs, column: str) -> dict[int, int]:
    """References per concept in one column: one grouped query for all concepts."""
    from django.db.models import Count
    return {row[column]: row['n'] for row in qs.values(column).annotate(n=Count('pk'))}


def _collect_references(registry, using, plans: dict[int, ConceptPlan]) -> None:
    """Fill each plan's references, one query per column for every concept at once."""
    ids = list(plans)
    classes: dict[str, str] = {}   # instance_data's class per model, read once
    for model, fld in concept_fk_columns(registry, using):
        label = _label(model)
        qs = model._default_manager.using(using).filter(**{f'{fld.attname}__in': ids})
        counts = _grouped_counts(qs, fld.attname)
        for cid, count in counts.items():
            key = (label, fld.name)
            plan = plans[cid]
            instance_class = _class_of(classes, label)
            if key in DEPENDENT_COLUMNS:
                action = 'delete'
            elif key in MOVE_OR_DROP_COLUMNS:
                action = 'move_or_drop' if plan.action == 'repoint' else 'delete'
            elif plan.action == 'repoint':
                action = 'repoint'
            elif key in CLEAR_BLOCKED:
                action = 'blocks'
                plan.blockers.append(f'{label}.{fld.name}={count} cannot be cleared: a curator must map it')
            elif key in CLEAR_AS_MAPPING:
                action = 'clear_mapping'
                _note_demotions(plan, qs.filter(**{fld.attname: cid}), label)
            elif _clears_to_zero(fld) and not plan.zero_exists:
                action = 'blocks'
                plan.blockers.append(f'{label}.{fld.name}={count} needs concept 0, which is not here')
            elif fld.null or instance_class == 'patient':
                action = 'clear'
            else:
                action = 'blocks'
                plan.blockers.append(f'{label}.{fld.name}={count} cannot be cleared')
            plan.references.append(Reference(label, fld.name, count, action, instance_class, 'fk'))
            if action == 'repoint' and plan.kind == 'table':
                _check_specimen(plan, model, key, qs.filter(**{fld.attname: cid}))
    for label, column in ID_COLUMNS + INFORMATIONAL_ID_COLUMNS:
        model = registry.get_model(*label.split('.'))
        qs = model._default_manager.using(using).filter(**{f'{column}__in': ids})
        informational = (label, column) in INFORMATIONAL_ID_COLUMNS
        for cid, count in _grouped_counts(qs, column).items():
            plan = plans[cid]
            # A snapshot id is kept as it is when the concept is cleared, not repointed.
            keep = informational or plan.action != 'repoint'
            plan.references.append(Reference(
                label, column, count, 'info' if keep else 'repoint',
                _class_of(classes, label), 'id'))
            if not keep and plan.kind == 'table':
                _check_specimen(plan, model, (label, column), qs.filter(**{column: cid}))
    for label, column in JSON_ID_LISTS:
        model = registry.get_model(*label.split('.'))
        for cid, plan in plans.items():
            count = model._default_manager.using(using).filter(**{f'{column}__contains': [cid]}).count()
            if count:
                plan.references.append(Reference(
                    label, column, count, 'remove_from_list', _instance_class(label), 'json'))
    by_code = {plan.hkl_code: plan for plan in plans.values()}
    for label, vocab_col, code_col in CODE_COLUMNS + INFORMATIONAL_CODE_COLUMNS:
        model = registry.get_model(*label.split('.'))
        qs = model._default_manager.using(using).filter(
            **{vocab_col: HK_LABS_VOCABULARY, f'{code_col}__in': list(by_code)})
        informational = (label, vocab_col, code_col) in INFORMATIONAL_CODE_COLUMNS
        for code, count in _grouped_counts(qs, code_col).items():
            plan = by_code[code]
            plan.references.append(Reference(
                label, code_col, count, 'info' if informational else 'blocks',
                _instance_class(label), 'code'))
            if not informational:
                plan.blockers.append(f'{label}.{code_col}={count} names the code and is not rewritten')


def _clears_to_zero(fld) -> bool:
    """Whether a reference in this column is cleared to concept 0 rather than NULL."""
    return (not fld.null) or fld.attname.endswith('source_concept_id')


def _note_demotions(plan, qs, label: str) -> None:
    """Name the approved mappings that lose their destination."""
    if label == 'omop_core.SourceCodeConceptMapping':
        rows = qs.filter(status='approved').values_list('pk', 'source_vocabulary_id', 'source_code')
        plan.demotions += [f'SourceCodeConceptMapping {pk}: {vocab} {code}' for pk, vocab, code in rows]
    else:
        rows = qs.filter(status='approved').values_list('pk', 'field_name')
        plan.demotions += [f'FieldConceptMapping {pk}: {name}' for pk, name in rows]


def _check_specimen(plan, model, key, qs, limit: int = 20) -> None:
    """Note each referencing row whose own text says it is not serum or plasma."""
    pk_name = model._meta.pk.name
    names = _text_columns(model)
    if not names:
        return
    for row in qs.values(pk_name, *names).iterator(chunk_size=2000):
        reason = non_serum_reason(_text_of_row(pk_name, row))
        if reason:
            plan.non_serum_total += 1
            plan.skip.setdefault(key, []).append(row[pk_name])
            if len(plan.non_serum) < limit:
                plan.non_serum.append(f'{_label(model)} {pk_name}={row[pk_name]}: {reason!r}')


def _decide(plan: ConceptPlan) -> None:
    if plan.kind == 'table' and plan.target_state != 'ok':
        plan.outcome = 'skip_target'
        plan.detail = (f'standard LOINC {plan.target_code} is {plan.target_state.replace("_", " ")} '
                       'on this instance')
    elif plan.blockers:
        plan.outcome = 'skip_unhandled'
        plan.detail = '; '.join(plan.blockers)
    elif plan.non_serum_total:
        # Not serum or plasma, so not what the standard concept means: those rows
        # are cleared, not repointed, and the concept goes all the same.
        plan.outcome = 'delete'
        plan.detail = (f'{plan.non_serum_total} referencing row(s) look like urine/24 h/saliva/'
                       'per-mass and are cleared, not repointed: ' + '; '.join(plan.non_serum[:5]))
    else:
        plan.outcome = 'delete'


# --- applying ------------------------------------------------------------------

@dataclass
class Summary:
    deleted: int = 0          # concepts removed
    rows_cleared: int = 0
    demoted: int = 0          # approved mappings sent back to proposed
    skipped: int = 0
    rows_repointed: int = 0
    rows_deleted: int = 0
    notes_moved: int = 0


def apply_plan(registry, plans: list[ConceptPlan], using: str = 'default') -> Summary:
    """Repoint or clear every reference of each concept, then delete the ones nothing is left on.

    A concept is handled in a savepoint of its own: whatever goes wrong with one
    leaves it, and its references, exactly as they were, and the rest go on.
    A listed duplicate's non-serum references are cleared, not repointed. Every concept kept is logged at WARNING with the
    reason, and so is every approved mapping that loses its destination.
    """
    summary = Summary()
    priorities_removed = False
    for plan in plans:
        if plan.outcome != 'delete':
            summary.skipped += 1
            logger.warning('HK-Labs cleanup: kept %s (%s): %s', plan.hkl_code, plan.outcome, plan.detail)
            continue
        try:
            with transaction.atomic(using=using):
                stats = _apply_one(registry, using, plan)
                # Deferred foreign keys are checked at COMMIT, past every handler
                # here: check now, so a reference this plan did not see undoes
                # only this concept.
                with connections[using].cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
                    cursor.execute('SET CONSTRAINTS ALL DEFERRED')
        except (DatabaseError, RuntimeError) as exc:
            plan.outcome, plan.detail = 'failed', f'{type(exc).__name__}: {exc}'.splitlines()[0]
            summary.skipped += 1
            logger.warning('HK-Labs cleanup: kept %s (failed, rolled back): %s', plan.hkl_code, plan.detail)
            continue
        summary.rows_repointed += stats['repointed']
        summary.rows_cleared += stats['cleared']
        summary.rows_deleted += stats['deleted']
        summary.notes_moved += stats['notes_moved']
        summary.demoted += stats['demoted']
        priorities_removed = priorities_removed or stats['priority_deleted']
        for line in plan.demotions:
            logger.warning('HK-Labs cleanup: approved mapping demoted to proposed, destination %s '
                           'removed: %s', plan.hkl_code, line)
        summary.deleted += 1
        if plan.action == 'repoint':
            logger.info('HK-Labs cleanup: removed %s -> LOINC %s (%d row(s) repointed, %d cleared)',
                        plan.hkl_code, plan.target_code, stats['repointed'], stats['cleared'])
            if plan.non_serum_total:
                logger.warning('HK-Labs cleanup: %s: %s', plan.hkl_code, plan.detail)
        else:
            logger.warning('HK-Labs cleanup: removed %s with no standard equivalent: %d '
                           'reference(s) cleared', plan.hkl_code, stats['cleared'])
    if priorities_removed:
        try:
            with transaction.atomic(using=using):
                _renumber_priorities(registry, using)
        except DatabaseError:
            logger.warning('HK-Labs cleanup: could not renumber curator priority positions', exc_info=True)
    return summary


def _timestamp_update(model, update: dict) -> dict:
    """Add ``updated_at`` where the model has one: ``update()`` skips ``auto_now``.

    The code-mapping API's ``updated_since`` delta reads it, so a change it did
    not stamp would never reach the ETL.
    """
    if _has_field(model, 'updated_at'):
        update = {**update, 'updated_at': timezone.now()}
    return update


def _demote(model, qs, plan, label: str) -> int:
    """Clear a mapping's destination and demote an approved one to proposed, with a note."""
    note = (f'HK-Labs concept {plan.hkl_code} "{plan.concept_name}" was removed (#2922). '
            'The destination was cleared and the code is back in the queue for a standard '
            'concept or a curator mint.')
    cleared = {
        'omop_core.SourceCodeConceptMapping': {
            'target_concept': None, 'destination_vocabulary_id': '', 'destination_unit_concept': None,
            'reviewer': None, 'reviewed_at': None, 'proposed_by': None, 'origin_system': '',
            'suggested_action': '', 'suggestion_confidence': None, 'decision_confidence': None,
            'locked_by': None, 'locked_at': None,
        },
        'omop_core.FieldConceptMapping': {
            'concept': None, 'vocabulary_id': '', 'concept_code': '', 'reviewer': None,
            'reviewed_at': None,
        },
    }[label]
    demoted = 0
    for row in list(qs):
        values = {name: value for name, value in cleared.items() if _has_field(model, name)}
        if getattr(row, 'status', '') == 'approved':
            demoted += 1
            values['status'] = 'proposed'
            values['notes'] = ((row.notes + '\n') if row.notes else '') + note
        model._default_manager.using(qs.db).filter(pk=row.pk).update(**_timestamp_update(model, values))
    return demoted


def _clear_values(fld, key) -> dict:
    """What a cleared reference becomes: concept 0 or NULL, and its text twins blank."""
    values = {fld.attname: 0 if _clears_to_zero(fld) else None}
    twin = TWIN_COLUMNS.get(key)
    if twin:
        values.update({col: '' for col in twin if col})
    return values


def _repoint_rows(model, fld, key, qs, plan, stats, using) -> None:
    """Point the rows of *qs* at the standard concept, with their text twins."""
    update = {fld.attname: plan.target_id}
    twin = TWIN_COLUMNS.get(key)
    if twin:
        update[twin[0]] = STANDARD_VOCABULARY
        if twin[1]:
            update[twin[1]] = plan.target_code
    stats['repointed'] += qs.update(**_timestamp_update(model, update))


def _apply_one(registry, using, plan) -> dict:
    cid, tid = plan.concept_id, plan.target_id
    Concept = registry.get_model('omop_core', 'Concept')
    stats = {'repointed': 0, 'cleared': 0, 'deleted': 0, 'notes_moved': 0, 'demoted': 0,
             'priority_deleted': False}
    repoint = plan.action == 'repoint'

    held = {(r.model_label, r.column) for r in plan.references if r.kind == 'fk'}
    for model, fld in concept_fk_columns(registry, using):
        key = (_label(model), fld.name)
        if key not in held:
            continue
        qs = model._default_manager.using(using).filter(**{fld.attname: cid})
        if key in DEPENDENT_COLUMNS:
            n = qs.count()
            if n:
                if key[0] == 'omop_core.ConceptMappingPriority':
                    stats['priority_deleted'] = True
                logger.warning('HK-Labs cleanup: %s: deleting %d %s row(s)', plan.hkl_code, n, key[0])
                qs._raw_delete(using)
                stats['deleted'] += n
        elif key in MOVE_OR_DROP_COLUMNS:
            for row in list(qs):
                taken = tid is not None and model._default_manager.using(using).filter(
                    **{fld.attname: tid}).exists()
                if tid is None or taken:
                    logger.warning('HK-Labs cleanup: %s: dropped a %s', plan.hkl_code, key[0])
                    row.delete()
                    stats['deleted'] += 1
                else:
                    setattr(row, fld.attname, tid)
                    row.save(update_fields=[fld.attname])
                    stats['notes_moved'] += 1
        elif key in CLEAR_AS_MAPPING and (not repoint or plan.skip.get(key)):
            # No standard equivalent, or rows that are not what the standard concept
            # means: those lose the destination; the rest are repointed below.
            demote_qs = qs if not repoint else qs.filter(pk__in=plan.skip[key])
            n = demote_qs.count()
            stats['demoted'] += _demote(model, demote_qs, plan, key[0])
            stats['cleared'] += n
            if repoint:
                _repoint_rows(model, fld, key, qs.exclude(pk__in=plan.skip[key]), plan, stats, using)
        else:
            if not qs.exists():
                continue
            if _has_field(model, 'person'):
                # Re-derived by `backfill_patient_records`, which selects on this.
                # Before the update: afterwards the rows no longer match qs.
                registry.get_model('omop_core', 'PatientRecord')._default_manager.using(using).filter(
                    person_id__in=qs.values('person_id')).update(derivation_version=0)
            if repoint:
                skipped = plan.skip.get(key, ())
                if skipped:
                    # Not serum or plasma: cleared, never repointed to a serum code.
                    stats['cleared'] += qs.filter(pk__in=skipped).update(**_timestamp_update(
                        model, _clear_values(fld, key)))
                    qs = qs.exclude(pk__in=skipped)
                _repoint_rows(model, fld, key, qs, plan, stats, using)
            else:
                stats['cleared'] += qs.update(**_timestamp_update(model, _clear_values(fld, key)))

    if repoint:
        for label, column in ID_COLUMNS:
            model = registry.get_model(*label.split('.'))
            qs = model._default_manager.using(using).filter(**{column: cid})
            # A snapshot id on a row that is not serum is left as it is.
            qs = qs.exclude(pk__in=plan.skip.get((label, column), ()))
            stats['repointed'] += qs.update(**_timestamp_update(model, {column: tid}))
    for label, column in JSON_ID_LISTS:
        model = registry.get_model(*label.split('.'))
        manager = model._default_manager.using(using)
        for row in manager.filter(**{f'{column}__contains': [cid]}).only(column):
            kept = [i for i in getattr(row, column) if i != cid]
            manager.filter(pk=row.pk).update(**{column: kept})

    # Only the columns that held a reference: discovery is complete, so these
    # are the ones that could still hold one.
    remaining = [
        f'{_label(m)}.{f.name}' for m, f in concept_fk_columns(registry, using)
        if (_label(m), f.name) in held
        and m._default_manager.using(using).filter(**{f.attname: cid}).exists()
    ]
    if remaining:
        raise RuntimeError(f'still referenced by {", ".join(remaining)}')
    Concept._default_manager.using(using).filter(concept_id=cid).delete()
    return stats


def _has_field(model, name: str) -> bool:
    return any(f.name == name for f in model._meta.concrete_fields)


def _renumber_priorities(registry, using) -> None:
    """Keep curator priority positions dense (1..N) after rows were removed."""
    Priority = registry.get_model('omop_core', 'ConceptMappingPriority')
    rows = list(Priority._default_manager.using(using).order_by('position', 'concept_id'))
    for index, row in enumerate(rows, start=1):
        if row.position != index:
            row.position = index
    Priority._default_manager.using(using).bulk_update(rows, ['position'])


# --- preventing a new duplicate -------------------------------------------------

def normalize_name(name: str | None) -> str:
    """Case- and spacing-insensitive form of a concept name."""
    return re.sub(r'\s+', ' ', (name or '').strip()).casefold()


def _code_tail(code: str | None) -> str:
    """A code without an ``hkl:`` style prefix: ``hkl:2857-1`` -> ``2857-1``."""
    code = (code or '').strip()
    return code.split(':', 1)[1] if re.match(r'^hk[a-z]:', code, re.IGNORECASE) else code


def active_standard(queryset):
    """Active, standard concepts of a queryset."""
    return queryset.filter(standard_concept='S', invalid_reason__isnull=True)


def find_standard_duplicate(name: str | None = None, code: str | None = None,
                            domain_id: str = 'Measurement'):
    """An active standard LOINC concept a new HK-Labs concept would duplicate, or None.

    Duplicate means the same LOINC code once an ``hkl:`` prefix is taken off, or
    the same name (case and spacing aside) in the domain. A name is matched on
    the standard concept's own name: no synonym or fuzzy match, because a near
    match is a curator's call and a wrong merge is worse than a duplicate.
    """
    from omop_core.models import Concept

    base = active_standard(Concept.objects.filter(vocabulary_id=STANDARD_VOCABULARY))
    tail = _code_tail(code)
    if tail:
        hit = base.filter(concept_code=tail).first()
        if hit is not None:
            return hit
    wanted = normalize_name(name)
    if len(wanted) >= 3:
        spaced = re.sub(r'\s+', ' ', (name or '').strip())
        for hit in base.filter(domain_id=domain_id, concept_name__iexact=spaced).order_by('concept_id')[:20]:
            if normalize_name(hit.concept_name) == wanted:
                return hit
    return None


def retired_standard_concept(concept_code: str | None):
    """The standard concept a retired ``hkl:`` code stands for, or None.

    A code that was retired is never minted again: a lab named the same way
    resolves to this concept instead. None when the code was not retired or the
    standard concept is not loaded or not active here.
    """
    target_code = RETIRED_HKL_DUPLICATES.get((concept_code or '').strip().lower())
    if not target_code:
        return None
    from omop_core.models import Concept
    return active_standard(Concept.objects.filter(
        vocabulary_id=STANDARD_VOCABULARY, concept_code=target_code)).first()


def alias_for(vocabulary_id: str | None, concept_code: str | None) -> tuple[str, str] | None:
    """``(vocabulary, code)`` of the standard concept a removed duplicate stood for, or None."""
    if vocabulary_id == HK_LABS_VOCABULARY and (concept_code or '') in RETIRED_HKL_DUPLICATES:
        return STANDARD_VOCABULARY, RETIRED_HKL_DUPLICATES[concept_code]
    return None


def is_removed_hkl_concept(vocabulary_id: str | None, concept_code: str | None) -> bool:
    """Whether (vocabulary, code) names an auto-created HK-Labs concept this cleanup removes.

    ``copy_reference_data`` skips these, so an older instance cannot bring one back.
    """
    code = (concept_code or '').strip().lower()
    return (vocabulary_id == HK_LABS_VOCABULARY and code.startswith(HKL_PREFIX)
            and not code.startswith(HKL_FALLBACK_PREFIX))
