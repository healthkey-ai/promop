# Clinical units policy

## Purpose

Clinical measurements arrive in units chosen by the reporting laboratory. PRomop
must preserve those source facts while presenting predictable, clinically useful
derived values to applications, matching, and analytics. This policy defines that
boundary for every measurement—not just white blood cell counts.

## Rules

1. **OMOP source facts are immutable.** `Measurement.value_as_number`,
   `unit_source_value`, and `unit_concept_id` retain the source-system value and
   unit. Organization preferences never rewrite them.
2. **Derived fields declare their unit.** New projection fields must encode the
   unit in the field name where practical (for example,
   `wbc_count_thousand_per_ul`) or must be accompanied by an explicit unit field.
   Do not infer a scale from a vague field name such as `*_count`.
3. **US oncology is the default derived convention.** The default follows the
   common US oncology representation used by mCODE/USCDI implementations. A
   tenant may select an alternative canonical system where the projection has
   implemented support for it.
4. **Conversions are explicit and allowlisted.** A projection may convert only
   recognized source-unit expressions. Missing or unrecognized units fail closed:
   the derived value is omitted and a warning is recorded. Source data remains
   available for repair.
5. **Unit changes require a migration plan.** Adding a canonical unit or changing
   a conversion must include source-unit tests, boundary/range tests, documented
   backwards compatibility, and a repair/backfill plan for affected derived data.

## Organization defaults

Every `Organization` exposes `clinical_unit_system`:

| Setting | Meaning |
|---|---|
| `US_ONCOLOGY` (default) | Use the US oncology canonical representation where supported. |
| `SI` | Use an SI canonical representation where supported. |

The setting is a default for **derived compatibility fields only**. It is not a
request to transform stored OMOP facts, and a field whose name explicitly fixes a
unit remains fixed regardless of the organization setting. Changing the setting
marks that organization's PatientRecords stale; run
`python manage.py backfill_patient_records --organization <slug>` to rederive them.

## Current implementation: WBC

White blood cell count is the first implementation of this policy. The explicitly
named `wbc_count_thousand_per_ul` field is always `10*3/uL`; the legacy,
unit-qualified `white_blood_cell_count` uses the organization's default:

| Setting | Derived WBC unit |
|---|---|
| `US_ONCOLOGY` | `10*3/uL` |
| `SI` | `10*9/L` |

Those two representations have the same numeric value. Raw `cells/uL` and
`cells/L` values are converted before projection. Historical `CELLS/UL` and
`CELLS/L` values remain readable but are not emitted by new code.

## Developer checklist

When adding a measurement projection, document the source LOINC(s), supported
source units, target canonical units, conversion formula, organization-setting
behavior, and unknown-unit behavior. Add tests for each supported conversion,
both organization settings where applicable, and a fail-closed unknown-unit case.

## ANC and platelets (#640)

[LOINC 751-8](https://loinc.org/751-8) and
[LOINC 777-3](https://loinc.org/777-3) identify absolute neutrophil and platelet
counts. Their canonical PatientRecord fields are always `10*3/uL`, numerically
equal to `10*9/L`, regardless of organization preferences.

| Source scale | Conversion to thousands/µL |
| --- | --- |
| cells/µL, cell/µL, /µL, {cells}/µL, #/µL | Divide by 1,000 |
| cells/L, cell/L, /L, {cells}/L, #/L | Divide by 1,000,000,000 |
| 10*3/µL, 10^3/µL, K/µL, 10*9/L, 10^9/L | Unchanged |
| G/L (legacy giga-count notation) | Unchanged; lowercase g/L is a mass unit and is rejected |

The converter accepts ASCII `u`, both micro symbols, superscript ³/⁹, spaces,
and historical uppercase CELLS unit codes. Negative/nonfinite counts are
unknown; zero is a valid measured result. The source unit text takes precedence;
an absent source unit may use a UCUM unit concept. An explicit unsupported unit
does not silently fall back to a different scale.

The newest non-erroneous matching result is selected across LOINC, source-code,
supported source-display and historic concept-name paths. A newer empty result
or one with an unknown unit suppresses older results. Approved scalar mapping
readback also converts source units. Pending user edits remain preserved.

`absolute_neutrophile_count` retains the canonical numeric value and now carries
`absolute_neutrophile_count_units=10*3/uL`. `platelet_count` is an integer column,
so it carries cells/µL with `platelet_count_units=CELLS/UL`; for example,
150.5 thousands/µL becomes 150,500 cells/µL. Nonintegral cells/µL or integer
overflow leaves that legacy pair unset while the canonical decimal field remains
subject to its existing range and precision checks. Unit-choice labels now show
the literal scale instead of labeling CELLS/UL as thousands/µL.

FHIR upload and the import command share this projection path. They preserve
the source numeric value; UCUM `valueQuantity.code` is retained when a unit
display is absent. Re-imports persist unit-only corrections as well as numeric
changes. The read model performs conversion; source Measurements are not scaled.

### ANC and platelet rollout (#640)

1. Deploy the code and `0233_blood_count_units_640`. The migration updates model
   unit choices only; it does not rewrite clinical rows.
2. Snapshot the database before refreshing derived records. Inventory ANC and
   platelet source units, missing units, approved mapping overrides and pending
   edits in each intended deployment. Missing/unsupported units will become
   unknown, not assumed thousands/µL. Fix source evidence through the ordinary
   import/correction workflow before relying on these values for eligibility.
3. Preview stale records with `python manage.py backfill_patient_records
   --organization <slug> --dry-run`. This counts stale records; it does not show
   a clinical before/after diff. Review representative source facts and expected
   canonical/legacy values before the scoped refresh.
4. Run `python manage.py backfill_patient_records --organization <slug>
   --batch-size 50` after reviewing the preview. Derivation version 7 marks this
   correction. Run again to verify idempotency; compare source values and units,
   canonical fields, legacy pairs, unknowns and preserved pending edits.
5. Verify the CancerBot boundary: canonical ANC ×1,000 gives cells/µL, and the
   direct platelet value is explicitly in cells/µL. Do not change trial thresholds
   to compensate for old projection scale errors.

The field/value mapping implementation in `feat/field-value-concept-mappings`
owns broader mapping and audited reconciliation work. Integrate its independent
migration head with this one and preserve `blood_count_projection` when merging
the approved mapping reader. Use that branch's reviewed preview/apply/rollback
workflow once available rather than introducing a second reconciliation system.
Coordinate the derivation-version bump when merging either branch.

Rollback of the unit-choice migration does not restore a previous derived-data
snapshot. Keep corrected source facts; restore derived rows only from reviewed
recovery evidence. Redeploying old extraction code can reintroduce the original
scale error. No staging backfill is performed by this change.

#### Render read-only audit, 2026-09-14

A transaction with `default_transaction_read_only=on` examined the latest native
LOINC and supported legacy source results at 07:20 UTC. No data was changed.

| Latest result coverage | ANC | Platelets |
| --- | ---: | ---: |
| Results linked to PatientRecords | 1,999 | 3,000 |
| Recognized source scale (`10*3/uL`) | 999 | 2,000 |
| Missing source scale and UCUM fallback | 1,000 | 1,000 |
| Canonical values expected to become unknown | 1,000 | 1,000 |

The recognized-scale results already agree with the normalized canonical value.
No pending canonical edits were observed in this scope. These are result counts,
not a count of distinct affected people across both markers. Custom approved
question overrides were excluded. Recheck source evidence and affected cohorts
before applying a staging backfill; the numbers alone cannot establish a missing
unit or authorize a source-data repair.
