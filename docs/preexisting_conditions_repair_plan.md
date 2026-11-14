# Pre-existing-condition answer repair: staging handoff

Status: implementation prepared locally. **No deployment verification or remote
repair is authorized by this file.** Production audit and writes remain out of scope.

## Owners and release gates

The staging deployment owner must provide evidence for every gate below. A merged
PR is not deployment evidence. The operator who runs repair must independently
review the manifest and receive explicit approval for that cohort.

1. Land #1995 (which contains #1997/#1998) and the repair/shared-writer follow-up
   on the intended integration branch. Coordinate convergence with the deployed
   branch; Render staging deploys from `dev`, not automatically from `cb-v2`.
2. Verify the active **Render** services `promop-staging` and
   `promop-staging-worker` run the approved revision. Their `DATABASE_URL` must
   match the operator's `STAGING_DATABASE_URL`. Do not infer this from the old
   `ctomop_dev` URL in CancerBot's environment. Never print credentials.
3. Verify the exact migrations for formula deactivation, contradiction constraint,
   and attestation exist and are applied. The command checks the migration names
   in `REQUIRED` in
   `omop_core/management/commands/repair_pre_existing_conditions.py` and the actual
   named CHECK constraint. Resolve any integration migration changes before use.
4. Confirm the negative-answer formula is inactive and cannot be reactivated by a
   curation import or partial formula PATCH.
5. On designated test patients, verify the deployed UI/API, not just fixtures:
   - confirm a previously unchanged `True` using the deliberate button;
   - withdraw it: the flag becomes `NULL`, never `False`;
   - save categories: they are retained and the blanket answer is withdrawn;
   - clear the last category: the answer remains unknown;
   - an unrelated autosave neither creates nor renews attestation;
   - a late autosave response does not restore a withdrawn answer;
   - stale snapshots return 409 and unauthorized access is refused.
6. If CancerBot participates, deploy its self-only federation adapter, update its
   PROMOP dependency to the reviewed shared-writer revision (with migrations), and
   vendor the corresponding widget. The current old dependency intentionally
   returns 503 for explicit writes. Verify the actual host transport, not a mock.
   The dedicated GET snapshot avoids widening the frozen general projection.
7. Confirm backups, maintenance window, write pause, expected cohort size and
   rollback ownership. Benchmark a representative local clone first: apply locks
   all four answer/provenance tables for a consistent decision and rollback state.
   Acquisition has a five-second timeout; this is not a zero-downtime bulk job.

## Read-only sizing (deployment owner/operator)

Use a private local directory, outside the repository, with restricted access.
The manifest and repair backup contain clinical categories and record identifiers.
Use a SELECT-only credential for the audit where available.

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py audit_pre_existing_conditions \
  --output "$PRIVATE_DIR/conditions-audit.json"
```

Read the full manifest v2. The repair also checks database host, port and name;
regenerate old v2 manifests that predate port identification. Approve its
`approval_sha256` out of band (the full-document hash, not the underlying
`manifest_sha256` data fingerprint). Do not simply compute a new approved hash after a
conflict. A summary-only file is not accepted for repair.

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_pre_existing_conditions \
  --manifest "$PRIVATE_DIR/conditions-audit.json" --expect-sha "$APPROVED_AUDIT_SHA"
```

Dry-run does not write DB rows or a backup. It does not attest deployment readiness.
Only `unattributed_true` and `rewritten_true` with raw `NULL`/`[]` categories are
selected. Explicit-action confirmations, False, unknown, contradictions, malformed
values and nonempty lists are not changed. There is no inference from diagnoses.

## Apply: separate approval required

Do not run this section until all gates above are signed off. `--authoring-verified`
records the operator's assertion; it does **not** perform UI verification.
`--environment staging` requires the connected host/port/name to match
`STAGING_DATABASE_URL` exactly. A target matching `PROD_DATABASE_URL` is refused.
The default `local` mode requires explicit loopback HOST configuration. Empty HOST
and connection-service targets are refused; the effective connected identity must
match the configured host, port and database name.
Local mode also verifies the connected peer's loopback address. `hostaddr` options
and inherited `PGHOSTADDR` overrides are refused, even when HOST says localhost.

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_pre_existing_conditions \
  --manifest "$PRIVATE_DIR/conditions-audit.json" --expect-sha "$APPROVED_AUDIT_SHA" \
  --environment staging --authoring-verified --apply \
  --backup "$PRIVATE_DIR/conditions-repair-backup.json"
```

The transaction rechecks the approved cohort and readiness under write-excluding
locks. Selected flags become NULL; legacy attestations are removed but preserved
in the private backup and a durable `RecordRevision` repair ledger. Original
timestamps retain microseconds. Backup creation is exclusive, mode 0600; both the
file and its parent directory are fsynced before DB commit. Any failure aborts all DB changes. A file remaining
after failure is **not** proof of commit; rollback verifies the matching DB ledger.
Protect and retain the database backup independently of this targeted receipt.

After success, record the printed `backup_sha256`, re-audit, verify exact counts,
test both matchers on designated fixtures, and recheck explicit confirmation.
Do not reuse the original audit: flag/provenance changes invalidate it.

## Guarded rollback

Rollback restores only this run's original flags and legacy attestations. It does
not restore a whole database. It refuses the entire cohort if any record,
non-repair revision history or attestation changed after apply, or if a DB ledger
entry is missing. It never overwrites a subsequent patient's or clinician's answer.

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_pre_existing_conditions \
  --rollback "$PRIVATE_DIR/conditions-repair-backup.json" --expect-sha "$APPROVED_BACKUP_SHA"
```

Only after reviewing that dry-run and receiving rollback approval:

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py repair_pre_existing_conditions \
  --rollback "$PRIVATE_DIR/conditions-repair-backup.json" --expect-sha "$APPROVED_BACKUP_SHA" \
  --environment staging --authoring-verified --apply \
  --backup "$PRIVATE_DIR/conditions-rollback-receipt.json"
```

On a conflict, stop for manual review; do not delete evidence, reset timestamps,
edit the receipt, or substitute a new hash to force rollback. Rollback intentionally
restores the historical uncertain True value, not a new explicit confirmation.
