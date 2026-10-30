# PatientRecord staging store — design plan

Status: design plan, no implementation. Parent: cancerbot-org/cancerbot#5523 and
#5590. Upstream issue: healthkey-ai/promop#2116.

## 1. Purpose

The staging store is a **durable transfer queue** for values that cannot be written
to `PatientRecord` at cutover:

- a disease-less patient's payload;
- a value that lost a conflict to a pending/newer target;
- a displaced target value;
- a value awaiting an approved resolution.

It is **not a clinical source**. The matcher, Trial Details and the profile API never
read it. It exists so no value is silently dropped and no bare `PatientRecord` is
created just to satisfy a link.

## 2. Non-goals

- no clinical formula, threshold or vocabulary;
- no `PatientInfo` read (the archive is not a source);
- no matcher read path;
- no automatic transfer of a losing value;
- no replacement for the OMOP projection or the PatientRecord-first writer.

## 3. Storage location

Recommended: `patient_portal` (application/cutover state), **not** `omop_core`
(OMOP CDM tables). The staging row references a person by value, like
`UserProfile.person_id`; it is not an OMOP entity and must not appear in the CDM
schema.

`patient_portal` already owns patient-facing application state and migrations; no new
app for one table. This is a decision to approve, not an open question.

## 4. Model

`PatientRecordStaging` (one row per field or line component, not one JSON blob per
patient):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | primary key |
| `batch_key` | text | client idempotency key for a create batch |
| `item_key` | text | stable within the batch: `<field>` or `<field>:<index>` |
| `group_key` | text, null | atomic group (e.g. one therapy line) |
| `payload_hash` | char(64) | canonical hash of the item payload |
| `subject_key` | text | stable CB identity: `user:<id>` or `patient_info:<id>` |
| `person_id` | bigint, null | PROMOP Person; null until CB links |
| `field` | text | manifest field name |
| `value` | jsonb | scalar/list/dict, with the manifest type |
| `kind` | text | `source_value` or `displaced_target` |
| `provenance` | jsonb | required, see §5 |
| `manifest_hash` | char(64) | reviewed allowlist that produced the value |
| `state` | text | see §6 |
| `destination_kind` | text, null | `patient_record_field` or `staging_item` |
| `destination_ref` | text, null | field name or staging item id |
| `winning_item_id` | UUID, null | for a displaced target: the item that won |
| `owner` | text | accountable role or service identity; required |
| `created_by` | text | authenticated creator; required |
| `target_version` | text, null | `PatientRecord.updated_at` + fingerprint the decision is bound to |
| `approval` | jsonb, null | see §7 |
| `transferred_at` | timestamptz, null | terminal success |
| `created_at` / `updated_at` | timestamptz | audit |

Constraints and indexes:

- unique `(batch_key, item_key)`;
- unique `content_hash` = `sha256(batch_key | item_key | payload_hash)` so a reused
  `batch_key` with a different payload is rejected, not merged;
- indexes: `(subject_key, state)`, `(person_id, state)`, `(state, updated_at)`,
  `(group_key)` where not null;
- `state` in the enum; `kind=displaced_target` requires
  `state=retained_not_for_transfer` and `winning_item_id`;
- `state=transferred` requires `transferred_at`;
- `state=approved_deletion` requires an approval with `decision=delete`;
- `awaiting_transfer` cannot be a `displaced_target`;
- `destination_kind` and `destination_ref` are set together or both null.

Provenance is a required JSON schema, not free text:

```json
{
  "source": "cb_patient_info | migration | staging",
  "source_record": "patient_info:<id>",
  "edited_at": "ISO-8601 or null",
  "revision": "audit/revision reference or null",
  "manifest_hash": "sha256",
  "captured_at": "ISO-8601"
}
```

Every state change also appends `PatientRecordStagingTransition`:
`(staging_id, from_state, to_state, actor, reason, approval_id, created_at)`.
The transition history is append-only and is the audit source; the current `state`
is a cache of the last transition.

## 5. Identity

- `subject_key` is the stable CB identity. `patient_info:<id>` is an opaque
  identifier; it is never dereferenced and never read as clinical data.
