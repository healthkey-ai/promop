# ADR 0001 — PRomop is the vocabulary source of truth

**Status:** Proposed cross-repository policy (originally 2026-07-22; revised
2026-09-14). The release-pinned mirror direction records the decision in
[#337](https://github.com/healthkey-ai/promop/issues/337); shared governance and
consumer conformance are separate from implementation of the PRomop APIs.
**Deciders:** PRomop, EXACT, CancerBot and SoC maintainers.
**Supersession scope:** the earlier EXACT/SoC cross-vocabulary mapping proposals;
consumer-side acknowledgement is tracked in the active plan.

This ADR owns vocabulary authority, distribution and interpretation rules.
Delivery status, outstanding work, issue ownership and acceptance gates live in
[the field/value mapping plan](../../field_concept_mapping_plan.md#vocabulary-distribution-and-mapping-provenance).
The [consumer protocol](../vocab-consumer-cache-protocol.md) owns the HTTP and
checksum contract.

## Context

Independent vocabulary artifacts and hand-maintained concept dictionaries drift.
Consumers need a coherent vocabulary generation for an operation, including its
concepts, relationships and versions. A collection of independently cached query
results does not establish that coherence.

Vocabulary content, curated mappings and source-owned clinical catalogs also have
different identities. Publishing Athena data does not approve a field answer,
validate a genomic interpretation or replace an application's source value set.

## Decision

1. PRomop is the canonical publisher of the **declared loaded vocabulary corpus**
   used by these applications: concepts, relationships, synonyms, ancestry, drug
   strengths, source-to-concept maps and vocabulary metadata. Authority covers
   the published scope; it does not claim every external ontology is loaded.
2. Consumers synchronize through PRomop's **release-pinned vocabulary mirrors**.
   They may replicate the tables required by their declared workload, preserving
   the dependencies needed for complete resolution. A mirror is a read-only
   derivative of PRomop's publication, not an independently curated vocabulary.
3. The concept/search/lookup/graph APIs remain available for queries and curation.
   Ad hoc API-result caching is not the consumer synchronization contract.
4. PRomop owns patient-side vocabulary-derived projections. Consumers use the
   published patient identifiers with their provenance; they do not silently
   reinterpret an asserted regimen or recompute its classes under another release.
5. Retain source identity and unresolved values. Concept availability, mapping
   approval and clinical evidence are distinct requirements.

## Access mechanism: release-pinned mirror

A consumer builds an immutable local generation from one manifest and verifies
stream completion, row counts and supported content checksums before activation.
Use a single coordinated sync writer and atomically switch the active-generation
pointer only after every required table verifies. Read that pointer once per
operation; all reads in that operation use the pinned generation. Keep a verified
last-known-good generation according to an explicit staleness policy.

Cross-artifact computations must verify compatible release identities: patient
projections, trial mappings and the consumer's vocabulary generation cannot be
silently combined across releases. Missing, stale, mismatched or incomplete
required evidence yields an explicit unknown/failure, never a dropped eligibility
requirement or a weakened exclusion. Record release identity and age on decisions.

The implemented [protocol](../vocab-consumer-cache-protocol.md) provides manifest
list/detail/latest APIs, ETags and checksummed NDJSON table downloads. Snapshot
URLs accept only the latest published release and return `409` for an older one.
Historical manifests remain addressable. Downloads read **current live tables**;
they are not retained point-in-time row snapshots. A release ETag identifies a
manifest, not a fresh digest of current rows. Verify downloaded bytes and retry
or obtain a consistent publication on mismatch; never activate mismatched data.

The current `VocabularyRelease` identity is a database primary key; therapy
provenance serializes it as a string. It is not a globally content-addressed ID.
Concept/graph responses have release-based ETags, which do not by themselves
provide historical query pinning or a transactionally frozen graph. The loader
uses upserts and guarded replacement rather than `TRUNCATE ... CASCADE`.
Publication metadata exists, but that alone does not establish an atomic switch
of all live vocabulary tables. The active plan owns those remaining guarantees.

## Integrity / no poisoning

Licensed vocabulary identifiers must identify licensed vocabulary content.
Never mint local concepts inside HemOnc, LOINC, SNOMED or another external
namespace. Preserve local source codes in an explicit local namespace; report
missing, ambiguous, invalid or unsupported mappings for curation.

Validate vocabulary/code, validity, domain and the target's role before using a
concept. A field question, coded answer, whole assertion, drug classification and
OMOP provenance Type Concept are not interchangeable. Standard clinical fact
columns require an appropriate standard concept; classification use has its own
role-specific validation. Retain raw source identity when no suitable target
exists, using concept 0 or an unresolved/pending representation only where the
approved writer contract supports it.

Vocabulary release identity must be recorded separately from **mapping revision**
and **source catalog/recipe version**. A mapping can change without an Athena
refresh. An unchanged catalog can acquire a reviewed recipe correction. Neither
change permits silently rewriting historical clinical facts or curator decisions.

## The CB exception (temporary)

The `cb_code ↔ concept_id` bridge belongs to CancerBot's category semantics.
Any transition copy must identify its source key/version, vocabulary release,
reviewed mapping revision, status, owner and expiry. It is not permission to
curate a second vocabulary corpus.

Retire the bridge only when every active criterion has an approved replacement,
matching preserves requirements, exclusions, ambiguity and source-asserted
identity, and production reads/writes have ceased for at least two published
releases. Retain a versioned audit archive. HemOnc coverage alone cannot retire
non-drug modalities or categories without a lossless replacement.
[ADR 0002](0002-omop-therapy-types.md) defines the hybrid drug-class transition.

## Governance

PRomop owns publication and the canonical corpus. Source owners retain their
catalogs and authored clinical meaning; authorized curators review mappings, with
clinical review where semantics require it. Consumer maintainers own mirror
activation and conformance. Shared governance must specify reviewer authority,
publication cadence, compatibility and retirement policy before claiming full
cross-repository adoption.

Deprecation must distinguish retained, invalidated, uniquely replaced, ambiguous
and unmapped identifiers. Do not automatically replace an identifier merely
because a new release exists. Relationship changes also invalidate derived
expansions even when concept IDs remain unchanged.

## Relationship to the active plans

The [field/value plan](../../field_concept_mapping_plan.md) owns stable
scoped choices, reviewed answer mappings, question/answer roles, transfer and
reconciliation. Existing therapy catalogs remain authoritative source inventories
and are exposed through provider adapters; they are not recreated as flat enums.

The [Genomics architecture](../genomics_architecture.md) owns finding parents,
linked components and source-preserving storage. Its
[implementation plan](../genomics_implementation.md#fieldvalue-mapping-coordination)
owns recipe audits, catalog/value review and import provenance. This ADR neither
flattens findings into answer labels nor changes their storage model.
