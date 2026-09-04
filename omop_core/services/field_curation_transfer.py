"""Copy the field-curation tables from one PRomop instance to another.

The field-mapping screen is backed by five tables, and they only mean anything
together: a ``FieldConceptMapping`` names the OMOP concept a PatientRecord field
carries, ``FieldChoice``/``FieldChoiceCode`` bound its answers, ``FieldFormula``
computes it, ``FieldSynonym`` names it, and a ``CustomPatientField`` is a field
that exists *only* because a mapping was approved for it. Moving a subset
produces a half-configured instance — a custom field whose mapping is missing
cannot be saved at all — so the default is to move all five.

The transfer is split in two so it is testable without a second database:

* :func:`read_payload` turns one instance's curation tables into plain dicts.
* :func:`apply_payload` writes those dicts into the instance running the code.

Two things deliberately do not survive the trip:

* **Row IDs.** Rows are matched on their natural key (``field_name``, or
  ``(field_name, display)`` for a choice), never on the source PK, because the
  two instances assign PKs independently.
* **User foreign keys.** ``reviewer`` and ``created_by`` point at ``Identity``
  rows whose IDs mean something different on each instance, so they are cleared
  rather than carried over — a wrong attribution is worse than none.

The concept FK is re-resolved rather than copied. ``concept_id`` is stable
across instances for anything loaded from Athena, but locally minted concepts
(``Concept.source == 'HealthKey'``) are numbered per instance, so the same id
can name a different concept here. ``(vocabulary_id, concept_code)`` is unique
and is the meaning, so it wins; the id is only a fallback for a mapping that
never recorded a code.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from django.db import transaction

from omop_core.models import (
    Concept,
    CustomPatientField,
    FieldChoice,
    FieldChoiceCode,
    FieldConceptMapping,
    FieldFormula,
    FieldSynonym,
)

# Table keys, in dependency order: a CustomPatientField requires its mapping, so
# mappings are written first and pruned last.
TABLES = ('mappings', 'custom_fields', 'choices', 'formulas', 'synonyms')
# The field-mapping page calls these two APIs.  The other curation tables are
# supported for explicit migrations, but copying them by default would broaden
# a field-mapping copy into unrelated configuration.
DEFAULT_TABLES = ('mappings', 'synonyms')

# Columns copied verbatim for each mapping. Excludes id, concept (re-resolved),
# reviewer (cleared), and the auto timestamps.
_MAPPING_FIELDS = (
    'vocabulary_id', 'concept_code', 'unit', 'omop_table', 'source_value',
    'value_kind', 'type_concept_id', 'value_vocabulary', 'multiple',
    'status', 'reviewed_at', 'notes',
)
_CUSTOM_FIELD_FIELDS = ('display_name', 'tab', 'field_type', 'mode')


@dataclass
class TransferStats:
    """What a run did, per table."""
    created: dict[str, int] = dataclass_field(default_factory=dict)
    updated: dict[str, int] = dataclass_field(default_factory=dict)
    deleted: dict[str, int] = dataclass_field(default_factory=dict)
    skipped: dict[str, int] = dataclass_field(default_factory=dict)
    warnings: list[str] = dataclass_field(default_factory=list)

    def _bump(self, bucket: dict[str, int], table: str, n: int = 1) -> None:
        bucket[table] = bucket.get(table, 0) + n

    def total(self, bucket: dict[str, int]) -> int:
        return sum(bucket.values())


class _Rollback(Exception):
    """Raised to unwind the transaction after a --dry-run."""


def read_payload(using: str, tables: tuple[str, ...] = TABLES) -> dict:
    """Read the curation tables from ``using`` into JSON-safe dicts.

    Read-only: nothing is written to the source instance.
    """
    payload: dict[str, list[dict]] = {}

    if 'mappings' in tables:
        payload['mappings'] = [
            {
                'field_name': m.field_name,
                # The concept's own code, not just the denormalized columns —
                # a mapping saved before those were populated still resolves.
                'concept_vocabulary_id': m.concept.vocabulary_id if m.concept else '',
                'concept_code_resolved': m.concept.concept_code if m.concept else '',
                'concept_id': m.concept_id,
                **{name: getattr(m, name) for name in _MAPPING_FIELDS},
            }
            for m in FieldConceptMapping.objects.using(using)
            .select_related('concept')
            .order_by('field_name')
        ]

    if 'custom_fields' in tables:
        payload['custom_fields'] = [
            {
                'field_name': c.field_name,
                'mapping_field_name': c.mapping.field_name,
                **{name: getattr(c, name) for name in _CUSTOM_FIELD_FIELDS},
            }
            for c in CustomPatientField.objects.using(using)
            .select_related('mapping')
            .order_by('field_name')
        ]

    if 'choices' in tables:
        payload['choices'] = [
            {
                'field_name': fc.field_name,
                'display': fc.display,
                'sort_order': fc.sort_order,
                'codes': [
                    {
                        'code': code.code,
                        'vocabulary_id': code.vocabulary_id,
                        'display': code.display,
                        'is_primary': code.is_primary,
                    }
                    for code in sorted(
                        fc.codes.all(), key=lambda c: (c.vocabulary_id, c.code)
                    )
                ],
            }
            for fc in FieldChoice.objects.using(using)
            .prefetch_related('codes')
            .order_by('field_name', 'sort_order', 'display')
        ]

    if 'formulas' in tables:
        payload['formulas'] = [
            {
                'field_name': f.field_name,
                'formula': f.formula,
                'is_active': f.is_active,
            }
            for f in FieldFormula.objects.using(using).order_by('field_name')
        ]

    if 'synonyms' in tables:
        payload['synonyms'] = [
            {
                'field_name': s.field_name,
                'synonym_text': s.synonym_text,
                'source': s.source,
            }
            for s in FieldSynonym.objects.using(using)
            .order_by('field_name', 'synonym_text')
        ]

    return payload


def apply_payload(
    payload: dict,
    tables: tuple[str, ...] = TABLES,
    prune: bool = False,
    dry_run: bool = False,
) -> TransferStats:
    """Write ``payload`` into the default database.

    Existing rows matching on the natural key are overwritten from the payload.
    With ``prune``, rows the payload does not mention are deleted, making the
    target an exact mirror of the source.
    """
    stats = TransferStats()
    try:
        with transaction.atomic():
            if 'mappings' in tables:
                _apply_mappings(payload.get('mappings', []), stats)
            if 'custom_fields' in tables:
                _apply_custom_fields(payload.get('custom_fields', []), stats)
            if 'choices' in tables:
                _apply_choices(payload.get('choices', []), stats)
            if 'formulas' in tables:
                _apply_formulas(payload.get('formulas', []), stats)
            if 'synonyms' in tables:
                _apply_synonyms(payload.get('synonyms', []), stats)
            if prune:
                _prune(payload, tables, stats)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    return stats


# ── Concept resolution ────────────────────────────────────────────────────

def _resolve_concept(row: dict, stats: TransferStats) -> Concept | None:
    """Find the local Concept a source mapping refers to.

    Prefers ``(vocabulary_id, concept_code)`` — unique, and the same pair names
    the same concept on any instance. Falls back to the raw ``concept_id`` only
    when the source recorded no code.
    """
    vocab = row.get('concept_vocabulary_id') or row.get('vocabulary_id') or ''
    code = row.get('concept_code_resolved') or row.get('concept_code') or ''
    if vocab and code:
        concept = Concept.objects.filter(
            vocabulary_id=vocab, concept_code=code
        ).first()
        if concept is not None:
            return concept
    else:
        # Old rows may predate the denormalized vocabulary/code columns.  An ID
        # is safe only when there is no semantic identifier to resolve: if a
        # supplied code is absent locally, the same numeric ID may name a
        # completely different locally-created concept on this instance.
        concept_id = row.get('concept_id')
        if concept_id is not None:
            concept = Concept.objects.filter(concept_id=concept_id).first()
            if concept is not None:
                return concept

    concept_id = row.get('concept_id')
    if concept_id is not None or (vocab and code):
        stats.warnings.append(
            f"{row['field_name']}: concept {vocab}:{code or concept_id} is not "
            f"loaded on this instance — mapping copied with no concept."
        )
    return None


# ── Per-table application ─────────────────────────────────────────────────

def _apply_mappings(rows: list[dict], stats: TransferStats) -> None:
    existing = {m.field_name: m for m in FieldConceptMapping.objects.all()}
    for row in rows:
        values = {name: row[name] for name in _MAPPING_FIELDS}
        values['concept'] = _resolve_concept(row, stats)
        # Attribution does not cross instances — see module docstring.
        values['reviewer'] = None

        mapping = existing.get(row['field_name'])
        if mapping is None:
            FieldConceptMapping.objects.create(field_name=row['field_name'], **values)
            stats._bump(stats.created, 'mappings')
            continue
        for name, value in values.items():
            setattr(mapping, name, value)
        mapping.save(update_fields=[*values, 'updated_at'])
        stats._bump(stats.updated, 'mappings')


def _apply_custom_fields(rows: list[dict], stats: TransferStats) -> None:
    existing = {c.field_name: c for c in CustomPatientField.objects.all()}
    mappings = {m.field_name: m for m in FieldConceptMapping.objects.all()}
    for row in rows:
        mapping = mappings.get(row['mapping_field_name'])
        if mapping is None:
            # Only reachable when --tables excluded mappings; a custom field
            # cannot be created without one.
            stats.warnings.append(
                f"custom field {row['field_name']}: mapping "
                f"{row['mapping_field_name']} is not present — skipped."
            )
            stats._bump(stats.skipped, 'custom_fields')
            continue
        values = {name: row[name] for name in _CUSTOM_FIELD_FIELDS}
        values['mapping'] = mapping
        values['created_by'] = None
        custom = existing.get(row['field_name'])
        if custom is None:
            CustomPatientField.objects.create(field_name=row['field_name'], **values)
            stats._bump(stats.created, 'custom_fields')
            continue
        for name, value in values.items():
            setattr(custom, name, value)
        custom.save(update_fields=[*values, 'updated_at'])
        stats._bump(stats.updated, 'custom_fields')


def _apply_choices(rows: list[dict], stats: TransferStats) -> None:
    existing = {(c.field_name, c.display): c for c in FieldChoice.objects.all()}
    for row in rows:
        key = (row['field_name'], row['display'])
        choice = existing.get(key)
        if choice is None:
            choice = FieldChoice.objects.create(
                field_name=row['field_name'],
                display=row['display'],
                sort_order=row['sort_order'],
                created_by=None,
            )
            stats._bump(stats.created, 'choices')
        else:
            choice.sort_order = row['sort_order']
            choice.created_by = None
            choice.save(update_fields=['sort_order', 'created_by'])
            stats._bump(stats.updated, 'choices')
        # Codes are pure data hanging off the choice, so the source's set
        # replaces the local one wholesale rather than being merged.
        choice.codes.all().delete()
        FieldChoiceCode.objects.bulk_create([
            FieldChoiceCode(choice=choice, **code) for code in row['codes']
        ])


def _apply_formulas(rows: list[dict], stats: TransferStats) -> None:
    existing = {f.field_name: f for f in FieldFormula.objects.all()}
    for row in rows:
        formula = existing.get(row['field_name'])
        if formula is None:
            FieldFormula.objects.create(
                field_name=row['field_name'],
                formula=row['formula'],
                is_active=row['is_active'],
                created_by=None,
            )
            stats._bump(stats.created, 'formulas')
            continue
        formula.formula = row['formula']
        formula.is_active = row['is_active']
        formula.created_by = None
        formula.save(update_fields=['formula', 'is_active', 'created_by', 'updated_at'])
        stats._bump(stats.updated, 'formulas')


def _apply_synonyms(rows: list[dict], stats: TransferStats) -> None:
    existing = {
        (s.field_name, s.synonym_text): s for s in FieldSynonym.objects.all()
    }
    for row in rows:
        key = (row['field_name'], row['synonym_text'])
        synonym = existing.get(key)
        if synonym is None:
            FieldSynonym.objects.create(
                field_name=row['field_name'],
                synonym_text=row['synonym_text'],
                source=row['source'],
                created_by=None,
            )
            stats._bump(stats.created, 'synonyms')
            continue
        synonym.source = row['source']
        synonym.created_by = None
        synonym.save(update_fields=['source', 'created_by'])
        stats._bump(stats.updated, 'synonyms')


def _prune(payload: dict, tables: tuple[str, ...], stats: TransferStats) -> None:
    """Delete local rows the source does not have.

    Custom fields go before mappings: the FK is PROTECT, so a mapping that only
    exists to back a pruned custom field cannot be deleted before it.
    """
    if 'custom_fields' in tables:
        keep = {row['field_name'] for row in payload.get('custom_fields', [])}
        deleted, _ = CustomPatientField.objects.exclude(field_name__in=keep).delete()
        stats._bump(stats.deleted, 'custom_fields', deleted)

    if 'mappings' in tables:
        keep = {row['field_name'] for row in payload.get('mappings', [])}
        deleted, _ = FieldConceptMapping.objects.exclude(field_name__in=keep).delete()
        stats._bump(stats.deleted, 'mappings', deleted)

    if 'choices' in tables:
        keep = {(row['field_name'], row['display']) for row in payload.get('choices', [])}
        stale = [c.pk for c in FieldChoice.objects.all()
                 if (c.field_name, c.display) not in keep]
        if stale:
            # Cascades to FieldChoiceCode; count only the choices themselves.
            FieldChoice.objects.filter(pk__in=stale).delete()
            stats._bump(stats.deleted, 'choices', len(stale))

    if 'formulas' in tables:
        keep = {row['field_name'] for row in payload.get('formulas', [])}
        deleted, _ = FieldFormula.objects.exclude(field_name__in=keep).delete()
        stats._bump(stats.deleted, 'formulas', deleted)

    if 'synonyms' in tables:
        keep = {(row['field_name'], row['synonym_text'])
                for row in payload.get('synonyms', [])}
        stale = [s.pk for s in FieldSynonym.objects.all()
                 if (s.field_name, s.synonym_text) not in keep]
        if stale:
            FieldSynonym.objects.filter(pk__in=stale).delete()
            stats._bump(stats.deleted, 'synonyms', len(stale))
