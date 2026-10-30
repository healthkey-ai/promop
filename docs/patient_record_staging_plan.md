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

Open for review: `patient_portal` vs a new small app. The recommendation is
`patient_portal` because it already owns patient-facing application state and
migrations; no new app for one table.

## 4. Model

`PatientRecordStaging` (one row per field, not one JSON blob per patient):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | primary key |
| `transfer_key` | text, unique | client idempotency key for a create batch |
| `subject_key` | text | stable CB identity: `user:<id>` or `patient_info:<id>` |
| `person_id` | bigint, null | PROMOP Person; null until CB links |
| `field` | text | manifest field name |
| `value` | jsonb | scalar/list/dict, with the manifest type |
| `kind` | text | `source_value` or `displaced_target` |
| `provenance` | jsonb | source, edit/revision time, audit reference |
| `manifest_hash` | char(64) | reviewed allowlist that produced the value |
| `state` | text | see §5 |
| `destination` | text, null | staging row id / target field for a displaced value |
| `owner` | text, null | accountable role or person |
| `target_version` | text, null | `PatientRecord.updated_at` + fingerprint the decision is bound to |
| `approval` | jsonb, null | see §6 |
| `attempts` | int | bounded retry count |
| `claimed_at` | timestamptz, null | atomic claim for transfer |
| `transferred_at` | timestamptz, null | terminal success |
| `created_at` / `updated_at` | timestamptz | audit |

Indexes: `(subject_key, state)`, `(person_id, state)`, `(state, updated_at)`,
unique `transfer_key` per create batch (or `(transfer_key, field)` if the batch key
is reused across rows).

Constraints: `state` in the enum; `transferred` requires `transferred_at`;
`approved_deletion` requires an approval with a deletion decision.

## 5. States and transitions

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

## 6. Approval object

Common fields for **every** outcome:

```json
{
  "approver": "actor id",
  "role": "clinical-data-owner | data-owner | privacy | security",
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
their own conflict; a role not named here cannot approve.

## 7. Transfer algorithm

`transfer(subject_key | staging_ids, *, expected_target_version, actor)`:

1. Lock the staging rows (`select_for_update`) in id order; skip rows not in
   `awaiting_transfer`.
2. Lock the `PatientRecord` row (or the `Person` when the record does not exist
   yet) **after** the staging rows. No path may take the record lock before the
   staging lock.
3. Recompute the target version. If it differs from `expected_target_version`, move
   the row to `conflict` and stop; do not write.
4. If `state == conflict`, require a valid, unexpired approval whose
   `target_version`/`manifest_hash` match; otherwise stop.
5. Create the `PatientRecord` in the same transaction **only when the batch carries
   a disease value**; otherwise leave the patient staged. Apply the staged fields
   to `PatientRecord` and add them to `user_edited_fields`. No full
   `refresh_patient_record`.
6. Project mapped fields best-effort per field, using the reviewed
   `project_single_value` contract. A projection failure leaves the record edit and
   a pending projection; it does not fail the transfer.
7. Mark `transferred` in the same transaction as the record write; a crash leaves
   either both or neither.
8. A repeated call is a no-op that returns the current state.

Disease-less patient: CB creates the Person/link, then calls transfer with the
person id and the staged payload. The record is created only in the transfer
transaction that also writes the disease; it is never a bare record.

Losing/displaced values: `kind=displaced_target` rows are written at the same time
as the winning value, but stay `retained_not_for_transfer`. They are never
auto-transferred.

## 8. API surface for CancerBot

Internal service endpoints; not patient-facing.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/patient-record-staging/` | service token, `patient_record_staging:write` | create a batch (idempotent by `transfer_key`) |
| `GET` | `/api/v1/patient-record-staging/?subject_key=…` | service token, `:read` | list state for a patient |
| `POST` | `/api/v1/patient-record-staging/transfer/` | service token, `:write` | transfer a batch with expected target version |
| `POST` | `/api/v1/patient-record-staging/<id>/resolve/` | staff/clinical role | record an approval/resolution |
| `POST` | `/api/v1/patient-record-staging/<id>/delete/` | privacy/data owner | approved deletion |

The API never accepts `PatientInfo` and never returns it. Responses expose state,
field, and the repair action, not clinical values, to non-owner roles.

Open for review: service-token endpoints vs an in-process Python service (CB embeds
PROMOP per ADR 0001). Recommendation: implement the service first, expose the
minimal token endpoints for the migration job and repair, and keep the matcher
outside.

## 9. Concurrency and idempotency

- `transfer_key` makes create idempotent; a duplicate create returns the existing
  rows.
- Transfer claims rows with `select_for_update`; a concurrent transfer sees
  `transferred` and no-ops.
- Lock order: staging rows → `PatientRecord`/`Person`. CB's repair holds
  `UserProfile` before calling PROMOP; PROMOP never takes `UserProfile`.
- A stale approval returns the row to `conflict`; it never writes.
- Retry is bounded; exhausted rows stay `conflict` and alert.

## 10. Retention and security

- Staging contains PHI. Access is restricted to the service and authorized
  clinical/data roles; every read/write is audited.
- `retained_not_for_transfer` and `approved_deletion` follow the retention policy;
  deletion is a recorded action, not a silent purge.
- No values in logs or error messages; log ids and field names only.
- The archive `PatientInfo` is never read.

## 11. Tests

- create idempotency by `transfer_key`;
- state transitions, including stale approval → `conflict`;
- target changed between read and transfer → `conflict`, no write;
- newer target edit/clear blocks transfer; old target value preserved as
  `displaced_target`;
- disease-less batch transfers atomically with the disease and creates no bare
  record;
- losing value never auto-transfers;
- concurrent transfer: one wins, one no-ops;
- projection failure keeps the record edit and marks pending;
- API auth: service token scopes, staff resolution, no self-approval, non-owner
  cannot read values;
- retention/deletion audit.

## 12. Open questions for review

1. `patient_portal` vs a new app for the model.
2. Does transfer create the `PatientRecord` when the batch carries disease, or does
   CB create it first? Recommendation: transfer creates it in the same transaction.
3. Service-token API vs in-process service first.
4. Retention period for `retained_not_for_transfer` / `approved_deletion`.
5. Projection failure representation: reuse the existing pending marker on
   `PatientRecord`, or add a staging `projection_state`.
6. Therapy-line atomic groups: one staging row per line or per field? Recommendation:
   one row per field plus a `group_key`, transferred atomically.
7. Who may approve `retain`/`delete` (clinical/data/privacy split).

## 13. Delivery slices

1. Model + migration + state machine tests (no API).
2. Transfer service for a single scalar field, with revalidation/idempotency.
3. Batch/disease-less transfer and displaced-target retention.
4. Service-token API for create/list/transfer.
5. Resolution API + retention/deletion.
6. CB client + repair integration (cancerbot-org/cancerbot#5594).
