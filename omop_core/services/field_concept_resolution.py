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

``resolve_field_concept_mappings`` is that re-save, done for every row at
once: it finds every mapping with no concept, works out the
``(vocabulary_id, concept_code)`` pair that names its concept, looks the
Concept up in batches, and saves the FK on each row exactly as the editor
would, so the ``post_save`` projection (``signals.field_concept_mapping_saved``)
backfills any pending user edits on a field whose mapping just came into
force. It is idempotent (a resolved row is never a candidate again) and it
never changes a row's text columns, status, provenance or ``updated_at``: the
mapping's intent is unchanged, only the pointer the schema could not hold yet
is filled in. The Athena loader runs it after a successful load, and
``manage.py resolve_field_concept_mappings`` runs it on an instance that
loaded Athena before the loader did so.

Where the pair comes from, strongest first:

- the row's own text columns, when both are set: the curator's entry;
- the fixture's resolved pair for that field name (``concept_vocabulary_id``,
  ``concept_code_resolved`` in ``field_curation_v1.json``), when the text is
  blank. The three SCT rows deliberately carry no code (they are identified
  by ``source_value``, see migration 0156) and name their concept only this
  way, so without it they would never resolve on a fresh instance.

A row with neither names no concept (the fixture's recipe-only rows) and is
not a candidate. A row at concept 0, the "No matching concept" placeholder
the genomics seeds park a field on, already points at a Concept and is not
a candidate either, so "0 still unresolved" after a load means every
candidate's code was found, not that every field has a real concept.

Only ``FieldConceptMapping`` needs this. The other curation tables either
keep their concept FKs unconstrained (``SourceCodeConceptMapping``,
``SourceCodeAxisVariant``: ``db_constraint=False``, so the seed can store the
id before the Concept exists), carry text only (``FieldChoiceCode``), or are
keyed on the Concept itself (``ConceptMappingPriority``).
"""
from dataclasses import dataclass, field
from functools import lru_cache
import json
import logging
from pathlib import Path

from django.db import transaction
from django.db.models import QuerySet

from omop_core.models import Concept, FieldConceptMapping

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

FIXTURE_PATH = Path(__file__).resolve().parent.parent / 'data' / 'field_curation_v1.json'

# How many unresolved references a log line names before it says "and N more".
_LOGGED_REFS = 20

# The fixture's "resolved pair" for a row that resolved to nothing on the
# source instance: concept 0 (migration 0077), which names no vocabulary code.
_NO_CONCEPT = ('None', 'No matching concept')


@dataclass
class ResolutionResult:
    """What one pass did: rows pointed at a Concept, and rows still without one."""

    resolved: int = 0
    unresolved: int = 0
    # (field_name, vocabulary_id, concept_code) of every row left unresolved,
    # in field_name order, so a caller can name them.
    unresolved_refs: list[tuple[str, str, str]] = field(default_factory=list)
    # Rows resolved to a concept Athena has since retired (invalid_reason
    # set): the pair is what the fixture names, so it is honoured, but a
    # curator should re-point the row at the active replacement.
    retired_refs: list[tuple[str, str, str]] = field(default_factory=list)

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
        text += '.'
        if self.retired_refs:
            shown = ', '.join(f'{name} ({vocab}:{code})'
                              for name, vocab, code in self.retired_refs[:_LOGGED_REFS])
            text += (f' {len(self.retired_refs)} of the resolved point at a retired '
                     f'concept, re-curate: {shown}.')
        return text


@lru_cache(maxsize=1)
def fixture_concept_refs() -> dict[str, tuple[str, str]]:
    """field_name → the (vocabulary_id, concept_code) the fixture resolved it to.

    Only rows whose resolved pair names a real concept are listed; a row the
    source instance had at concept 0 or at no concept names nothing here.
    Read once per process: the fixture is checked in and does not change.
    """
    if not FIXTURE_PATH.exists():
        # Checked in, and 0240 needs it too: a missing file is a broken
        # install, so say so rather than quietly treating the uncoded rows
        # as naming nothing.
        logger.warning('field mappings: fixture %s not found; uncoded rows cannot resolve',
                       FIXTURE_PATH)
        return {}
    data = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    refs = {}
    for row in data.get('mappings', []):
        pair = (row.get('concept_vocabulary_id') or '', row.get('concept_code_resolved') or '')
        if all(pair) and pair != _NO_CONCEPT:
            refs[row['field_name']] = pair
    return refs


def unresolved_mappings() -> QuerySet:
    """Mappings with no concept, whether or not a pair can be found for them."""
    return FieldConceptMapping.objects.filter(concept__isnull=True).order_by('field_name')


def _concept_pair(field_name: str, vocabulary_id: str, concept_code: str) -> tuple[str, str] | None:
    """The pair that names this row's concept, or None when nothing does."""
    if vocabulary_id and concept_code:
        return (vocabulary_id, concept_code)
    return fixture_concept_refs().get(field_name)


def resolve_field_concept_mappings(
    batch_size: int = BATCH_SIZE, *, dry_run: bool = False,
) -> ResolutionResult:
    """Set ``concept`` on every mapping whose named concept is loaded.

    Each batch is one Concept query; each row that resolved is then locked,
    re-checked for NULL and saved in a savepoint of its own, in pk order, so
    a concurrent curator save wins (it waits for one row, not a batch) and a
    row that already points at a concept is never touched. The savepoint is
    also where the post_save projection runs inline when no broker is
    configured. A caller holding a transaction, such as the Athena loader's
    hook under ``sync_athena_vocabulary``, commits or discards the lot with
    it. With ``dry_run`` the rows are counted and nothing is locked, saved
    or projected.
    """
    result = ResolutionResult()
    candidates = []
    for pk, field_name, vocab, code in unresolved_mappings().values_list(
        'pk', 'field_name', 'vocabulary_id', 'concept_code',
    ):
        pair = _concept_pair(field_name, vocab, code)
        if pair is not None:
            candidates.append((pk, field_name, pair))
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start:start + batch_size]
        wanted = {pair for _, _, pair in chunk}
        # One superset query per batch (vocabularies × codes), narrowed to the
        # exact pairs in Python, as field_curation_transfer._concept_caches does.
        # (vocabulary_id, concept_code) is unique on concept, so a pair names
        # at most one row. A retired concept still resolves (it is what the
        # fixture names, as in apply_payload) and is reported.
        concepts = {
            (vocab, code): (concept_id, bool(invalid_reason))
            for vocab, code, concept_id, invalid_reason in Concept.objects.filter(
                vocabulary_id__in={vocab for vocab, _ in wanted},
                concept_code__in={code for _, code in wanted},
            ).values_list('vocabulary_id', 'concept_code', 'concept_id', 'invalid_reason')
            if (vocab, code) in wanted
        }
        concept_by_pk: dict[int, int] = {}
        for pk, field_name, (vocab, code) in chunk:
            found = concepts.get((vocab, code))
            if found is None:
                result.unresolved += 1
                result.unresolved_refs.append((field_name, vocab, code))
                continue
            concept_id, retired = found
            concept_by_pk[pk] = concept_id
            if retired:
                result.retired_refs.append((field_name, vocab, code))
        if dry_run:
            result.resolved += len(concept_by_pk)
            continue
        for pk in sorted(concept_by_pk):
            with transaction.atomic():
                mapping = FieldConceptMapping.objects.select_for_update().filter(
                    pk=pk, concept__isnull=True,
                ).first()
                if mapping is None:
                    continue
                # save(), not update(): the post_save receiver is what
                # projects a pending user edit once the mapping is in force,
                # as a hand re-save in the editor would. update_fields keeps
                # auto_now updated_at as it was.
                mapping.concept_id = concept_by_pk[pk]
                mapping.save(update_fields=['concept'])
                result.resolved += 1
    return result
