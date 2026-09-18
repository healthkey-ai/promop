"""Copy reference data (see instance_data) from another PRomop instance into this one.

Rows are matched on a natural key, never on the source pk, because instances
number rows independently. Foreign keys travel as the related row's natural
key and concepts as (vocabulary_id, concept_code), then resolve on arrival.
Links to system data, like the Identity that created a row, are cleared: the
same id is a different person on another instance.

The field-mapping and code-mapping tables go through field_curation_transfer,
whose payload is also a fixture format. Everything here wraps it.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from typing import Any, TypedDict

from django.db import transaction
from django.db.models import Field, Model

from omop_core.models import (
    Concept,
    ConceptClass,
    DiseaseTherapyRegimen,
    Domain,
    MappingDestinationCandidate,
    SourceCodeConceptMapping,
    TherapyComponentClassLink,
    TherapyOutcome,
    TherapyRegimenComponent,
    ToxicityGrade,
    Vocabulary,
)
from omop_core.services.field_curation_transfer import (
    TABLES as CURATION_TABLES,
    TransferStats,
    apply_payload,
    read_payload,
)
from omop_core.services.instance_copy import (
    CHUNK,
    ConceptRef,
    chunked,
    concept_refs,
    local_concept_ids,
)
from omop_core.mapping.therapy import SOURCE_HEALTHKEY
from omop_core.services.instance_data import lookup_models
from omop_core.services.pk import next_pk_batch

Columns = dict[str, Any]
Key = tuple[Any, ...]


class ReferenceRow(TypedDict):
    """One row, with links as natural keys so it means the same on any instance."""
    values: Columns
    links: dict[str, Key | None]
    concepts: dict[str, ConceptRef | None]
    m2m: dict[str, list[Key]]


class LocalConcepts(TypedDict):
    """HealthKey-minted concepts and the vocabulary rows they need."""
    vocabularies: list[Columns]
    domains: list[Columns]
    classes: list[Columns]
    concepts: list[Columns]


class ReferencePayload(TypedDict):
    """Everything read_reference reads, ready for apply_reference."""
    local_concepts: LocalConcepts
    tables: dict[str, Iterable[ReferenceRow]]
    curation: dict[str, Any]
    candidates: Iterable[ReferenceRow]


@dataclass(frozen=True)
class ReferenceTable:
    """A copied reference model and the columns that identify a row on any instance."""
    model: type[Model]
    key: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.model._meta.object_name


class _Rollback(Exception):
    """Unwinds the transaction after a dry run."""


@cache
def copied_tables() -> list[ReferenceTable]:
    """Tables in write order: a row comes after every table its keys point at."""
    models = {*lookup_models(), ToxicityGrade}
    # MutationCode points at MutationGene, so lookups without such links go first.
    lookups = sorted(models, key=lambda m: (
        any(f.related_model in models for f in m._meta.concrete_fields), m._meta.label,
    ))
    return [
        *(ReferenceTable(m, ('code',)) for m in lookups),
        ReferenceTable(TherapyRegimenComponent, ('regimen', 'component')),
        ReferenceTable(TherapyComponentClassLink, ('component', 'therapy_class')),
        ReferenceTable(DiseaseTherapyRegimen, ('disease', 'round', 'regimen')),
        ReferenceTable(TherapyOutcome, ('code',)),
    ]


# After the code mappings, because each candidate belongs to one.
CANDIDATES = ReferenceTable(
    MappingDestinationCandidate, ('mapping', 'target_vocabulary_id', 'target_concept_code'),
)


@cache
def _related_keys() -> dict[type[Model], tuple[str, ...]]:
    """Natural key of every model a copied row may point at. Plain columns only."""
    keys = {t.model: t.key for t in copied_tables() if len(t.key) == 1}
    keys[SourceCodeConceptMapping] = ('source_vocabulary_id', 'source_code')
    return keys


def read_reference(using: str, stream: bool = False) -> ReferencePayload:
    """Read every copied reference table from using. Nothing is written there.

    With stream the large tables are generators, so the source connection must
    stay open until apply_reference has consumed them.
    """
    def rows(table: ReferenceTable) -> Iterable[ReferenceRow]:
        read = _read_table(table, using)
        return read if stream else list(read)

    return {
        'local_concepts': _read_local_concepts(using),
        'tables': {t.label: rows(t) for t in copied_tables()},
        'curation': read_payload(using, tables=CURATION_TABLES, stream=stream),
        'candidates': rows(CANDIDATES),
    }


def apply_reference(payload: ReferencePayload, prune: bool = False, dry_run: bool = False) -> TransferStats:
    """Write a read_reference payload into this database in one transaction.

    prune deletes curation rows the source lacks. It never deletes lookups or
    therapy reference rows, because patient data may point at them.
    """
    stats = TransferStats()
    try:
        with transaction.atomic():
            _apply_local_concepts(payload['local_concepts'], stats)
            for table in copied_tables():
                _apply_table(table, payload['tables'][table.label], stats)
            _merge(stats, apply_payload(payload['curation'], tables=CURATION_TABLES, prune=prune))
            _apply_table(CANDIDATES, payload['candidates'], stats)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    return stats


def _foreign_keys(model: type[Model]) -> list[Field]:
    """Concrete many-to-one fields of model."""
    return [f for f in model._meta.concrete_fields if f.many_to_one]


def _read_table(table: ReferenceTable, using: str) -> Iterator[ReferenceRow]:
    """Yield rows with links replaced by natural keys and concept refs."""
    model = table.model
    related_keys = _related_keys()
    plain = [f for f in model._meta.concrete_fields if not f.is_relation and not f.primary_key]
    fks = _foreign_keys(model)
    m2m = list(model._meta.many_to_many)
    queryset = model.objects.using(using).order_by('pk').prefetch_related(*(f.name for f in m2m))
    for chunk in chunked(queryset.iterator(chunk_size=CHUNK)):
        concepts = concept_refs(
            using, (getattr(o, f.attname) for o in chunk for f in fks if f.related_model is Concept),
        )
        keys = {
            f.name: _pk_to_key(f.related_model, using, {getattr(o, f.attname) for o in chunk})
            for f in fks if f.related_model in related_keys
        }
        for obj in chunk:
            yield {
                'values': {f.attname: getattr(obj, f.attname) for f in plain},
                'links': {name: by_pk.get(getattr(obj, f'{name}_id')) for name, by_pk in keys.items()},
                'concepts': {
                    f.name: concepts.get(getattr(obj, f.attname))
                    for f in fks if f.related_model is Concept
                },
                'm2m': {
                    f.name: sorted(tuple(getattr(r, k) for k in related_keys[f.related_model])
                                   for r in getattr(obj, f.name).all())
                    for f in m2m
                },
            }


def _pk_to_key(model: type[Model], using: str, pks: Iterable[Any]) -> dict[Any, Key]:
    """Natural key of each pk on the given database."""
    wanted = {pk for pk in pks if pk is not None}
    if not wanted:
        return {}
    fields = _related_keys()[model]
    return {
        pk: tuple(rest)
        for pk, *rest in model.objects.using(using).filter(pk__in=wanted).values_list('pk', *fields)
    }


def _key_to_pk(model: type[Model], keys: Iterable[Key | None]) -> dict[Key, Any]:
    """Pk here of each natural key that exists here."""
    wanted = {k for k in keys if k is not None}
    if not wanted:
        return {}
    fields = _related_keys()[model]
    lookup = {f'{name}__in': {k[i] for k in wanted} for i, name in enumerate(fields)}
    return {
        tuple(rest): pk
        for pk, *rest in model.objects.filter(**lookup).values_list('pk', *fields)
        if tuple(rest) in wanted
    }


def _apply_table(table: ReferenceTable, rows: Iterable[ReferenceRow], stats: TransferStats) -> None:
    """Create or overwrite rows by natural key, a chunk at a time."""
    model, label = table.model, table.label
    fks = {f.name: f for f in _foreign_keys(model)}
    cleared = [f for f in fks.values() if f.related_model is not Concept and f.related_model not in _related_keys()]
    key_attnames = [model._meta.get_field(name).attname for name in table.key]
    copied = [f.attname for f in model._meta.concrete_fields if not f.primary_key]

    for chunk in chunked(rows):
        link_pks = {
            name: _key_to_pk(fks[name].related_model, (row['links'][name] for row in chunk))
            for name in chunk[0]['links']
        }
        concept_ids = local_concept_ids(
            ref for row in chunk for ref in row['concepts'].values() if ref is not None
        )
        wanted: list[tuple[ReferenceRow, Columns]] = []
        for row in chunk:
            values = _resolve_row(row, fks, link_pks, concept_ids, label, stats)
            if values is None:
                stats._bump(stats.skipped, label)
                continue
            values.update({f.attname: None for f in cleared})
            wanted.append((row, values))

        existing = _existing_by_key(model, key_attnames, [v for _, v in wanted])
        creates, updates = [], []
        for row, values in wanted:
            obj = existing.get(tuple(values[a] for a in key_attnames))
            if obj is None:
                creates.append((row, model(**values)))
                continue
            for attname, value in values.items():
                setattr(obj, attname, value)
            updates.append((row, obj))
        model.objects.bulk_create([obj for _, obj in creates])
        if updates:
            model.objects.bulk_update([obj for _, obj in updates], copied)
        _apply_m2m(model, creates + updates, stats)
        stats._bump(stats.created, label, len(creates))
        stats._bump(stats.updated, label, len(updates))


def _resolve_row(
    row: ReferenceRow, fks: dict[str, Field], link_pks: dict[str, dict[Key, Any]],
    concept_ids: dict[ConceptRef, int], label: str, stats: TransferStats,
) -> Columns | None:
    """Target column values for a row, or None when a required link is missing here."""
    values = dict(row['values'])
    for name, key in row['links'].items():
        pk = link_pks[name].get(key) if key is not None else None
        if key is not None and pk is None:
            stats.warn(f'{label}: {fks[name].related_model._meta.object_name} {key} is not on this instance.')
            if not fks[name].null:
                return None
        values[fks[name].attname] = pk
    for name, ref in row['concepts'].items():
        concept_id = concept_ids.get(ref) if ref is not None else None
        if ref is not None and concept_id is None:
            stats.warn(f'{label}: concept {ref[0]}:{ref[1]} is not loaded on this instance.')
            if not fks[name].null:
                return None
        values[fks[name].attname] = concept_id
    return values


def _existing_by_key(
    model: type[Model], key_attnames: list[str], wanted: list[Columns],
) -> dict[Key, Model]:
    """Rows here that match the natural keys of wanted."""
    if not wanted:
        return {}
    lookup = {f'{a}__in': {v[a] for v in wanted} for a in key_attnames}
    return {tuple(getattr(obj, a) for a in key_attnames): obj for obj in model.objects.filter(**lookup)}


def _apply_m2m(model: type[Model], saved: list[tuple[ReferenceRow, Model]], stats: TransferStats) -> None:
    """Set many-to-many links of saved rows by the related natural keys."""
    for field in model._meta.many_to_many:
        related = field.related_model
        pks = _key_to_pk(related, (tuple(k) for row, _ in saved for k in row['m2m'][field.name]))
        for row, obj in saved:
            keys = [tuple(k) for k in row['m2m'][field.name]]
            for key in (k for k in keys if k not in pks):
                stats.warn(f'{model._meta.object_name}: {related._meta.object_name} {key} is not on this instance.')
            getattr(obj, field.name).set([pks[k] for k in keys if k in pks])


def _read_local_concepts(using: str) -> LocalConcepts:
    """Concepts no release has, with the vocabulary, domain and class rows they need."""
    concepts = list(Concept.objects.using(using).filter(source=SOURCE_HEALTHKEY).values())
    return {
        'vocabularies': list(Vocabulary.objects.using(using).filter(
            vocabulary_id__in={c['vocabulary_id'] for c in concepts}).values()),
        'domains': list(Domain.objects.using(using).filter(
            domain_id__in={c['domain_id'] for c in concepts}).values()),
        'classes': list(ConceptClass.objects.using(using).filter(
            concept_class_id__in={c['concept_class_id'] for c in concepts}).values()),
        'concepts': concepts,
    }


def _apply_local_concepts(data: LocalConcepts, stats: TransferStats) -> None:
    """Add missing HealthKey concepts under this instance's own ids, update the rest."""
    # Only missing rows: the release loader owns the ones already here.
    for model, part in ((Vocabulary, 'vocabularies'), (Domain, 'domains'), (ConceptClass, 'classes')):
        pk = model._meta.pk.attname
        present = set(model.objects.filter(pk__in=[r[pk] for r in data[part]]).values_list('pk', flat=True))
        model.objects.bulk_create([model(**r) for r in data[part] if r[pk] not in present])

    rows = data['concepts']
    refs = [(r['vocabulary_id'], r['concept_code']) for r in rows]
    existing = local_concept_ids(refs)
    missing = [r for r, ref in zip(rows, refs) if ref not in existing]
    for row, concept_id in zip(missing, next_pk_batch(Concept, 'concept_id', len(missing))):
        Concept.objects.create(**{**row, 'concept_id': concept_id})
    for row, ref in zip(rows, refs):
        if ref in existing:
            Concept.objects.filter(concept_id=existing[ref]).update(
                **{k: v for k, v in row.items() if k != 'concept_id'},
            )
    stats._bump(stats.created, 'Concept (HealthKey)', len(missing))
    stats._bump(stats.updated, 'Concept (HealthKey)', len(rows) - len(missing))


def _merge(into: TransferStats, other: TransferStats) -> None:
    """Add other's counts and warnings to into."""
    for bucket in ('created', 'updated', 'deleted', 'skipped'):
        for table, n in getattr(other, bucket).items():
            into._bump(getattr(into, bucket), table, n)
    for message in other.warnings:
        into.warn(message)
    into.suppressed_warnings += other.suppressed_warnings
