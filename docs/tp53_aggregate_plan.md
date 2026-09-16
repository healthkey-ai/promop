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

## EXACT compatibility for 1.3

The owner selected EXACT as the only downstream consumer for this release gate. [EXACT PR #483](https://github.com/healthkey-ai/exact/pull/483) preserves an explicitly supplied Boolean or null through snake/camel-case inline input, source-row adaptation, repeated normalization and the attribute service used by eligibility matching. Unknown remains an unresolved candidate criterion; it cannot produce a confirmed negative match. Omitted aggregates retain EXACT's previous legacy derivation. All three EXACT backend CI groups passed. Verify the deployed consumer includes the merge before rollout.

## Scoped reconciliation

`reconcile_tp53_cache` previews by default. Choose `--organization SLUG`, `--person-id ID`, or explicitly `--all`. After reviewing the JSON receipt, repeat with `--apply` to recompute only `tp53_disruption` from current source findings under the same rule used by version 8. It holds records with pending TP53, general-genomics or priority-genomics edits. Apply takes the same PatientRecord lock as the structured writer, commits each record separately, and is idempotent on retry.

The receipt reports before/after states, transitions, held records and partial completion without patient identifiers or source text. Retain it with the deployed revision. This scoped operation does not change source facts, other cached fields, `derived_at`, or the overall derivation version; it does not claim that version 7's blood-unit refresh has occurred. Held records require separate review. A failed run reports committed progress before exiting unsuccessfully. Recovery uses preserved source facts and a reviewed compatible rule; do not treat historical cached false values as negative evidence.

## Deployment and reconciliation

No column/schema change is needed for the TP53 correction. Version 8 marks older derivations stale; source
OMOP rows remain unchanged. Review the aggregate impact report and
`backfill_patient_records --dry-run` output before scheduling a full rederivation. The scoped command above avoids that wider refresh. The full derivation includes version 7's unit
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