- `person_id` is nullable until CB links the identity. A transfer may create the
  Person only through the same service call that carries the disease and the rest of
  the payload; it must not create a bare Person/record first.
- A batch must contain exactly one `subject_key`; mixed-subject batches are rejected.
- `destination_ref` for a displaced target points at the winning item, so the audit
  shows both sides.

## 6. States and transitions

| State | Meaning | Allowed next |
| --- | --- | --- |
| `awaiting_transfer` | safe to transfer once the target is ready | `transferred`, `conflict`, `approved_deletion` |
| `conflict` | unresolved; blocks the cutover gate | `awaiting_transfer` (approved resolution), `retained_not_for_transfer`, `approved_deletion` |
| `retained_not_for_transfer` | a losing/displaced value kept for audit | `approved_deletion` |
| `approved_deletion` | approved removal; purge per retention | terminal |
| `transferred` | terminal success | terminal |

A `conflict → awaiting_transfer` resolution is bound to the target version/hash and
manifest hash it was decided against. If the target changes, the row returns to
`conflict`; the stale approval cannot authorize a write.

A losing value never moves to `awaiting_transfer` automatically. Only an explicit
approved resolution can do that.

Every transition out of `conflict` writes a `PatientRecordStagingTransition` and the
approval object in the same transaction.

## 7. Approval object

Common fields for **every** outcome:

```json
{
  "approver": "actor id (server-derived, not body-supplied)",
  "role": "clinical-data-owner | data-owner | privacy",
  "scope": "patient/field/cohort",
  "decision": "transfer | retain | delete",
  "reason": "why",
  "mode": "overwrite | chronology_override | none",
  "target_version": "updated_at:fingerprint",
  "manifest_hash": "sha256",
  "expires_at": "ISO-8601"
}
```

Additional requirements: retention location for `retain`; retention policy
reference and deletion action for `delete`. The migration author cannot approve
their own conflict; the creator (`created_by`) cannot be the approver. A role not
named in §8 cannot approve.

## 8. Authorization

Internal service endpoints; not patient-facing.

| Action | Allowed actor | Scope |
| --- | --- | --- |
| create batch | service token | `patient_record_staging:write` |
| list state | service token | `patient_record_staging:read` |
| transfer | service token | `patient_record_staging:write` |
| resolve transfer/retain | clinical-data-owner or data-owner | object-level, per §7 |
| resolve delete | data-owner **and** privacy | object-level, per §7 |
| purge | data-owner + privacy | retention job |

Rules:

- The approver identity is derived from the authenticated request, never from the
  body. The creator cannot approve.
- The service token may act only for the `subject_key` it names; `person_id` must
  match that subject's link. A token cannot read another subject's values.
- Non-owner responses are redacted: state, field, repair action only; no `value`,
  `provenance` or `approval` payload.
- In-process calls (CB embeds PROMOP per ADR 0001) use the same service and audit;
  they do not bypass authorization or the transition history.
- `service_tokens.ALLOWED_SCOPES` must be extended explicitly; generic staff
  permission is not clinical/data authority.

## 9. Transfer algorithm

`transfer(batch_key, *, expected_target_version, actor)`:

1. Authorize the actor and load the batch. Reject mixed `subject_key`; resolve the
   Person/record once.
2. Lock all staging items in `id` order, then lock the `PatientRecord`/`Person`
   (sorted when a batch spans more than one target). No path may take the record
   lock before the staging lock.
3. Re-run the **full decision evaluator** under the lock for every item: target
   presence, pending/clear, provenance comparison, explicit-clear evidence,
   chronology, group atomicity. A version match alone is not authority to
   overwrite.
4. If the stored decision's `target_version`/`manifest_hash` does not match the
   current target, return to `conflict`. If an approval exists, verify decision,
   scope, expiry, target version and manifest hash even when the row is
   `awaiting_transfer`; an expired approval is a `conflict`.
5. For a disease-less batch: create the Person/record only in this transaction and
   only when a valid disease item is part of the transferred group. A disease value
   that is skipped, conflicting or losing does not authorize record creation.
