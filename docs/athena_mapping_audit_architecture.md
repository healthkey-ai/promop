# Auditing Athena mapping reconciliation

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
