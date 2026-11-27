"""Copy the field-curation tables from one PRomop instance to another.

The field-mapping screen is backed by six tables, and they only mean anything
together: a ``FieldConceptMapping`` names the OMOP concept a PatientRecord field
carries, ``FieldChoice``/``FieldChoiceCode`` bound its answers, ``FieldFormula``
computes it, ``FieldSynonym`` names it, a ``FieldDiseaseMembership`` says which
diseases' records it is part of, and a ``CustomPatientField`` is a field
that exists *only* because a mapping was approved for it. Moving a subset
produces a half-configured instance — a custom field whose mapping is missing
cannot be saved at all — so the default is to move all six.

``SourceCodeConceptMapping`` (the ``/code-mappings`` screen) is carried too, but
only when asked for: ingest reads it, so copying an approved row changes what a
later import resolves to, not just what the editor offers.

The transfer is split in two so it is testable without a second database:

* :func:`read_payload` turns one instance's curation tables into plain dicts.
* :func:`apply_payload` writes those dicts into the instance running the code.

Two things deliberately do not survive the trip:

* **Row IDs.** Rows are matched on their natural key (``field_name``,
  ``(field_name, display)`` for a choice, ``(field_name, disease)`` for a
  disease membership, and ``(organization_slug,
  source_vocabulary_id, source_code)`` for a code mapping), never on the source
  PK, because the two instances assign PKs independently.
* **User foreign keys.** ``reviewer`` and ``created_by`` point at ``Identity``
  rows whose IDs mean something different on each instance, so they are cleared
  rather than carried over — a wrong attribution is worse than none.

Concept FKs are re-resolved rather than copied. That is the one on a field
mapping and all three on a source-code mapping. ``concept_id`` is stable
across instances for anything loaded from Athena, but locally minted concepts
(``Concept.source == 'HealthKey'``) are numbered per instance, so the same id
can name a different concept here. ``(vocabulary_id, concept_code)`` is unique
and is the meaning, so it wins; the id is only a fallback for a mapping that
never recorded a code.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field as dataclass_field
from itertools import groupby

from django.db import transaction
from django.db.models import Case, CharField, F, Value, When
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from omop_core.services.field_mapping_provenance import CURATOR_PROVENANCE, portable_provenance
from omop_core.models import (
    Concept,
    CustomPatientField,
    FieldChoice,
    FieldChoiceCode,
    FieldConceptMapping,
    FieldDiseaseMembership,
    FieldFormula,
    FieldSynonym,
    Organization,
    SourceCodeConceptMapping,
)
from omop_core.services.source_vocabularies import VOCABULARY_OID_ALIASES

# Table keys, in dependency order: a CustomPatientField requires its mapping, so
# mappings are written first and pruned last.
TABLES = ('mappings', 'custom_fields', 'memberships', 'choices', 'formulas',
          'synonyms', 'code_mappings')
# The field-mapping page calls these two APIs.  The other curation tables are
# supported for explicit migrations, but copying them by default would broaden
# a field-mapping copy into unrelated configuration.
DEFAULT_TABLES = ('mappings', 'synonyms')
# code_mappings stays out of the default set: it is a different screen and it
# steers ingest. copy_reference_data asks for every table explicitly.

# Columns copied verbatim for each mapping. Excludes id, concept (re-resolved),
# reviewer (cleared), and the auto timestamps.
_MAPPING_FIELDS = (
    'provenance',
    'vocabulary_id', 'concept_code', 'unit', 'omop_table', 'source_value',
    'value_kind', 'type_concept_id', 'value_vocabulary', 'multiple',
    'status', 'reviewed_at', 'notes',
    'tab',
)
# Columns a payload written before they existed may omit. Provenance reads as
# blank: attribution does not cross instances. A placement is curation the
# target owns, so an absent ``tab`` is not carried: blank on create, left
# alone on update (the committed fixture predates the column, #2328).
_OPTIONAL_MAPPING_FIELDS = frozenset({'provenance', 'tab'})
_NOT_CARRIED_WHEN_ABSENT = frozenset({'tab'})
_CUSTOM_FIELD_FIELDS = ('display_name', 'tab', 'field_type', 'mode')
# A field's scope (#2331): one row per disease whose extension it is in.
_MEMBERSHIP_FIELDS = ('required', 'section', 'sort_order')

# Excludes id, the concept FKs (re-resolved), the user FKs (cleared), the auto
# timestamps, and the occurrence counters (see _apply_code_mappings).
_CODE_MAPPING_FIELDS = (
    'domain_id', 'source_code_description', 'umls_source_name',
    'suggestion_outcome', 'suggestion_model_version',
    'destination_vocabulary_id', 'omop_table', 'source', 'status', 'notes',
    'origin', 'origin_system', 'suggest_strategy', 'umls_cui', 'reviewed_at',
)
# Added after the payload format settled, so a payload written before them
# still applies: a missing key takes the model default.
_CODE_MAPPING_OPTIONAL_FIELDS: dict[str, object] = {
    # LOINC axis entries and the variant count variant routing is gated on.
    'loinc_axes': dict,
    'snomed_attributes': dict,
    'axis_variant_count': 0,
    # The last curator decision's surface and ranker confidence (#2382).
    'decision_surface': '',
    'decision_confidence': None,
    # Whether the stored suggestion is a ranker-unavailable fallback (#2743):
    # it travels with the suggestion and outcome it describes.
    'suggestion_fallback': False,
    # Whether Suggest approved the stored suggestion itself (#2856): travels
    # with the outcome that says whether a curator upheld it.
    'suggestion_auto_approved': False,
    # The score it was approved at, which the analytics bands read (#2856).
    'auto_approved_confidence': None,
    # Travels with the note it describes (#2423): the copy replaces the text,
    # so a payload without the key leaves no mark over the source's note.
    'curator_note_at': None,
    # The wearable metric category, measure class and metric key. See
    # _CODE_MAPPING_NOT_CARRIED_WHEN_ABSENT.
    'category': '',
    'measure_class': '',
    'metric_key': '',
}
# Curation a payload written before the column cannot speak for: blank on a
# row it creates, left as it is on a row that exists here.
_CODE_MAPPING_NOT_CARRIED_WHEN_ABSENT = frozenset({'category', 'measure_class', 'metric_key'})
_CODE_MAPPING_CONCEPT_FKS = (
    'source_concept', 'target_concept', 'suggested_target_concept',
)

# Spellings the code mapping tabs show as one vocabulary. Matching on the raw
# spelling copied ICD10:A41.9 next to an existing ICD10CM:A41.9.
_VOCABULARY_ALIASES: dict[str, str] = {**VOCABULARY_OID_ALIASES}

# A real source table is ~117k rows. Held as one list it does not fit in a
# 512Mi job, and neither does a dict of every local row to match against.
CODE_MAPPING_CHUNK: int = 2000

# One unresolvable concept per FK per row would be ~350k strings.
_WARNING_CAP: int = 100


@dataclass
class TransferStats:
    """What a run did, per table."""
    created: dict[str, int] = dataclass_field(default_factory=dict)
    updated: dict[str, int] = dataclass_field(default_factory=dict)
    deleted: dict[str, int] = dataclass_field(default_factory=dict)
    skipped: dict[str, int] = dataclass_field(default_factory=dict)
    warnings: list[str] = dataclass_field(default_factory=list)
    suppressed_warnings: int = 0
    # Prune needs the source keys, but streaming does not keep the rows.
    code_mapping_keys: set[tuple[str, str, str]] = dataclass_field(default_factory=set)
    # Target rows already written from a source row, so a later chunk leaves them alone.
    claimed_code_mappings: set[int] = dataclass_field(default_factory=set)

    def warn(self, message: str) -> None:
        """Keep the first _WARNING_CAP warnings and count the rest."""
        if len(self.warnings) < _WARNING_CAP:
            self.warnings.append(message)
        else:
            self.suppressed_warnings += 1

    def _bump(self, bucket: dict[str, int], table: str, n: int = 1) -> None:
        bucket[table] = bucket.get(table, 0) + n

    def total(self, bucket: dict[str, int]) -> int:
        return sum(bucket.values())


class _Rollback(Exception):
    """Raised to unwind the transaction after a --dry-run."""


def read_payload(
    using: str, tables: tuple[str, ...] = TABLES, stream: bool = False,
) -> dict:
    """Read the curation tables from ``using`` into JSON-safe dicts.

    Read-only: nothing is written to the source instance.

    With stream, code_mappings is a generator, so the source connection must
    stay open until it is consumed.
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
                # Like the reviewer it mirrors, an approver's name does not
                # travel: on the target it is just "curator" (#1719).
                'provenance': portable_provenance(m.provenance),
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
                # A removed field travels as removed, with its rejected mapping (#2920);
                # a payload written before this reads as "leave alone".
                'retired': c.retired_at is not None,
            }
            for c in CustomPatientField.objects.using(using)
            .select_related('mapping')
            .order_by('field_name')
        ]

    if 'memberships' in tables:
        payload['memberships'] = [
            {
                'field_name': m.field_name,
                'disease': m.disease,
                **{name: getattr(m, name) for name in _MEMBERSHIP_FIELDS},
                'provenance': _portable_membership_provenance(m.provenance),
            }
            for m in FieldDiseaseMembership.objects.using(using)
            .order_by('field_name', 'disease')
        ]
        # What the source can vouch for. A field's scope is replaced on the
        # target only for the fields and categories named here: a field or a
        # category the source has never heard of is left as the target has it.
        from omop_core.services.disease_profiles import category_codes, field_names
        payload['membership_source'] = {
            'fields': sorted(field_names(using)),
            'categories': sorted(category_codes(using)),
        }

    if 'choices' in tables:
        payload['choices'] = [
            {
                'field_name': fc.field_name,
                'display': fc.display,
                'sort_order': fc.sort_order,
                # Identity, scope and spellings (#1224). An older payload has
                # none of these and is read as one unscoped choice per display.
                'code': fc.code,
                'context_key': fc.context_key,
                'value': fc.value,
                'value_type': fc.value_type,
                'retired': fc.retired_at is not None,
                'aliases': sorted(
                    ({'alias': a.alias, 'source': a.source} for a in fc.aliases.all()),
                    key=lambda a: a['alias'].casefold()),
                'diseases': sorted(d.disease for d in fc.disease_scope.all()),
                # What the answer means and who decided it: only the decision in
                # force and the open proposal. Concepts travel as vocabulary and
                # code; the reviewer, ids and revision log stay on their instance.
                'value_mappings': [
                    {
                        'role': m.role, 'outcome': m.outcome, 'status': m.status,
                        'vocabulary_id': m.vocabulary_id, 'concept_code': m.concept_code,
                        'question_vocabulary_id': m.question_vocabulary_id,
                        'question_concept_code': m.question_concept_code,
                        'rationale': m.rationale, 'vocabulary_release': m.vocabulary_release,
                        'provenance': m.provenance,
                    }
                    for m in sorted(fc.value_mappings.all(), key=lambda m: (m.role, m.status))
                    if m.status in ('approved', 'proposed')
                ],
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
            .prefetch_related('codes', 'aliases', 'disease_scope', 'value_mappings')
            .order_by('field_name', 'context_key', 'sort_order', 'display')
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

    if 'code_mappings' in tables:
        rows = iter_code_mappings(using)
        # Only table big enough that materializing it can exhaust the job memory.
        payload['code_mappings'] = rows if stream else list(rows)

    return payload


def _portable_membership_provenance(value: str) -> str:
    """The seed's label travels; whoever set a scope by hand becomes ``curator``."""
    from omop_core.services.disease_profiles import SEED_PROVENANCE

    return value if value in ('', SEED_PROVENANCE) else CURATOR_PROVENANCE


def _person_labels(using: str) -> frozenset[str]:
    """Every way an approval on ``using`` can have named a person in origin_system.

    Code Mapping approval stamps ``_user_display(user)[:50]`` (#1708): the
    email, else ``str(identity)``, which is ``issuer|sub``. origin_system is
    otherwise free text from many importers, so people are recognised by
    matching this instance's identities rather than by an allowlist.
    """
    from patient_portal.models import Identity
    width = SourceCodeConceptMapping._meta.get_field('origin_system').max_length
    labels = set()
    for email, name, issuer, sub in Identity.objects.using(using).values_list('email', 'name', 'issuer', 'sub'):
        labels.update(value[:width] for value in (email, name, f'{issuer}|{sub}') if value)
    return frozenset(labels)


def _code_mapping_row(m: SourceCodeConceptMapping, people: frozenset[str] = frozenset()) -> dict[str, object]:
    """One source-code mapping as a JSON-safe dict."""
    origin = m.origin_system or ''
    return {
        'organization_slug': m.organization.slug if m.organization_id else '',
        'source_vocabulary_id': m.source_vocabulary_id,
        'source_code': m.source_code,
        **{
            fk: {
                'vocabulary_id': c.vocabulary_id if c else '',
                'concept_code': c.concept_code if c else '',
                'concept_id': getattr(m, f'{fk}_id'),
            }
            for fk in _CODE_MAPPING_CONCEPT_FKS
            for c in (getattr(m, fk),)
        },
        **{name: getattr(m, name) for name in _CODE_MAPPING_FIELDS},
        **{name: getattr(m, name) for name in _CODE_MAPPING_OPTIONAL_FIELDS},
        # Approval stamps the approver as origin_system (#1708). Like the
        # cleared reviewer it names a person on this instance only (#1719).
        'origin_system': CURATOR_PROVENANCE if '@' in origin or origin in people else m.origin_system,
    }


def _code_key(vocabulary_id: str, source_code: str, organization_slug: str = '') -> tuple[str, str, str]:
    """The code a mapping is about, whatever spelling of its vocabulary it uses."""
    return (
        organization_slug,
        _VOCABULARY_ALIASES.get(vocabulary_id, vocabulary_id),
        source_code.strip(' ').lower(),
    )


def _canonical_vocabulary() -> Case:
    """SQL version of the vocabulary part of _code_key."""
    return Case(
        *(When(source_vocabulary_id=alias, then=Value(target)) for alias, target in _VOCABULARY_ALIASES.items()),
        default=F('source_vocabulary_id'), output_field=CharField(),
    )


def iter_code_mappings(using: str) -> Iterator[dict[str, object]]:
    """Yield source-code mapping rows without materializing the table."""
    queryset = (
        SourceCodeConceptMapping.objects.using(using)
        .select_related('organization', *_CODE_MAPPING_CONCEPT_FKS)
        # Rows for one code stay next to each other, so a chunk rarely splits them.
        .order_by(_canonical_vocabulary(), Lower(Trim('source_code')), 'source_vocabulary_id')
    )
    people = _person_labels(using)
    for m in queryset.iterator(chunk_size=CODE_MAPPING_CHUNK):
        yield _code_mapping_row(m, people)


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

    One table is not additive without ``prune``: a field's disease scope
    (``memberships``) is a single value, so it is replaced from the source for
    the fields and categories the payload's ``membership_source`` names. See
    :func:`_apply_memberships`.
    """
    stats = TransferStats()
    try:
        with transaction.atomic():
            if 'mappings' in tables:
                _apply_mappings(payload.get('mappings', []), stats)
            if 'custom_fields' in tables:
                _apply_custom_fields(payload.get('custom_fields', []), stats)
            # Only a payload that carries the table can say what a field's
            # scope is: one written before memberships existed (the committed
            # fixture) changes none.
            if 'memberships' in tables and 'memberships' in payload:
                _apply_memberships(payload, stats)
            if 'choices' in tables:
                _apply_choices(payload.get('choices', []), stats)
            if 'formulas' in tables:
                _apply_formulas(payload.get('formulas', []), stats)
            if 'synonyms' in tables:
                _apply_synonyms(payload.get('synonyms', []), stats)
            if 'code_mappings' in tables:
                _apply_code_mappings(payload.get('code_mappings', []), stats)
            if prune:
                _prune(payload, tables, stats)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    return stats


# ── Concept resolution ────────────────────────────────────────────────────

def _resolve_concept_ref(
    vocab: str, code: str, concept_id: int | None, label: str,
    stats: TransferStats,
    by_code: dict[tuple[str, str], Concept] | None = None,
    by_id: dict[int, Concept] | None = None,
    allow_id_fallback: bool = True,
) -> Concept | None:
    """Find the local Concept named by (vocab, code), else by concept_id.

    Returns None instead of the source id because a code mapping's concept FKs
    are db_constraint=False. A stale id would be accepted and would name a
    different concept, which ingest then acts on.
    """
    if vocab and code:
        # Without caches this is one query per reference.
        if by_code is not None:
            concept = by_code.get((vocab, code))
        else:
            concept = Concept.objects.filter(
                vocabulary_id=vocab, concept_code=code
            ).first()
        if concept is not None:
            return concept
    elif concept_id is not None and allow_id_fallback:
        # Old rows may predate the denormalized vocabulary/code columns.  An ID
        # is safe only when there is no semantic identifier to resolve: if a
        # supplied code is absent locally, the same numeric ID may name a
        # completely different locally-created concept on this instance.
        if by_id is not None:
            concept = by_id.get(concept_id)
        else:
            concept = Concept.objects.filter(concept_id=concept_id).first()
        if concept is not None:
            return concept

    if concept_id is not None or (vocab and code):
        stats.warn(
            f"{label}: concept {vocab}:{code or concept_id} is not "
            f"loaded on this instance — mapping copied with no concept."
        )
    return None


def _resolve_concept(row: dict, stats: TransferStats) -> Concept | None:
    """The concept a field mapping refers to."""
    return _resolve_concept_ref(
        row.get('concept_vocabulary_id') or row.get('vocabulary_id') or '',
        row.get('concept_code_resolved') or row.get('concept_code') or '',
        row.get('concept_id'),
        row['field_name'],
        stats,
    )


# ── Per-table application ─────────────────────────────────────────────────

def _apply_mappings(rows: list[dict], stats: TransferStats) -> None:
    existing = {m.field_name: m for m in FieldConceptMapping.objects.all()}
    for row in rows:
        values = {name: row.get(name, '') if name in _OPTIONAL_MAPPING_FIELDS else row[name]
                  for name in _MAPPING_FIELDS}
        values['concept'] = _resolve_concept(row, stats)
        # Attribution does not cross instances, see module docstring.
        values['reviewer'] = None

        mapping = existing.get(row['field_name'])
        if mapping is None:
            FieldConceptMapping.objects.create(field_name=row['field_name'], **values)
            stats._bump(stats.created, 'mappings')
            continue
        for name in _NOT_CARRIED_WHEN_ABSENT - set(row):
            values.pop(name)
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
            stats.warn(
                f"custom field {row['field_name']}: mapping "
                f"{row['mapping_field_name']} is not present — skipped."
            )
            stats._bump(stats.skipped, 'custom_fields')
            continue
        values = {name: row[name] for name in _CUSTOM_FIELD_FIELDS}
        values['mapping'] = mapping
        values['created_by'] = None
        custom = existing.get(row['field_name'])
        if 'retired' in row:
            was_retired = custom is not None and custom.retired_at is not None
            values['retired_at'] = (
                (custom.retired_at if was_retired else timezone.now()) if row['retired'] else None)
        if custom is None:
            CustomPatientField.objects.create(field_name=row['field_name'], **values)
            stats._bump(stats.created, 'custom_fields')
            continue
        for name, value in values.items():
            setattr(custom, name, value)
        custom.save(update_fields=[*values, 'updated_at'])
        stats._bump(stats.updated, 'custom_fields')


def _apply_memberships(payload: dict, stats: TransferStats) -> None:
    """Make each field's scope here what it is at the source.

    A scope is one value, the set of diseases, and narrowing it or making a
    field base is done by removing rows. Adding the source's rows to the
    target's would leave a union neither curator chose, so the set is
    replaced, but only as far as the source can vouch for it. The payload's
    ``membership_source`` names the fields and the categories the source
    knows; a row here is removed when the source knows both its field and its
    disease and does not have it. A field created only on this instance, a
    category added only here, and a payload without ``membership_source``
    (written by hand, or before the key existed) remove nothing.

    A membership names a Disease Category by code. copy_reference_data copies
    the categories first; a payload loaded on its own may name one this
    instance does not have. That row is skipped, and the field's scope here
    is then left exactly as it is: skipping the row and replacing the rest
    would make a disease-specific field base. A row for a field that does not
    exist here is skipped too.
    """
    from omop_core.services.disease_profiles import extension_codes, field_names

    known = set(extension_codes())
    # A removed field keeps its scope here too, so a restore on this instance is right.
    fields = field_names(include_retired=True)
    source = payload.get('membership_source') or {}
    source_fields = set(source.get('fields', ()))
    source_categories = set(source.get('categories', ()))

    wanted: dict[str, dict[str, dict]] = defaultdict(dict)
    unresolved: set[str] = set()
    for row in payload['memberships']:
        field_name, disease = row['field_name'], row['disease']
        if field_name not in fields:
            stats.warn(f'membership {field_name}: no such field on this instance; skipped.')
        elif disease not in known:
            unresolved.add(field_name)
            stats.warn(
                f'membership {field_name}: disease category {disease!r} does not exist on '
                f'this instance; skipped, and the scope the field has here is left as it is.'
            )
        else:
            wanted[field_name][disease] = row
            continue
        stats._bump(stats.skipped, 'memberships')

    existing: dict[str, dict[str, FieldDiseaseMembership]] = defaultdict(dict)
    for membership in FieldDiseaseMembership.objects.all():
        existing[membership.field_name][membership.disease] = membership
    stale = [
        membership.pk
        for field_name, held in existing.items()
        if field_name in source_fields and field_name not in unresolved
        for disease, membership in held.items()
        if disease in source_categories and disease not in wanted.get(field_name, {})
    ]
    if stale:
        FieldDiseaseMembership.objects.filter(pk__in=stale).delete()
        stats._bump(stats.deleted, 'memberships', len(stale))

    for field_name, by_disease in wanted.items():
        if field_name in unresolved:
            continue
        for disease, row in by_disease.items():
            values = {name: row[name] for name in _MEMBERSHIP_FIELDS}
            values['provenance'] = row.get('provenance', '')
            # Attribution does not cross instances, see module docstring.
            values['created_by'] = None
            membership = existing[field_name].get(disease)
            if membership is None:
                FieldDiseaseMembership.objects.create(
                    field_name=field_name, disease=disease, **values)
                stats._bump(stats.created, 'memberships')
                continue
            for name, value in values.items():
                setattr(membership, name, value)
            membership.save(update_fields=[*values, 'updated_at'])
            stats._bump(stats.updated, 'memberships')


def _apply_choices(rows: list[dict], stats: TransferStats) -> None:
    from django.utils import timezone

    from omop_core.models import FieldChoiceAlias, FieldChoiceDisease
    from omop_core.services import disease_profiles, field_choices

    known = disease_profiles.category_codes() - {disease_profiles.OTHER}
    claimed = {(r['field_name'], r.get('context_key', ''), r['code'])
               for r in rows if r.get('code')}

    def local():
        by_code, by_display = {}, {}
        for c in FieldChoice.objects.all():
            by_code[(c.field_name, c.context_key, c.code)] = c
            by_display[(c.field_name, c.context_key, c.display)] = c
        return by_code, by_display

    def find(row):
        """The local choice a payload row is, or None for a new one.

        A choice is found by its stable code. One the code does not find is
        found by its display only when the payload has no code (it predates
        codes) or the local choice's own code is one the payload does not
        name: an instance that numbered a label that slugs alike differently
        still has the same choice, which then takes the payload's code.
        """
        context = row.get('context_key', '')
        if row.get('code'):
            hit = by_code.get((row['field_name'], context, row['code']))
            if hit is not None:
                return hit
        hit = by_display.get((row['field_name'], context, row['display']))
        if hit is None:
            return None
        if row.get('code') and (hit.field_name, hit.context_key, hit.code) in claimed:
            return None
        return hit

    by_code, by_display = local()
    # A source that renamed one choice and gave its old label to another, or
    # swapped two labels, would collide with the label rule midway. Move every
    # label that is about to change aside first; each is set again below.
    moving = []
    for row in rows:
        hit = find(row)
        if hit is not None and hit.display != row['display']:
            moving.append(hit)
    for choice in moving:
        choice.display = f'~moving~{choice.pk}'
        choice.save(update_fields=['display'])
    if moving:
        by_code, by_display = local()

    for row in rows:
        context = row.get('context_key', '')
        choice = find(row)
        value_type = row.get('value_type', 'text')
        try:
            value = field_choices.clean_value(value_type, row.get('value', ''))
        except field_choices.ChoiceError as exc:
            stats.warnings.append(f'choice {row["field_name"]}/{row["display"]}: {exc}')
            value, value_type = '', 'text'
        if choice is None:
            choice = FieldChoice.objects.create(
                field_name=row['field_name'],
                display=row['display'],
                sort_order=row['sort_order'],
                code=row.get('code', ''),
                context_key=context,
                value=value,
                value_type=value_type,
                retired_at=timezone.now() if row.get('retired') else None,
                created_by=None,
            )
            stats._bump(stats.created, 'choices')
        else:
            choice.display = row['display']
            choice.sort_order = row['sort_order']
            if row.get('code') and choice.code != row['code'] and (
                    choice.field_name, choice.context_key, row['code']) not in by_code:
                choice.code = row['code']
            if 'value' in row:
                choice.value = value
                choice.value_type = value_type
                if row.get('retired'):
                    # Keep when it was retired, not when it was last copied.
                    choice.retired_at = choice.retired_at or timezone.now()
                else:
                    choice.retired_at = None
            choice.created_by = None
            choice.save()
            stats._bump(stats.updated, 'choices')
        by_code[(choice.field_name, choice.context_key, choice.code)] = choice
        by_display[(choice.field_name, choice.context_key, choice.display)] = choice
        # Codes, spellings and scope are pure data hanging off the choice, so
        # the source's set replaces the local one wholesale rather than being
        # merged. A payload from before #1224 names none and leaves them be.
        choice.codes.all().delete()
        FieldChoiceCode.objects.bulk_create([
            FieldChoiceCode(choice=choice, **code) for code in row['codes']
        ])
        if 'aliases' in row:
            choice.aliases.all().delete()
            FieldChoiceAlias.objects.bulk_create([
                FieldChoiceAlias(
                    choice=choice, field_name=choice.field_name,
                    context_key=choice.context_key, alias=a['alias'],
                    alias_key=field_choices.fold(a['alias']),
                    source=a.get('source', 'curator'))
                for a in row['aliases']
            ], ignore_conflicts=True)
        if 'diseases' in row:
            choice.disease_scope.all().delete()
            # A category this instance does not have cannot scope anything here.
            FieldChoiceDisease.objects.bulk_create([
                FieldChoiceDisease(choice=choice, disease=d)
                for d in sorted(set(row['diseases'])) if d in known
            ])
        if 'value_mappings' in row:
            _apply_value_mappings(choice, row['value_mappings'], stats)


def _apply_value_mappings(choice, rows: list[dict], stats: TransferStats) -> None:
    """Make the choice's current answer mappings the source's.

    A concept is found again by vocabulary and code, never by the source's id.
    An approval whose concept this instance has not loaded, or that is not a
    valid target here, cannot be approved here, so the decision already in
    force locally is left as it is and the source's answer is kept as an
    unresolved proposal (unless the source also has an open proposal for the
    role, which then wins), with a warning. A decision the source no longer has
    in force is superseded (approved) or rejected (proposed) rather than
    deleted, so its revisions stay. Reviewers are not copied; the same account
    id is another person on another instance, and a transferred row is
    attributed to the transfer, with no review date.
    """
    from omop_core.models import FieldValueConceptMapping as Mapping
    from omop_core.services import field_value_mappings as fvm

    label = 'transfer'
    where = f'value mapping {choice.field_name}/{choice.display}'
    wanted: dict[tuple[str, str], dict] = {}
    for row in rows:
        role, outcome = row.get('role'), row.get('outcome')
        if (role not in fvm.ROLES or outcome not in fvm.OUTCOMES
                or row.get('status') not in ('approved', 'proposed')):
            stats.warnings.append(f'{where}: skipped a row with an unknown role, outcome or status.')
            continue
        if outcome == 'no_equivalent' and row.get('concept_code'):
            stats.warnings.append(f'{where}: skipped a "no equivalent" row that names a concept.')
            continue
        if role != 'context_recipe' and row.get('question_concept_code'):
            stats.warnings.append(f'{where}: skipped a row with a question outside a recipe.')
            continue
        wanted[(role, row['status'])] = row

    ctx = fvm.CheckContext()
    plan = []
    blocked_approvals = set()
    for (role, status), row in sorted(wanted.items()):
        target = question = None
        resolution = ''
        if row['outcome'] == 'mapped':
            target = fvm.lookup_concept(vocabulary_id=row.get('vocabulary_id', ''),
                                        concept_code=row.get('concept_code', ''))
            problem = fvm.target_problem(target, role, choice.field_name, ctx=ctx)
            if problem:
                resolution = 'unresolved' if target is None else 'invalid'
                stats.warnings.append(f'{where}: {problem}')
                target = None
            if role == 'context_recipe':
                question = fvm.lookup_concept(vocabulary_id=row.get('question_vocabulary_id', ''),
                                              concept_code=row.get('question_concept_code', ''))
                if fvm.target_problem(question, role, choice.field_name, question=True, ctx=ctx):
                    question = None
                    resolution = resolution or 'unresolved'
        if status == 'approved' and row['outcome'] == 'mapped' and (
                target is None or (role == 'context_recipe' and question is None)):
            blocked_approvals.add(role)
            if (role, 'proposed') in wanted:
                continue                      # the source's own open proposal holds the place
            status = 'proposed'
        plan.append((role, status, row, target, question, resolution))

    applied = set()
    for role, status, row, target, question, resolution in plan:
        applied.add((role, status))
        fields = dict(
            outcome=row['outcome'], target_concept=target,
            vocabulary_id=row.get('vocabulary_id', ''), concept_code=row.get('concept_code', ''),
            question_concept=question,
            question_vocabulary_id=row.get('question_vocabulary_id', ''),
            question_concept_code=row.get('question_concept_code', ''),
            source_resolution=resolution, rationale=row.get('rationale', ''),
            vocabulary_release=(fvm.release_of(target.vocabulary_id) if target else ''),
            provenance=(row.get('provenance', '') or label)[:100],
            reviewer=None, reviewed_at=None, proposed_by=None,
        )
        existing = Mapping.objects.filter(choice=choice, role=role, status=status).first()
        if existing is None:
            if status == 'approved':
                for old in Mapping.objects.filter(choice=choice, role=role, status='approved'):
                    old.status = 'superseded'
                    old.save(update_fields=['status', 'updated_at'])
                    fvm._record(old, 'superseded', None, label)
            existing = Mapping.objects.create(choice=choice, role=role, status=status, **fields)
        else:
            for name, value in fields.items():
                setattr(existing, name, value)
            existing.save()
        fvm._record(existing, 'transferred', None, label)
    # What the source no longer has in force. A local approval an incoming one
    # could not replace here stays in force.
    for old in Mapping.objects.filter(choice=choice, status__in=('approved', 'proposed')):
        key = (old.role, old.status)
        if key in applied or (old.status == 'approved' and old.role in blocked_approvals):
            continue
        old.status = 'superseded' if old.status == 'approved' else 'rejected'
        old.save(update_fields=['status', 'updated_at'])
        fvm._record(old, 'transferred', None, label)


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


def _apply_code_mappings(
    rows: Iterable[dict[str, object]], stats: TransferStats,
) -> None:
    """Sync source-code mappings by code, whatever vocabulary spelling each side uses.

    The source is the truth: for each code the rows here are updated, created or
    deleted until they match the source rows. Blank vocabulary is a real value,
    an uncoded lab name, so those rows key correctly instead of colliding.

    occurrence_count, first_seen and last_seen are not copied. They count this
    instance own ingest traffic, so the source numbers would misattribute it.
    """
    for chunk in _chunked_by_code(rows, CODE_MAPPING_CHUNK):
        _apply_code_mapping_chunk(chunk, stats)


def _chunked_by_code(
    rows: Iterable[dict[str, object]], size: int,
) -> Iterator[list[dict[str, object]]]:
    """Chunks of about size rows that never split the rows of one code.

    A split code would have its other spellings deleted by one chunk and
    recreated by the next.
    """
    chunk: list[dict[str, object]] = []
    for row in rows:
        key = _code_key(row['source_vocabulary_id'], row['source_code'], row.get('organization_slug') or '')
        if len(chunk) >= size and _code_key(
            chunk[-1]['source_vocabulary_id'], chunk[-1]['source_code'],
            chunk[-1].get('organization_slug') or '',
        ) != key:
            yield chunk
            chunk = []
        chunk.append(row)
    if chunk:
        yield chunk


def _concept_caches(
    rows: list[dict[str, object]],
) -> tuple[dict[tuple[str, str], Concept], dict[int, Concept]]:
    """Resolve every concept a chunk refers to in two queries, not two per row.

    The code lookup is one superset SELECT over the chunk vocabularies and
    codes. Extra pairs it returns are never read.
    """
    pairs: set[tuple[str, str]] = set()
    ids: set[int] = set()
    for row in rows:
        for fk in _CODE_MAPPING_CONCEPT_FKS:
            ref = row.get(fk) or {}
            vocab, code = ref.get('vocabulary_id') or '', ref.get('concept_code') or ''
            if vocab and code:
                pairs.add((vocab, code))
            elif ref.get('concept_id') is not None:
                ids.add(ref['concept_id'])

    by_code: dict[tuple[str, str], Concept] = {}
    if pairs:
        by_code = {
            (c.vocabulary_id, c.concept_code): c
            for c in Concept.objects.filter(
                vocabulary_id__in={v for v, _ in pairs},
                concept_code__in={c for _, c in pairs},
            )
        }
    by_id: dict[int, Concept] = {}
    if ids:
        by_id = {c.concept_id: c for c in Concept.objects.filter(concept_id__in=ids)}
    return by_code, by_id


def _apply_code_mapping_chunk(
    rows: list[dict[str, object]], stats: TransferStats,
) -> None:
    """Sync one chunk: per code update what is here, create what is missing, delete extras."""
    stats.code_mapping_keys.update(
        (row.get('organization_slug') or '', row['source_vocabulary_id'], row['source_code'])
        for row in rows
    )
    targets = _target_code_mappings(rows, stats)
    by_code, by_id = _concept_caches(rows)
    organization_slugs = {row.get('organization_slug') or '' for row in rows} - {''}
    organizations = {
        organization.slug: organization
        for organization in Organization.objects.filter(slug__in=organization_slugs)
    }
    rows = sorted(rows, key=lambda r: _code_key(
        r['source_vocabulary_id'], r['source_code'], r.get('organization_slug') or ''))
    for key, group in groupby(rows, key=lambda r: _code_key(
            r['source_vocabulary_id'], r['source_code'], r.get('organization_slug') or '')):
        group = list(group)
        here = targets.get(key, [])
        exact = {(m.source_vocabulary_id, m.source_code): m for m in here}
        paired = {id(row): exact.pop((row['source_vocabulary_id'], row['source_code']), None) for row in group}
        spare = sorted(exact.values(), key=lambda m: m.pk)
        for row in group:
            if paired[id(row)] is None and spare:
                paired[id(row)] = spare.pop(0)
        # Deleted first: a paired row may take the spelling of an extra.
        if spare:
            SourceCodeConceptMapping.objects.filter(pk__in=[m.pk for m in spare]).delete()
            stats._bump(stats.deleted, 'code_mappings', len(spare))
        for row in group:
            _write_code_mapping(
                row, paired[id(row)], stats, by_code, by_id, organizations,
            )


def _target_code_mappings(
    rows: list[dict[str, object]], stats: TransferStats,
) -> dict[tuple[str, str, str], list[SourceCodeConceptMapping]]:
    """Rows here with the same codes as the chunk, under any spelling, not yet claimed."""
    keys = {
        _code_key(row['source_vocabulary_id'], row['source_code'], row.get('organization_slug') or '')
        for row in rows
    }
    canonical = {vocabulary for _, vocabulary, _ in keys}
    spellings = canonical | {alias for alias, target in _VOCABULARY_ALIASES.items() if target in canonical}
    found: dict[tuple[str, str, str], list[SourceCodeConceptMapping]] = defaultdict(list)
    candidates = SourceCodeConceptMapping.objects.select_related('organization').filter(source_vocabulary_id__in=spellings).alias(
        code=Lower(Trim('source_code')),
    ).filter(code__in={code for _, _, code in keys}).exclude(pk__in=stats.claimed_code_mappings)
    for mapping in candidates:
        key = _code_key(
            mapping.source_vocabulary_id, mapping.source_code,
            mapping.organization.slug if mapping.organization_id else '',
        )
        if key in keys:
            found[key].append(mapping)
    return found


def _write_code_mapping(
    row: dict[str, object], mapping: SourceCodeConceptMapping | None, stats: TransferStats,
    by_code: dict[tuple[str, str], Concept], by_id: dict[int, Concept],
    organizations: dict[str, Organization],
) -> None:
    """Create the row, or overwrite mapping with it, spelling included."""
    label = f"{row['source_vocabulary_id'] or '(uncoded)'}:{row['source_code']}"
    values = {name: row[name] for name in _CODE_MAPPING_FIELDS}
    for name, default in _CODE_MAPPING_OPTIONAL_FIELDS.items():
        if (mapping is not None and name not in row
                and name in _CODE_MAPPING_NOT_CARRIED_WHEN_ABSENT):
            continue
        values[name] = row.get(name, default() if callable(default) else default)
    organization_slug = row.get('organization_slug') or ''
    if organization_slug:
        values['organization'] = organizations.get(organization_slug)
        if values['organization'] is None:
            stats.warn(f'{label}: organization {organization_slug!r} does not exist; skipped')
            stats._bump(stats.skipped, 'code_mappings')
            return
    else:
        values['organization'] = None
    for fk in _CODE_MAPPING_CONCEPT_FKS:
        ref = row.get(fk) or {}
        values[fk] = _resolve_concept_ref(
            ref.get('vocabulary_id') or '', ref.get('concept_code') or '', ref.get('concept_id'),
            f'{label} ({fk})', stats, by_code, by_id,
            # The source concept is select_related, so a missing one means
            # the source FK dangles. Its id names nothing we can trust.
            allow_id_fallback=False,
        )
    # Attribution does not cross instances, see module docstring.
    values.update(reviewer=None, created_by=None, updated_by=None,
                  source_vocabulary_id=row['source_vocabulary_id'], source_code=row['source_code'])
    if mapping is None:
        mapping = SourceCodeConceptMapping.objects.create(**values)
        stats._bump(stats.created, 'code_mappings')
    else:
        for name, value in values.items():
            setattr(mapping, name, value)
        mapping.save(update_fields=[*values, 'updated_at'])
        stats._bump(stats.updated, 'code_mappings')
    stats.claimed_code_mappings.add(mapping.pk)


def _prune(payload: dict, tables: tuple[str, ...], stats: TransferStats) -> None:
    """Delete local rows the source does not have.

    Custom fields go before mappings: the FK is PROTECT, so a mapping that only
    exists to back a pruned custom field cannot be deleted before it.
    """
    if 'custom_fields' in tables:
        keep = {row['field_name'] for row in payload.get('custom_fields', [])}
        gone = list(CustomPatientField.objects.exclude(field_name__in=keep)
                    .values_list('field_name', flat=True))
        # Memberships are keyed by name: left behind, a field created later
        # under the same name would inherit a scope nobody gave it.
        FieldDiseaseMembership.objects.filter(field_name__in=gone).delete()
        deleted, _ = CustomPatientField.objects.filter(field_name__in=gone).delete()
        stats._bump(stats.deleted, 'custom_fields', deleted)

    if 'mappings' in tables:
        keep = {row['field_name'] for row in payload.get('mappings', [])}
        deleted, _ = FieldConceptMapping.objects.exclude(field_name__in=keep).delete()
        stats._bump(stats.deleted, 'mappings', deleted)

    if 'choices' in tables:
        rows = payload.get('choices', [])
        keep_code = {(r['field_name'], r.get('context_key', ''), r['code'])
                     for r in rows if r.get('code')}
        keep_display = {(r['field_name'], r.get('context_key', ''), r['display']) for r in rows}
        stale = [c.pk for c in FieldChoice.objects.all()
                 if (c.field_name, c.context_key, c.code) not in keep_code
                 and (c.field_name, c.context_key, c.display) not in keep_display]
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

    if 'code_mappings' in tables:
        # Apply gathers these because the payload rows may be a spent generator.
        keep = stats.code_mapping_keys
        stale = [
            pk for pk, org_slug, vocab, code in SourceCodeConceptMapping.objects
            .values_list('pk', 'organization__slug', 'source_vocabulary_id', 'source_code')
            .iterator(chunk_size=CODE_MAPPING_CHUNK)
            if (org_slug or '', vocab, code) not in keep
        ]
        if stale:
            # Approved rows steer ingest, so this is a live behaviour change.
            SourceCodeConceptMapping.objects.filter(pk__in=stale).delete()
            stats._bump(stats.deleted, 'code_mappings', len(stale))
