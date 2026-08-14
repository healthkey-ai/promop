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
