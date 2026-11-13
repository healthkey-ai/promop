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
   risk. **Every clear candidate may be a genuine `True`.** Resubmitting a
   stored `True` unchanged through the native `PATCH /api/patient-info/<id>/`
   writes neither a revision nor a pending marker, in any era (the shared form
   route marks the field pending, so it classifies as authored). Such an answer is
   indistinguishable from the default and would be cleared. A `True` answered **before
   `RecordRevision` existed** (0125, while the columns date from 0002) is a second
   such case. The coverage split bounds only that second case:
   `clear_candidates_in_authored_records` means "this record has a person-authored
   revision on *some* field", not "this answer was confirmed". No code can tell
   them apart: the manifest review is the mitigation. Spot-check the records and
   compare the counts with staging's history before approving.
3. **Approve `approval_sha256` out of band** (the full-document hash, not the
   `manifest_sha256` data fingerprint). A summary-only manifest is not accepted
   for repair.
4. **Stop writes for the cohort.** Pause ETL and UI writes for the audited
   patients for the run. Apply locks the candidate records, their person rows and
   the pregnancy formula row; inserted evidence waits. The apply transaction runs
   at READ COMMITTED, so the post-lock re-derivation and the post-loop re-read
   see anything committed since the audit; an in-place update of an existing
   evidence row is not blocked by the row locks, and a change committed after the
   re-read or in a row the audit filtered out (an erroneous fact flipped to
   active, an observation patched in place without touching its person) is
   invisible. The write pause is therefore a gate, not a nicety. During the run
   itself unrelated patients stay writable. `lock_timeout` for acquiring the
   locks is 5 s and is reset before the writes.
5. **Keep the whole database quiet from the audit to `--apply`.** The approved
   digest covers every record and the global totals (each record's
   `derived_at`, its coverage flag, the person-authored revision count), not
   only the cohort. Any write to any patient between the audit and the apply,
   including a re-derivation, makes the repair refuse with "digest mismatch;
   rerun audit". That is fail-closed, but it costs the approval cycle: pause ETL,
   re-derivation and UI writes globally until the apply starts, and narrow the
   pause to the cohort only for the run.

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
  blocked for the loop (they still stale the approval if they land between the
  audit and the apply, see step 5 above). Inserted evidence (a revision, attestation or recorded
  fact) takes a key-share lock on the parent and waits; an in-place update of an
  existing evidence row does not. The transaction runs at READ COMMITTED when the
  command owns it, so the re-derivation under the locks and the post-loop re-read
  see anything committed since the audit — a serializable snapshot would be
  fixed by the audit read and miss exactly that window. A commit after the
  re-read, or a row the audit filtered out, can still change unseen, which is why
  the write pause above is required.
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
