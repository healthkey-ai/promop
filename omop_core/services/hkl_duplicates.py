"""Retire the HK-Labs concepts that duplicate a standard serum/plasma LOINC test.

``hkl:<slug>`` concepts are local, non-standard concepts HealthKey minted for a
lab name that had no LOINC match (see docs/vocabularies.md). Eighteen of them
turned out to name a test LOINC does have, so they sat beside the standard
concept in concept search (``hkl:psa`` ahead of LOINC 2857-1) and invited
mappings to the wrong one. This module holds

* the table of those eighteen (:data:`RETIRED_HKL_DUPLICATES`),
* the cleanup itself (:func:`build_plan`, :func:`apply_plan`), shared by the
  read-only ``audit_hkl_duplicates`` command and migration 0380 so the audit
  cannot disagree with what the migration does, and
* the guard that stops a new duplicate being minted
  (:func:`find_standard_duplicate`, :func:`retired_standard_concept`).

The cleanup functions take a model registry (``django.apps.apps`` or a
migration's ``apps``) and import no model themselves: a migration must run
against the schema of its own point in history. They use no other promop code.
A later change to this module does not re-run the migration on a database that
already applied it, and on a fresh database there is nothing to retire.

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

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)

HK_LABS_VOCABULARY = 'HK-Labs'
STANDARD_VOCABULARY = 'LOINC'

# hkl: concept_code -> LOINC code of the standard concept it duplicates.
# Every target is a serum/plasma test (or a calculation or a presence test with
# no specimen in its name) that was checked to exist, active and standard, on
# staging. The urine/24 h panels, the two codes that are LOINC codes themselves
# and the genuinely local ones were left out on purpose.
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
})

# One row per concept: it moves to the standard concept when that has none.
MOVE_OR_DROP_COLUMNS: frozenset[tuple[str, str]] = frozenset({
    ('omop_core.ConceptMappingNote', 'concept'),
})

# A foreign key's text twins, written beside it and updated with it.
# (model label, fk name) -> (vocabulary column, code column)
TWIN_COLUMNS: dict[tuple[str, str], tuple[str, str]] = {
    ('omop_core.FieldConceptMapping', 'concept'): ('vocabulary_id', 'concept_code'),
    ('omop_core.FieldValueConceptMapping', 'target_concept'): ('vocabulary_id', 'concept_code'),
    ('omop_core.FieldValueConceptMapping', 'question_concept'): (
        'question_vocabulary_id', 'question_concept_code'),
    ('omop_core.SourceCodeConceptMapping', 'target_concept'): ('destination_vocabulary_id', ''),
    ('omop_core.MappingDestinationCandidate', 'target_concept'): (
        'target_vocabulary_id', 'target_concept_code'),
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
    ('omop_core.PatientSourceCode', 'source_vocabulary_id', 'source_value'),
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


def concept_fk_columns(registry) -> list[tuple[object, object]]:
    """Every foreign key to Concept on every model: ``[(model, field)]``."""
    found = []
    for model in registry.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for fld in model._meta.concrete_fields:
            if fld.is_relation and fld.related_model is not None \
                    and _label(fld.related_model) == 'omop_core.Concept':
                found.append((model, fld))
    return sorted(found, key=lambda pair: (_label(pair[0]), pair[1].name))


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
    references: list[Reference] = field(default_factory=list)
    non_serum: list[str] = field(default_factory=list)   # 'Model pk=1: urine', at most 20
    non_serum_total: int = 0
    skip: dict = field(default_factory=dict)   # (model label, column) -> pks left alone
    blockers: list[str] = field(default_factory=list)    # references the cleanup does not rewrite
    outcome: str = ''      # delete | partial_non_serum | skip_target | skip_unhandled | failed
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


def build_plan(registry, using: str = 'default') -> list[ConceptPlan]:
    """One :class:`ConceptPlan` per retired concept present on this database."""
    Concept = registry.get_model('omop_core', 'Concept')
    present = {
        c.concept_code: c for c in Concept.objects.using(using).filter(
            vocabulary_id=HK_LABS_VOCABULARY, concept_code__in=list(RETIRED_HKL_DUPLICATES),
        )
    }
    if not present:
        return []
    targets = {
        c.concept_code: c for c in Concept.objects.using(using).filter(
            vocabulary_id=STANDARD_VOCABULARY,
            concept_code__in=set(RETIRED_HKL_DUPLICATES.values()),
        )
    }
    plans = {}
    for hkl_code in sorted(present):
        concept = present[hkl_code]
        target_code = RETIRED_HKL_DUPLICATES[hkl_code]
        target = targets.get(target_code)
        plans[concept.concept_id] = ConceptPlan(
            hkl_code=hkl_code, concept_id=concept.concept_id, concept_name=concept.concept_name,
            target_code=target_code, target_id=target.concept_id if target else None,
            target_name=target.concept_name if target else '', target_state=target_state(target),
        )
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
    for model, fld in concept_fk_columns(registry):
        label = _label(model)
        qs = model._default_manager.using(using).filter(**{f'{fld.attname}__in': ids})
        counts = _grouped_counts(qs, fld.attname)
        for cid, count in counts.items():
            key = (label, fld.name)
            action = ('delete' if key in DEPENDENT_COLUMNS
                      else 'move_or_drop' if key in MOVE_OR_DROP_COLUMNS else 'repoint')
            plan = plans[cid]
            plan.references.append(Reference(label, fld.name, count, action, _instance_class(label), 'fk'))
            if action == 'repoint':
                _check_specimen(plan, model, key, qs.filter(**{fld.attname: cid}))
    for label, column in ID_COLUMNS + INFORMATIONAL_ID_COLUMNS:
        model = registry.get_model(*label.split('.'))
        qs = model._default_manager.using(using).filter(**{f'{column}__in': ids})
        informational = (label, column) in INFORMATIONAL_ID_COLUMNS
        for cid, count in _grouped_counts(qs, column).items():
            plan = plans[cid]
            plan.references.append(Reference(
                label, column, count, 'info' if informational else 'repoint',
                _instance_class(label), 'id'))
            if not informational:
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
    if plan.target_state != 'ok':
        plan.outcome = 'skip_target'
        plan.detail = (f'standard LOINC {plan.target_code} is {plan.target_state.replace("_", " ")} '
                       'on this instance')
    elif plan.blockers:
        plan.outcome = 'skip_unhandled'
        plan.detail = '; '.join(plan.blockers)
    elif plan.non_serum_total:
        # The rows that are serum or plasma move; the others stay on the local
        # concept, which therefore stays.
        plan.outcome = 'partial_non_serum'
        plan.detail = (f'{plan.non_serum_total} referencing row(s) look like urine/24 h/saliva/'
                       'per-mass and are left alone: ' + '; '.join(plan.non_serum[:5]))
    else:
        plan.outcome = 'delete'


# --- applying ------------------------------------------------------------------

@dataclass
class Summary:
    deleted: int = 0
    partial: int = 0
    skipped: int = 0
    rows_repointed: int = 0
    rows_deleted: int = 0
    notes_moved: int = 0


def apply_plan(registry, plans: list[ConceptPlan], using: str = 'default') -> Summary:
    """Repoint every reference of each concept, then delete the ones nothing is left on.

    A concept is handled in a savepoint of its own: whatever goes wrong with one
    leaves it, and its references, exactly as they were, and the rest go on.
    A concept with non-serum references has only its serum references repointed
    and is kept. Every concept kept is logged at WARNING with the reason.
    """
    summary = Summary()
    priorities_removed = False
    for plan in plans:
        if plan.outcome not in ('delete', 'partial_non_serum'):
            summary.skipped += 1
            logger.warning('HK-Labs cleanup: kept %s (%s): %s', plan.hkl_code, plan.outcome, plan.detail)
            continue
        partial = plan.outcome == 'partial_non_serum'
        try:
            with transaction.atomic(using=using):
                stats = _apply_one(registry, using, plan, partial=partial)
        except (IntegrityError, RuntimeError) as exc:
            plan.outcome, plan.detail = 'failed', f'{type(exc).__name__}: {exc}'.splitlines()[0]
            summary.skipped += 1
            logger.warning('HK-Labs cleanup: kept %s (failed, rolled back): %s', plan.hkl_code, plan.detail)
            continue
        summary.rows_repointed += stats['repointed']
        summary.rows_deleted += stats['deleted']
        summary.notes_moved += stats['notes_moved']
        priorities_removed = priorities_removed or stats['priority_deleted']
        if partial:
            summary.partial += 1
            logger.warning('HK-Labs cleanup: kept %s (%s): %d row(s) repointed to LOINC %s; %s',
                           plan.hkl_code, plan.outcome, stats['repointed'], plan.target_code, plan.detail)
        else:
            summary.deleted += 1
            logger.info('HK-Labs cleanup: removed %s -> LOINC %s (%d row(s) repointed)',
                        plan.hkl_code, plan.target_code, stats['repointed'])
    if priorities_removed:
        _renumber_priorities(registry, using)
    return summary


def _apply_one(registry, using, plan, partial: bool = False) -> dict:
    cid, tid = plan.concept_id, plan.target_id
    Concept = registry.get_model('omop_core', 'Concept')
    stats = {'repointed': 0, 'deleted': 0, 'notes_moved': 0, 'priority_deleted': False}
    person_ids: set[int] = set()
    target_code = plan.target_code

    held = {(r.model_label, r.column) for r in plan.references if r.kind == 'fk'}
    for model, fld in concept_fk_columns(registry):
        key = (_label(model), fld.name)
        if key not in held:
            continue
        qs = model._default_manager.using(using).filter(**{fld.attname: cid})
        if partial:
            # The concept stays: only the serum references move, and nothing
            # that exists for the concept is touched.
            if key in DEPENDENT_COLUMNS or key in MOVE_OR_DROP_COLUMNS:
                continue
            qs = qs.exclude(pk__in=plan.skip.get(key, ()))
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
                taken = model._default_manager.using(using).filter(**{fld.attname: tid}).exists()
                if taken:
                    logger.warning('HK-Labs cleanup: %s: dropped a %s; the standard concept has one',
                                   plan.hkl_code, key[0])
                    row.delete()
                    stats['deleted'] += 1
                else:
                    setattr(row, fld.attname, tid)
                    row.save(update_fields=[fld.attname])
                    stats['notes_moved'] += 1
        else:
            if not qs.exists():
                continue
            if _has_field(model, 'person'):
                person_ids.update(qs.values_list('person_id', flat=True))
            twin = TWIN_COLUMNS.get(key)
            update = {fld.attname: tid}
            if twin:
                vocab_col, code_col = twin
                update[vocab_col] = STANDARD_VOCABULARY
                if code_col:
                    update[code_col] = target_code
            stats['repointed'] += qs.update(**update)

    for label, column in ID_COLUMNS:
        model = registry.get_model(*label.split('.'))
        qs = model._default_manager.using(using).filter(**{column: cid})
        if partial:
            qs = qs.exclude(pk__in=plan.skip.get((label, column), ()))
        stats['repointed'] += qs.update(**{column: tid})
    for label, column in ([] if partial else JSON_ID_LISTS):
        model = registry.get_model(*label.split('.'))
        for row in model._default_manager.using(using).filter(**{f'{column}__contains': [cid]}):
            kept = [i for i in getattr(row, column) if i != cid]
            model._default_manager.using(using).filter(pk=row.pk).update(**{column: kept})

    if person_ids:
        Record = registry.get_model('omop_core', 'PatientRecord')
        # Re-derived by `backfill_patient_records`, which selects on this.
        Record._default_manager.using(using).filter(person_id__in=person_ids).update(derivation_version=0)

    if partial:
        return stats
    # Only the columns that held a reference: discovery is complete, so these
    # are the ones that could still hold one.
    remaining = [
        f'{_label(m)}.{f.name}' for m, f in concept_fk_columns(registry)
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
    from django.db.models import Q
    return queryset.filter(standard_concept='S').filter(
        Q(invalid_reason__isnull=True) | Q(invalid_reason=''))


def find_standard_duplicate(name: str | None = None, code: str | None = None,
                            domain_id: str = 'Measurement'):
    """An active standard concept a new HK-Labs concept would duplicate, or None.

    Duplicate means the same name (case and spacing aside) in the domain, or
    the same LOINC code once an ``hkl:`` prefix is taken off. A name is matched
    on the standard concept's own name: no synonym or fuzzy match, because a
    near match is a curator's call and a wrong merge is worse than a duplicate.
    """
    from omop_core.models import Concept

    base = active_standard(Concept.objects.all())
    tail = _code_tail(code)
    if tail:
        hit = base.filter(vocabulary_id=STANDARD_VOCABULARY, concept_code=tail).first()
        if hit is not None:
            return hit
    wanted = normalize_name(name)
    if len(wanted) >= 3:
        for hit in base.filter(domain_id=domain_id, concept_name__iexact=(name or '').strip())[:20]:
            if normalize_name(hit.concept_name) == wanted:
                return hit
        # Spacing differences the iexact above cannot see.
        squeezed = re.sub(r'\s+', ' ', (name or '').strip())
        if squeezed != (name or '').strip():
            for hit in base.filter(domain_id=domain_id, concept_name__iexact=squeezed)[:20]:
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


def is_retired_code(vocabulary_id: str | None, concept_code: str | None) -> bool:
    return vocabulary_id == HK_LABS_VOCABULARY and (concept_code or '').strip().lower() in RETIRED_HKL_DUPLICATES
