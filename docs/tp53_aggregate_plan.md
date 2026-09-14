# Conservative TP53 aggregate correction for 1.3

Release-owner decision, 2026-09-14: preserve the current positive-evidence rule
and replace unsupported false results with unknown. This is scoped work under
[#1240](https://github.com/healthkey-ai/promop/issues/1240); it does not close the
clinical/source decisions in [#1246](https://github.com/healthkey-ai/promop/issues/1246).

## Consumer contract

`PatientRecord.tp53_disruption` is already nullable. Derivation version 8 emits:

- `true` when at least one genetic mutation meets the unchanged predicate: gene
  TP53 and interpretation Pathogenic (case-insensitive), assessment absent from
  the legacy dictionary, empty or `present`, and status absent from the legacy
  dictionary or `present`.
- `null` otherwise. No findings, benign/unclassified findings, unrelated genes,
  explicit absent, indeterminate, no-call and not-tested assessments do not
  establish a negative aggregate. This derivation emits no `false` result.

The legacy behavior for missing assessment/status is retained only as part of
the existing positive predicate. This change does not broaden it to Likely
pathogenic, implement source/date conflict resolution, establish assay
completeness, or add del(17p) to the aggregate. Gene-only negative evidence does
not establish structural absence. Named findings retain their original states.

API and trial consumers must preserve JSON null and treat it as unknown. Do
not coerce it with a boolean cast, `COALESCE(..., false)`, or a default negative
label. A negative eligibility criterion is not satisfied by this null value.
External consumer validation remains required before claiming the genomics
release gate complete. No clinical thresholds are introduced.

## Deployment and reconciliation

No column/schema change is needed for the TP53 correction. Version 8 marks older derivations stale; source
OMOP rows remain unchanged. Review the aggregate impact report and
`backfill_patient_records --dry-run` output before scheduling rederivation. The full derivation includes version 7's unit
normalization, so also review [the blood-count rollout](clinical-unit-policy.md#anc-and-platelet-rollout-640).
Do not run a blanket update replacing every stored false: refresh from current
source evidence, preserving the normal pending-edit rules.

Record the exact deployed SHA, counts of true/false/null before and after,
and downstream API/eligibility behavior. Recovery requires restoring a
compatible application artifact and rederiving the affected records from
preserved source facts; reverting code alone does not reverse stored values.
Do not treat a recovery-generated false as evidence of a clinical negative.


## Read-only Render staging estimate — 2026-09-14

At 08:32 UTC, evaluating the unchanged positive predicate against cached
`PatientRecord.genetic_mutations` found 3,000 stored false values that would
become null, with no qualifying positives in that cached input. Of those
records, 2,999 had derivation version 5 and one had version 4; none had pending
edits on `tp53_disruption` or `genetic_mutations`. This is a cache-based estimate,
not a fresh source extraction or evidence that the patients lack TP53 findings.
No data was changed. A real refresh also picks up intervening derivation changes.
