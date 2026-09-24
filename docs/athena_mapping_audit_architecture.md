# Auditing Athena mapping reconciliation

## Automatic downstream reconciliation

Migration `0258_reconcile_approved_icd10_from_athena_export` automatically performs
the approved-mapping reconciliation when deployments run `manage.py migrate`.
This includes HealthTree: no separate shell command, Drive credentials, local
download directory, STCM content, or network access is required. Migration 0256
is retained unchanged because it is already part of deployed migration history.

The migration uses a frozen historical-model implementation and a checksum-verified
snapshot of the original September 21, 2026 Athena export. The 1,532,768-byte gzip
contains 116,911 ICD10/ICD10CM source concepts, 148,546 outgoing Maps-to records,
and 15,974 referenced target concepts. It covers the full source vocabularies,
not a list of HealthKey mapping IDs. It retains all exported validity intervals
and evaluates them on the deployment date. This is ordinary Git content, not a
multi-gigabyte vocabulary download or Git LFS dependency.

Each instance's approved non-Athena SCCM mappings with one valid standard export
destination are reattributed to `athena`, updating the destination when needed.
The exact-vocabulary and HT-One label rules, conflict checks, locks, sign-off
preservation and future-import-only behavior described below also apply to the
migration. Missing destinations are reported and skipped; the migration does not
reload vocabularies or overwrite conflicting concept metadata. Its deployment
log reports changes and skipped outcomes. Already-Athena rows are excluded, so
the 1,674 manually corrected HealthKey rows are unchanged. Unresolved rows stay
unchanged unless their blocking conditions have been resolved by deployment.

The migration commits in batches of 250 and can safely resume after interruption.
Reversal retains completed corrections to avoid overwriting later curator edits.
Rebuild the artifact only for a new versioned migration using:

```bash
python scripts/build_athena_reconciliation_snapshot.py --path /path/to/original/athena-export --as-of 2026-09-21 --output /tmp/evidence.json.gz
```

The builder has no database dependency and records input-file fingerprints. Do
not replace this migration's frozen snapshot for a later vocabulary release.

## Reconcile from the original Athena export

Use `reconcile_athena_mappings` to check and optionally reattribute existing
approved, non-Athena ICD10/ICD10CM SCCM rows. It reads `CONCEPT.csv` and outgoing
`Maps to` relationships from `CONCEPT_RELATIONSHIP.csv` in the original export.
`SOURCE_TO_CONCEPT_MAP.csv` is not needed. The local `concept_relationship`
table is not independent evidence because curator approvals also write there.

From the directory containing `manage.py` (Render shell: `~/project/src`):

```bash
# Default source: the configured Athena Google Drive folder; database read-only.
python manage.py reconcile_athena_mappings --report /tmp/athena-preview.csv

# Use the original local export instead of downloading it.
python manage.py reconcile_athena_mappings --path ~/Downloads/vocabulary_download_v5 --report /tmp/athena-preview.csv

# After reviewing the dry-run, explicitly apply using a pinned export.
python manage.py reconcile_athena_mappings --archive /tmp/athena.zip --apply --report /tmp/athena-applied.csv
```

