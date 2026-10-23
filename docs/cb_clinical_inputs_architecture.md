# Versioned clinical source-input contract

`omop_core.services.clinical_input_contract` implements the narrow source-input
contract published in [PROMOP #1845](https://github.com/healthkey-ai/promop/pull/1845)
on the `cb-v2` integration schema. `RECIPES` is the authoritative field/source/unit
reference. Ratios are laboratory-ULN inputs, not INR or seconds; the SDK does not
calculate or substitute them. The nullable projection columns do not supply a
default swallowing answer.

## Availability and authority

SDK version 2 and field availability are separate contracts. `capabilities()`
requires an exact compatible approved `FieldConceptMapping`, the concept-zero
sentinel and the Patient self-report type vocabulary. It verifies the source,
table, scalar answer shape, type and unit. Descriptors and curator availability
use the same resolver; the writer re-resolves and locks mappings inside its
transaction. A client descriptor cannot authorize a recipe.

There are no automatically approved seeds. Missing, proposed, revoked or
incompatible mappings make SDK writes unavailable, not projection-only edits.
An approved mapping grants neither patient access nor a new clinical role.
Hosts must authorize the Person before calling this SDK; native API access and
tenant checks remain in the existing view paths. Facts retain the published
patient-self-report provenance, not an assertion of clinician authorship.

The legacy SCT binding uses a historical nonzero placeholder. It does **not**
authorize the SDK's concept-zero/self-report recipe. That native path remains
unchanged and is not advertised as SDK capability. Recognizing it as SDK
permission requires a separate explicit compatibility decision. Version 2 alone
therefore does not establish four-field readiness.

## Write transaction

1. Validate the entire clinical batch before creating facts.
2. Lock the PatientRecord projection, then Person, then mapping rows in stable
   field order. This preserves the existing projection-before-Person lock order.
3. Append observations. Corrections and clears never update/delete imported or
   earlier rows. False is an explicit answer; null appends a clear tombstone.
4. Require the latest date/primary-key source row to be the row just appended.
   A newer imported fact rejects even an equal-value edit or a clear that would
   otherwise look like unknown.
5. Supersede only the submitted fields' previous pending intent. Refresh and
   verify final values and absence of unresolved pending markers. Preserve other
   pending edits without treating this write as their attestation.

All steps are atomic. A validation, approval, writer, refresh or readback failure
rolls back facts and projection changes. Mixed batches in the generic applier
also run inside one transaction. Native PATCH performs the clinical refresh
before saving other submitted projection edits, so it cannot erase those edits
before the ordinary pending/projection lifecycle handles them.
Body-measurement echoes preserve their original value/unit pair across that
refresh. Only actual value edits receive the native API's existing kg/cm policy;
unchanged pounds/inches/metres are never silently relabeled or converted.
Caller-narrowed descriptors may deny a clinical command even with an approved
recipe. A missing/read-only entry rejects the whole clinical/mixed batch, unlike
legacy scalar partial rejection; a permissive descriptor still cannot grant
mapping approval.

Native full-form autosave may echo unchanged typed values that have no pending
marker. These echoes are removed before availability checks and do not create
facts or acknowledge a source write. Changed or pending inputs still require
the dedicated writer. Explicit SDK equal-value commands always append evidence.
Native create/bulk-create rejects dedicated clinical inputs instead of silently
dropping them; author them after the authorized patient record exists.

## Read compatibility and pending values

Reading historical SDK facts does not depend on the current write approval.
The decoder selects latest date/primary key, checks recipe provenance, units and
answer slots, and returns explicit unknown for invalid latest facts or clears.
It never falls back to an older answer. Legacy non-SDK SCT facts retain their
native decoder; latest SDK SCT rows use strict decoding.

Strict decoding runs in the curated-read phase, before pending restoration.
The snapshot retains raw source-input rows before generic concept-keyed clear
filtering, so a source-key SCT clear can mask a legacy nonzero-concept date.
Generic conversion cannot override the new fields or built-in SCT source-family
selection. SCT mappings to other source keys retain their existing curated priority; this
contract only owns the built-in SCT source family, not cross-source precedence.
Existing unresolved pending values are restored normally, and invalid/absent facts cannot resolve a
pending clinical clear merely because both decode to null. Generic same-day
upsert and approval-triggered pending backfill cannot bypass the source writer.

## Integration boundary

Descriptors retain the `target="patient_record"` envelope and a dedicated
contract marker with source metadata. Consumers must route these edits through
`apply_field_writes` or `write_clinical_inputs`, never generic projection.
Compatibility with the separate shared-form candidate
[PROMOP #1984](https://github.com/healthkey-ai/promop/pull/1984) must be tested on
a composed revision; neither that PR nor #1845 is replaced by this implementation.
This contract does not reconcile IMWG derivation, approve mappings, repin
CancerBot or establish clinical/security release readiness.

## Persisted projections and rollout

Derivation version 19 makes records with earlier derivation versions eligible for the normal
`backfill_patient_records` command. The schema migration itself only adds
nullable columns: it does not perform that backfill or rewrite OMOP facts.
The command performs full PatientRecord re-derivation, not only these inputs.
Unchanged formulas do not imply unchanged computed results.

Before any staging backfill, review a scoped before/after projection comparison
and pending/source evidence, including downstream computed values. The command's
`--dry-run --organization <slug>` reports candidate counts only; it does not show
clinical value changes. A version bump and local synthetic backfill regression
are not clinical approval or authorization to execute a staging/production
backfill. IMWG reconciliation remains a separate gate.