6. Apply the group atomically through the shared PatientRecord writer (validation,
   `user_edited_fields`, source authorship, per-field savepoints). Do not use a raw
   `setattr` loop and do not call `refresh_patient_record`.
7. Write displaced targets as `retained_not_for_transfer` rows with
   `winning_item_id`, in the same transaction.
8. Mark `transferred` and append transitions in the same transaction as the record
   write. A crash leaves either both or neither.
9. Project mapped fields best-effort per field with `project_single_value`; a
   failure keeps the record edit and a pending projection. A durable worker owns
   retries after `transferred`; the transfer itself is not retried for projection.
10. A repeated call is a no-op that returns the current state.

## 10. Concurrency

- `batch_key`/`content_hash` make create idempotent; a duplicate key with a changed
  payload is rejected.
- Transfer claims items with `select_for_update`; a concurrent transfer sees
  `transferred` and no-ops.
- Creation race: the existing `/me/` path uses `get_or_create`; the transfer service
  must re-check the record after taking the Person/record lock and must not assume
  it is the only creator. The unique `content_hash` and `(batch_key, item_key)` are
  the database arbitration.
- Lock order: staging items → `PatientRecord`/`Person`. CB's repair holds
  `UserProfile` before calling PROMOP; PROMOP never takes `UserProfile`.
- Mixed-subject or multi-target batches lock all targets in sorted order; a batch
  that cannot be ordered is rejected.
- No `claimed_at` lease: the transfer is one transaction with row locks. A failed
  transaction leaves no claim; retries are explicit.

## 11. Retention and security

- Staging contains PHI. Access is restricted by §8; every read/write is audited.
- `retained_not_for_transfer` and `approved_deletion` follow the retention policy;
  deletion is a recorded action with a tombstone, not a silent purge.
- `transferred` rows are retained for audit for the configured period; a purge
  command removes payloads but keeps the transition tombstones.
- Legal hold overrides purge.
- No values in logs or error messages; log ids and field names only.
- The archive `PatientInfo` is never read.

## 12. Migration

- Additive migration on the current `cb-v2` leaf; run `makemigrations --check
  --dry-run` and the migration-graph check before push.
- If `cb-v2` moves while the PR is open, rebase and regenerate the unmerged
  migration; never renumber an applied migration.
- The staging table is not an OMOP CDM table and does not depend on OMOP vocab.

## 13. Tests

- create idempotency by `batch_key`; changed payload under the same key is rejected;
- state transitions, including stale/expired approval → `conflict`;
- target changed between read and transfer → `conflict`, no write;
- newer target edit/clear blocks transfer; old target value preserved as
  `displaced_target` with `winning_item_id`;
- disease-less batch transfers atomically with the disease and creates no bare
  record; a disease that is skipped/conflicting does not authorize creation;
- losing value never auto-transfers;
- concurrent transfer: one wins, one no-ops;
- creation race with the existing `/me/` `get_or_create` path;
- group atomicity (therapy line) and no partial group;
- projection failure keeps the record edit and marks pending; durable retry after
  `transferred`;
- API auth: scopes, object ownership, staff/clinical roles, no self-approval,
  redacted non-owner responses, in-process audit;
- retention/purge/legal hold.

## 14. Decisions to approve

1. `patient_portal` for the model.
2. Transfer creates the record when the batch carries disease (recommended).
3. Service-first with token endpoints; in-process calls use the same service.
4. Group identity `group_key`; one row per field/line component.
5. Role split and scopes in §8.
6. Projection retry owner: durable worker after `transferred`.
7. Retention period and legal-hold policy (numeric value can be approved later if
   purge is disabled until then).

## 15. Delivery slices

1. Model + migration + state machine + transition history (no API).
2. Transfer service for a single scalar field, with the full decision evaluator and
   revalidation.
3. Batch/disease-less transfer and displaced-target retention.
4. Service-token API for create/list/transfer.
5. Resolution API + retention/deletion.
6. CB client + repair integration (cancerbot-org/cancerbot#5594).
