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

### 4.1 Batch

`PatientRecordStagingBatch` fixes the composition and subject of one migration
request:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | primary key |
| `batch_key` | text, unique | client idempotency key |
| `subject_key` | text | exactly one subject per batch |
| `payload_hash` | char(64) | canonical hash of the committed item set |
| `group_manifest` | jsonb | `{group_key: [required_item_key, ...]}` |
| `state` | text | `open`, `committed`, `abandoned` |
| `created_by` | text | authenticated service identity |
| `created_at` / `committed_at` | timestamptz | audit |

Rules:

- a batch is `open` while items are appended; `commit` freezes the item set and
  computes `payload_hash`;
- a repeated `batch_key` with a different `subject_key` or `payload_hash` is
  rejected (`409 batch_key_conflict`);
- commit validates every `group_manifest` group has exactly its required item keys;
  a partial therapy line or any missing group member blocks the commit;
- committed items are immutable; a changed value or a displaced-target row requires
  a new batch;
- a batch never spans subjects.

### 4.2 Item

`PatientRecordStaging` (one row per field or line component, not one JSON blob per
patient):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | primary key |
| `batch` | FK | `PatientRecordStagingBatch` |
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
| `transferred_at` | timestamptz, null | terminal success |
| `created_at` / `updated_at` | timestamptz | audit |

Constraints and indexes:

- unique `(batch, item_key)`;
- unique `content_hash` = `sha256(batch_key | item_key | payload_hash)`;
- `group_key` is scoped to the batch; every item in a group shares the same
  `subject_key` and is transferred atomically or not at all;
- indexes: `(subject_key, state)`, `(person_id, state)`, `(state, updated_at)`,
  `(group_key)` where not null;
- `state` in the enum; `kind=displaced_target` requires `winning_item_id` and an
  initial state of `retained_not_for_transfer`, later `approved_deletion`;
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
`(staging_id, from_state, to_state, actor, reason, created_at)`.
The transition history is append-only and is the audit source; the current `state`
is a cache of the last transition. A transition that applied a resolution links to
its approvals through `PatientRecordStagingTransitionApproval` (M2M); a delete
transition therefore links both the `data-owner` and `privacy` approvals.

### 4.3 Approval

`PatientRecordStagingApproval` is a row, not a JSON blob inside the item:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | primary key |
| `staging` | FK | item being resolved |
| `approver` | text | server-derived actor |
| `role` | text | `clinical-data-owner`, `data-owner`, `privacy` |
| `decision` | text | `transfer`, `retain`, `delete` |
| `scope` | jsonb | typed: `{"patient": "...", "fields": [...], "cohort": "..."}` |
| `reason` | text | required |
| `mode` | text | `overwrite`, `chronology_override`, `none` |
| `target_version` | text | `updated_at:fingerprint` |
| `manifest_hash` | char(64) | reviewed allowlist |
| `expires_at` | timestamptz | required |
| `created_at` | timestamptz | audit |

Unique `(staging, approver)`; a deletion needs **two distinct approver identities**,
one with role `data-owner` and one with `privacy`. Two rows by the same person are
rejected. The transition links both approvals through the M2M table.

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

## 7. Approval

Approvals are rows (`§4.3 Auth` fields), not a free JSON object. Every transfer that
needed a resolution, and every retain/delete, carries one or more approval rows.

- `transfer` needs one of `clinical-data-owner` or `data-owner`.
- `retain` needs `data-owner` plus a retention location.
- `delete` needs **two distinct rows**: `data-owner` and `privacy`, each with its own
  approver.
- `scope` is typed and must cover the item's patient and field; a cohort scope covers
  only the listed cohort.
- `target_version` and `manifest_hash` bind the decision to the state it was made
  against; a changed target invalidates it.
- `expires_at` is required; an expired approval is `conflict`.
- The migration author and the item's `created_by` cannot be an approver;
  self-approval is rejected.

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

1. Authorize the actor and load the batch. Reject a batch that is not
   `committed`, is not single-subject, or has a missing group member.
2. Lock all staging items in `id` order, then lock the `PatientRecord`/`Person` for
   the batch's single subject. No path may take the record lock before the staging
   lock. A batch that names more than one subject/target is rejected.
3. Re-run the **normative decision table** under the lock, in this order:

   | Source | Target | Mode | Result |
   | --- | --- | --- | --- |
   | pending target | any | any | `keep_target`; stage loser |
   | absent/ambiguous | value/absent | any | `keep_target`/no-op; no invented clear |
   | value | absent | any | `copy` |
   | value | equal value | any | no-op |
   | value | different value | default | `keep_target`; conflict if significant |
   | value | different value | approved cohort/field + `overwrite`, source later | `copy`; displaced target retained |
   | explicit clear | value | default | `conflict` |
   | explicit clear | value | approved cohort/field + `overwrite`, source later | `clear`; displaced target retained |
   | equal/incomparable provenance | divergence | any | `conflict` |
   | no authoritative user event | divergence | any | `conflict` |

   A version match is **not** overwrite authority. `overwrite` requires the approved
   cohort/field scope and a later authoritative source; `chronology_override` needs
   its own recorded approval. This table is self-contained and must stay in lockstep
   with CB's `patientrecord-cutover-plan.md` §0.6.0–§0.6.2; a divergence is a
   blocker, not an interpretation.
4. Compare the stored decision's `target_version`/`manifest_hash` with the current
   target. If they differ, return to `conflict`. If an approval exists, verify
   decision, scope, expiry, target version and manifest hash even when the row is
   `awaiting_transfer`; an expired approval is a `conflict`.
5. For a disease-less batch: create the Person/record only in this transaction and
   only when a valid disease item is part of the transferred group. A disease value
   that is skipped, conflicting or losing does not authorize record creation.
6. Apply the group atomically through the shared PatientRecord writer (validation,
   revisions, `user_edited_fields`, source authorship). All record writes in the
   group share one transaction; a failure of any component rolls back the whole
   group, not only its savepoint. Per-field savepoints are only for the OMOP
   projection, never for the record writes. Do not call `refresh_patient_record`.
   Write a target revision/audit row tied to the staging batch and actor.
7. Write displaced targets as `retained_not_for_transfer` rows in a **new committed
   batch** keyed `"<source_batch>:displaced:<winning_item_id>"`; the original batch
   is immutable. Set `winning_item_id` and write them in the same transaction.
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
- Creation race: the existing `/me/` path uses `get_or_create`. Both it and the
  transfer service take the **same stable anchor first**:
  `pg_advisory_xact_lock(hashtext(subject_key))`, whether or not a Person/record
  exists. The anchor never switches to a row lock. The transfer then re-reads the
  record under the anchor and must not assume it is the only creator. The unique
  `content_hash`, `(batch, item_key)` and the anchor are the arbitration.
- Lock order: subject anchor → staging items → `PatientRecord`/`Person`. CB's repair
  holds `UserProfile` before calling PROMOP; PROMOP never takes `UserProfile`.
- A batch is single-subject by §4.1; a multi-target (multi-patient) batch is
  rejected, not ordered.
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
