# Versioned clinical source-input contract

`omop_core.services.clinical_input_contract` implements the narrow source-input
contract published in [PROMOP #1845](https://github.com/healthkey-ai/promop/pull/1845),
forward-ported onto the `dev` schema. `RECIPES` is the authoritative field/source/unit
reference. Ratios are laboratory-ULN inputs, not INR or seconds; the SDK neither
calculates nor substitutes them. Nullable projections supply no default swallowing
answer. This backend contract does not add frontend controls or new FHIR mappings.

## Availability and authority

SDK version is integer `2`; per-field availability is a separate contract.
`capabilities()` requires an exact compatible approved `FieldConceptMapping`,
the concept-zero sentinel and Patient self-report type vocabulary. It verifies
source, table, scalar answer shape, type and unit. Descriptors and curator
availability share the resolver; the writer re-resolves and locks mapping rows
inside its transaction. A caller descriptor can narrow access, never grant it.

There are no automatically approved seeds. Missing, proposed, revoked or
incompatible mappings fail closed without fallback to projection-only edits.
Legacy SCT `32817` remains available to existing native writes, not to the SDK's
concept-zero/self-report recipe. Custom approved SCT sources retain existing
curated priority; this contract defines no new cross-source medical policy.

Callers must supply an already-authorized Person. Native APIs retain tenant,
role, scope and ownership checks; a mapping decision grants no patient access.

## Append-only writes and readback

`write_clinical_inputs(person, changes)` validates raw commands before writing.
Explicit `False` is an answer; `None` appends a clear tombstone, never an inferred
negative. New SCT dates beyond `timezone.localdate()` are rejected. That limit
does not reject strictly decoded historical future dates on reads.

Corrections, equal-value retries and clears append distinct Observation facts.
They do not delete imports or upsert another same-day concept-zero input. The
writer locks the record, Person and mappings, checks that each appended fact
actually became the selected date/ID winner, refreshes the projection, and
verifies readback. An equal projected value alone cannot acknowledge a new fact
that lost source precedence. Validation or readback failure rolls back the batch.

The generic field applier dispatches these commands to the dedicated writer,
including clears, inside its transaction. The native serializer retains raw
type validation and suppresses unchanged full-autosave echoes without pending
intent. Those echoes neither require approval nor create self-report facts.
Historical future SCT echoes are normalized under read rules before they are
discarded; the future-date write restriction still applies to all remaining
commands, including an unchanged value with pending intent.
Body-measurement echoes retain existing units; actual edits keep the native unit
policy. Mixed edits preserve unrelated pending values.

## Refresh, pending intent and provenance

Snapshots retain raw source rows before generic clear filtering. The newest
date/ID wins for each source; strict provenance, units and answer-slot decoding
rejects malformed latest facts rather than resurrecting older results.
SCT tombstones mask older legacy-source dates while preserving their history.

Historical read compatibility is independent of current write approval. Only a
valid selected source answer or tombstone can resolve a pending clinical clear.
Curation does not backfill pending projection values as patient self-reports.
A new explicit command supersedes only its own pending intent; unrelated pending
edits remain pending. SCT projection and provenance share a pure source selector:
native baseline, strict SDK override, then the existing approved custom-source
priority. Provenance marks the actual table/row identity, not an equal or pending
projection value, and retains the winner within its 20-row history limit even
when an older custom source outranks newer canonical corrections. Exact custom
mapping history and canonical corrections/tombstones stay patient-scoped.

The SDK invokes full PatientRecord re-derivation, not a field-only refresh. It
does not add clinical formulas. Current `dev` IMWG/FLC pairing and therapy-history
semantics remain in force; the separate `cb-v2` therapy-attestation is not part
of this port. The existing refresh query budget remains 21.

## Schema boundary

`0310_cb_clinical_input_sources` depends on `0309_condition_context_mappings` and adds
exactly three nullable columns. Existing `sct_date` is not re-added. The migration
contains neither mapping approvals nor data backfill.

This migration targets the `dev` predecessor. It does not claim compatibility
with databases already migrated through the separate `cb-v2` attestation/clinical
chain; convergence of those histories needs a separate plan.

Delivery status, verification and rollout gates live in
[the delivery plan](cb_clinical_inputs_plan.md).
