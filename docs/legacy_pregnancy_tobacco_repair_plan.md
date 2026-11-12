# Legacy pregnancy/tobacco repair: staging handoff

Status: the commands are shipped (`#2450`, `#2498`-era code). **No deployment
verification or remote repair is authorized by this file.** Running the repair
is an operator decision; production writes remain out of scope.

## What this repairs

`no_pregnancy_or_lactation_status` was derived by `@not(pregnancy_test_result)`
and `no_tobacco_use_status` by the tobacco observations, both `null=False,
default=True`. A legacy row therefore says "not pregnant" / "does not use
tobacco" whether or not anyone answered. Migrations 0323 and 0329 made the
columns nullable self-assessments and deactivated the pregnancy formula.

`audit_legacy_pregnancy_tobacco` classifies every stored value from its evidence
(record revisions, `user_edited_fields`, the newest tobacco/pregnancy OMOP
fact). `repair_legacy_pregnancy_tobacco` clears **only `unattributed_true`** — a
pure legacy default — and never touches authored, source-backed, conflicting or
rewritten values; those stay for a person's answer or policy review.

## Before `--apply` on staging

1. **Read the audit, not just the counts.** The manifest is a private file
   (0600, never overwritten) holding person ids and answers:

   ```bash
   DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py audit_legacy_pregnancy_tobacco \
     --output "$PRIVATE_DIR/pregnancy-tobacco-audit.json"
   ```

2. **Judge the revision coverage.** `totals.evidence` publishes
   `authored_revisions_total`, `earliest_authored_revision_at`,
   `records_with_any_authored_revision` / `records_without_any_authored_revision`,
   and the number of clear candidates (`clear_candidates`,
   `clear_candidates_in_authored_records`). Only person-authored revisions count:
   migration `0321` and the service's `system` writes stamp a revision on nearly
   every legacy record, so including them would report full coverage and hide the
   risk. A genuine `True` answered **before `RecordRevision` existed** (0125,
   while the columns date from 0002), or entered by a path that wrote neither a
   revision nor a pending marker, is indistinguishable from the default and would
   be cleared. No code can tell them apart: the manifest review is the mitigation.
   Spot-check the records and compare the counts with staging's history before
   approving.
3. **Approve `approval_sha256` out of band** (the full-document hash, not the
   `manifest_sha256` data fingerprint). A summary-only manifest is not accepted
   for repair.
4. **Stop writes for the cohort.** Pause ETL and UI writes for the audited
   patients for the run. Apply locks the candidate records, their person rows and
   the pregnancy formula row; inserted evidence waits. A SERIALIZABLE transaction
   detects serialization anomalies, and the post-loop re-read sees classification
   changes visible to this transaction — neither is a guarantee that every
   concurrent change aborts, and a row the audit filtered out (an erroneous fact
   flipped to active, an observation patched in place without touching its
   person) is invisible to both. The write pause is therefore a gate, not a
   nicety. Unrelated patients stay writable. `lock_timeout` for acquiring the
   locks is 5 s and is reset before the writes.

## Dry-run

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_legacy_pregnancy_tobacco \
  --manifest "$PRIVATE_DIR/pregnancy-tobacco-audit.json" --expect-sha "$APPROVED_AUDIT_SHA"
```

Dry-run writes nothing (no DB rows, no backup) and is not deployment evidence. It
refuses a manifest whose version, database target, cohort size or digest no
longer matches a fresh audit.

## Apply: separate approval required

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_legacy_pregnancy_tobacco \
  --manifest "$PRIVATE_DIR/pregnancy-tobacco-audit.json" --expect-sha "$APPROVED_AUDIT_SHA" \
  --environment staging --apply \
  --backup "$PRIVATE_DIR/pregnancy-tobacco-repair-backup.json"
```

Guards, unchanged by this run:

- `--environment staging` requires host, port and database to match
  `STAGING_DATABASE_URL` exactly; a target matching `PROD_DATABASE_URL` is
  refused. `local` needs an explicit loopback HOST and a verified loopback peer;
  empty HOST, connection services and `hostaddr` overrides are refused.
- 0323/0329 must be applied and the pregnancy formula inactive.
- The whole run is one transaction. It locks the **candidate rows**, their
  person rows and the pregnancy formula row with `SELECT … FOR UPDATE` rather
  than `LOCK TABLE patient_record`; unrelated patient writes are no longer
  blocked for the loop. Inserted evidence (a revision, attestation or recorded
  fact) takes a key-share lock on the parent and waits; an in-place update of an
  existing evidence row does not. The transaction is SERIALIZABLE when the
  command owns it, which detects serialization anomalies, and the repair
  re-derives each candidate's classification under the locks, then re-reads the
  evidence after the loop; that read sees changes visible to this transaction,
  not every concurrent commit. A row the audit filtered out can still change
  unseen, which is why the write pause above is required.
- `RecordRevision` per cleared field (`changed_by='repair:2432:<uuid>'`) plus one
  ledger revision with before/after hashes; `user_edited_fields` is untouched.
  The backup is exclusive, mode 0600, fsynced before commit.
- NULL sticks: `no_pregnancy_or_lactation_status` is never derived after 0329,
  and `no_tobacco_use_status` is rebuilt from facts (a source-backed row is not
  a clear candidate in the first place).

After success, record the printed `backup_sha256`, re-audit, and compare the
classification counts. Do not reuse the original audit: it is invalidated by the
repair's own revisions.

## Guarded rollback

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_legacy_pregnancy_tobacco \
  --rollback "$PRIVATE_DIR/pregnancy-tobacco-repair-backup.json" --expect-sha "$APPROVED_BACKUP_SHA"
```

Rollback restores only this run's original flags and refuses the whole cohort if
any record, revision ledger entry or post-repair hash changed. Locks and target
guards are the same as apply, and the invocation above is a dry-run. After
reviewing it and receiving rollback approval:

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_legacy_pregnancy_tobacco \
  --rollback "$PRIVATE_DIR/pregnancy-tobacco-repair-backup.json" --expect-sha "$APPROVED_BACKUP_SHA" \
  --environment staging --apply \
  --backup "$PRIVATE_DIR/pregnancy-tobacco-rollback-receipt.json"
```

On a conflict, stop for manual review; do not edit the receipt, reset timestamps
or substitute a hash to force it.