The default folder is
[the shared Athena vocabulary download](https://drive.google.com/drive/u/1/folders/1HoRWGepqcH3pMKK03KNb1oWpaVs0Avl7).
`--gdrive URL` overrides it; `--path`, `--archive`, and `--gdrive` are mutually
exclusive. An optional positional `ICD10` or `ICD10CM` limits the stored source
vocabulary. Omitting it checks both. Only HT-One ICD10 rows use ICD10CM lookup
identity; other rows require their exact vocabulary. Codes ignore case and
surrounding whitespace.

The complete evidence is read before any database update or row lock. Current
source concepts, current outgoing relationships, and current standard targets
are required. Multiple distinct destinations are skipped even if only one is
loaded locally; duplicate edges to the same target do not introduce ambiguity.
The selected destination must already exist locally with matching vocabulary,
code and domain, be external, standard, active, and in a non-deprecated
vocabulary. Missing or conflicting concepts are reported for vocabulary repair.
This command does not alter concept metadata or create vocabulary records.

Dry-run issues only reads. Apply locks and rechecks complete SCCM snapshots and
the current destination concepts in batches of 250. Curator locks and concurrent
edits are preserved. Updates set the target and destination metadata, provenance
`athena`, source `Athena`, timestamp and an appended audit note. Approval status,
reviewer sign-off, suggestion history and existing patient facts stay unchanged.
Only future imports use the revised mapping definitions. Successful rows are
excluded on reruns. Earlier batches remain committed if a later batch fails;
the CSV is flushed after each successful batch.

The CSV records every eligible mapping's prior destination/provenance, exported
source and target IDs, proposed destination metadata, outcome, reason and export
SHA-256 fingerprint. The fingerprint is also retained in applied audit notes.
`--report -` sends CSV to stdout and progress to stderr. Downloaded archives are
temporary and deleted when the command exits. No vocabulary files are committed
to Git and no automatic migration applies the proposed changes.

The ordinary startup `prepare_production_database --gdrive ...` path only
bootstraps migration-required concepts when needed and runs migrations; it does
not automatically refresh the entire vocabulary on an initialized database.
Run this reconciliation explicitly when ready to review/apply its results.

## Diagnose the historical STCM-based migration

`audit_athena_mapping_reconciliation` explains migration `0256` by comparing
approved, non-Athena rows in `source_code_concept_mapping` (SCCM) with the
separate `source_to_concept_map` (STCM) vocabulary table. Its default is a
read-only audit. It does not infer that STCM is empty from unchanged SCCM rows.

Run from the directory containing `manage.py`, including `~/project/src` in
the Render shell:

```bash
python manage.py audit_athena_mapping_reconciliation --report /tmp/athena-mapping-audit.csv
python manage.py audit_athena_mapping_reconciliation ICD10 --report /tmp/icd10-audit.csv
```

The summary reports whether migration 0256 is recorded as applied, STCM counts,
and SCCM counts by vocabulary, approval status and provenance (`origin_system`).
The CSV contains one row per approved, non-Athena SCCM mapping, including raw
code matches, eligible destinations, proposed changes and exclusion reasons.
`--report -` emits CSV on stdout and sends summaries to stderr.

Both ICD10 and ICD10CM are checked by default. Code matching trims surrounding
whitespace and ignores case, matching migration 0256. The report separates
direct vocabulary/code matches from the migration's lookup: only HT-One rows
labelled ICD10 use ICD10CM evidence. Other rows require their exact vocabulary.
STCM dates and invalidity, and destination standard status and validity dates,
are checked separately. Duplicate evidence for the same destination counts as
one target. Multiple distinct valid standard targets are left unchanged.

An optional later apply run uses fresh evidence and updates only mappings with
exactly one eligible destination:

```bash
python manage.py audit_athena_mapping_reconciliation ICD10 --apply --report /tmp/icd10-applied.csv
```

Apply uses batches of 250, locks SCCM rows, compares all fields against their
initial snapshots, and skips concurrent edits and curator locks. It preserves
approval status and reviewer sign-off. It changes the destination and its
domain/vocabulary/table metadata, sets `origin_system=athena` and `source=Athena`,
and appends an audit note. Existing patient records and clinical facts are not
rewritten; the new definitions affect subsequent imports. Successful rows are
excluded on reruns. If a later batch fails, earlier committed batches remain
applied and the flushed CSV records them.

This command deliberately checks the evidence used by migration 0256. It does
not treat the local `concept_relationship` table as independent proof of
Athena provenance: curator approvals also mirror mappings there. When STCM
evidence is absent, consult an original Athena export before designing a
different reattribution rule. There is no new automatic data migration.

For local access to Render staging, first verify the database identity as
required by [the staging configuration guide](render-staging-celery.md). Run
tests against an isolated local database, never staging.
