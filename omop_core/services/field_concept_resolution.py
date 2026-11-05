"""Point field mappings at their Concept once the vocabulary that names it is loaded.

A ``FieldConceptMapping`` carries its concept twice: as the db-constrained
``concept`` FK and as the ``(vocabulary_id, concept_code)`` text the FK was
resolved from. On a brand-new instance the migrations run before
``load_athena_vocabularies``, so when 0240 seeds the rows from
``field_curation_v1.json`` there is no Concept to point at and every row
lands with ``concept = NULL``. Nothing re-saved them afterwards, so a fresh
instance kept deriving its labs from the ``_LOINC_LAB_FIELDS`` fallback and
showed every seeded row as unresolved in the editor, until each row was
re-saved by hand.

``resolve_field_concept_mappings`` is the repair: it finds every mapping with
no concept but a vocabulary and a code, looks the Concept up by that pair in
batches, and sets the FK. It is idempotent (a resolved row is never a
candidate again) and it never changes a row's text columns, status or
``updated_at``: the mapping's intent is unchanged, only the pointer the
schema could not hold yet is filled in. The Athena loader runs it after a
successful load, and ``manage.py resolve_field_concept_mappings`` runs it on
an instance that loaded Athena before the loader did so.

Only ``FieldConceptMapping`` needs this. The other curation tables either
keep their concept FKs unconstrained (``SourceCodeConceptMapping``,
``SourceCodeAxisVariant``: ``db_constraint=False``, so the seed can store the
id before the Concept exists), carry text only (``FieldChoiceCode``), or are
keyed on the Concept itself (``ConceptMappingPriority``).
"""
from collections import defaultdict
from dataclasses import dataclass, field
import logging

from django.db import transaction
from django.db.models import QuerySet

from omop_core.models import Concept, FieldConceptMapping

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# How many unresolved references a log line names before it says "and N more".
_LOGGED_REFS = 20


@dataclass
class ResolutionResult:
    """What one pass did: rows pointed at a Concept, and rows still without one."""

    resolved: int = 0
    unresolved: int = 0
    # (field_name, vocabulary_id, concept_code) of every row left unresolved,
    # in field_name order, so a caller can name them.
    unresolved_refs: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.resolved + self.unresolved

    def summary(self) -> str:
        if not self.total:
            return 'field mappings: every row with a concept code already names its Concept.'
        text = (f'field mappings: {self.resolved} row(s) now point at their Concept, '
                f'{self.unresolved} still unresolved')
        if self.unresolved_refs:
            shown = ', '.join(f'{name} ({vocab}:{code})'
                              for name, vocab, code in self.unresolved_refs[:_LOGGED_REFS])
            more = len(self.unresolved_refs) - _LOGGED_REFS
            text += f': {shown}' + (f', and {more} more' if more > 0 else '')
        return text + '.'


def unresolved_mappings() -> QuerySet:
    """Mappings that name a concept by code but do not point at one.

    A row with no vocabulary or no code names nothing (the fixture carries 17
    such approved rows: fields with a recipe but no concept), so it is not a
    candidate and never counts as unresolved.
    """
    return (
        FieldConceptMapping.objects
        .filter(concept__isnull=True)
        .exclude(vocabulary_id='')
        .exclude(concept_code='')
        .order_by('field_name')
    )


def resolve_field_concept_mappings(batch_size: int = BATCH_SIZE) -> ResolutionResult:
    """Set ``concept`` on every mapping whose ``(vocabulary_id, concept_code)`` is loaded.

    Each batch is one Concept query and one UPDATE per distinct concept, in
    its own transaction, so a partial run leaves whole batches resolved and
    the next run picks up the rest. A row only ever moves from NULL to the
    Concept its own text names: a curator's choice is never overwritten, and
    the WHERE on the UPDATE re-checks NULL so a concurrent save wins.
    """
    result = ResolutionResult()
    rows = list(unresolved_mappings().values_list(
        'pk', 'field_name', 'vocabulary_id', 'concept_code',
    ))
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        wanted = {(vocab, code) for _, _, vocab, code in chunk}
        # One superset query per batch (vocabularies × codes), narrowed to the
        # exact pairs in Python, as field_curation_transfer._concept_caches does.
        concept_ids = {
            (vocab, code): concept_id
            for vocab, code, concept_id in Concept.objects.filter(
                vocabulary_id__in={vocab for vocab, _ in wanted},
                concept_code__in={code for _, code in wanted},
            ).values_list('vocabulary_id', 'concept_code', 'concept_id')
            if (vocab, code) in wanted
        }
        pks_by_concept: dict[int, list[int]] = defaultdict(list)
        for pk, field_name, vocab, code in chunk:
            concept_id = concept_ids.get((vocab, code))
            if concept_id is None:
                result.unresolved += 1
                result.unresolved_refs.append((field_name, vocab, code))
                continue
            pks_by_concept[concept_id].append(pk)
        with transaction.atomic():
            for concept_id, pks in pks_by_concept.items():
                result.resolved += FieldConceptMapping.objects.filter(
                    pk__in=pks, concept__isnull=True,
                ).update(concept_id=concept_id)
    if result.unresolved:
        logger.warning(result.summary())
    else:
        logger.info(result.summary())
    return result
