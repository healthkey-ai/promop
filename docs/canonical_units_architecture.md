# Instance canonical LOINC units

PRomop instance administrators (`is_staff`) may choose a canonical unit for an
active standard LOINC Measurement concept. The choice applies across all
organizations on that instance. Athena standardizes the measurement concept;
it does not generally require one unique unit for that concept. LOINC example
units are reference information, not a mandatory or exhaustive list.

## Using the setting

Open **Code Mapping**, open a mapping whose destination is a LOINC measurement,
and find **Canonical unit for this instance** below the destination details.
Choose a unit and click **Save unit setting**. This saves independently of the
mapping. Ordinary curators can read the setting; only instance administrators
can change it. **Preserve source units** removes the override.

For LOINC 33358-3, choosing `g/dL` converts a result of `20 g/L` to `2 g/dL`.
A source reference interval of `10–30 g/L` becomes `1–3 g/dL`. Reload lab results
to see the new setting. Existing and future results use the same read-time
conversion, so no clinical backfill or patient-record rewrite is needed.

Lab result cards, history and trends use the normalized values. History retains
the original value and unit; the measurement edit dialog explicitly edits the
original result in its original unit. Missing, incompatible or unsupported units
produce a conversion warning and no normalized number, not a guessed value.
Graphs and deltas never combine different unit labels on one scale.

## Compatibility boundary

Stored OMOP values, units and reference intervals are preserved. Existing API
fields retain their source meaning. Measurement and lab-result APIs expose an
additive `normalized` object with `value`, `unit`, `range_low`, `range_high`,
`revision` and `error`. It is null when no override is configured. API consumers
performing comparisons or analytics in instance units must use this object as a
whole, checking `error`, rather than pairing a source number with its unit.

PatientRecord integration fields that explicitly specify a unit, such as
`albumin_g_dl`, and clinical rules with fixed-unit thresholds retain their
existing contracts. Choosing `g/L` does not put a g/L number into a field named
`albumin_g_dl`. Their dedicated conversion paths remain governed by
[the clinical units policy](clinical-unit-policy.md). This feature does not
claim new conversion coverage for those legacy projection paths.

## Validation and persistence

`CanonicalUnitPreference` stores the concept, unit, measured property and
revision. `CanonicalUnitChange` retains the actor, previous/new units and
revision for each change. Resetting keeps the row and increments its revision.
PUT `/api/v1/concepts/{concept_id}/canonical-unit/` requires `unit` and the
revision returned by GET; conflicting writes return 409. The concept row is
locked to serialize first-time writes as well as subsequent changes.

`load_loinc_classes` now imports LOINC `PROPERTY` and `SCALE_TYP` alongside
`EXAMPLE_UNITS`. When that metadata is absent, only explicit recognized
properties in the Athena concept name (for example `[Mass/volume]`) are used.
Unsupported properties and non-quantitative scales offer no unit choices.

Conversions use a case-sensitive, explicit UCUM registry with Decimal factors:
mass, substance and number concentrations, catalytic-activity concentration,
time, mass, length, volume and Celsius/Fahrenheit temperature. The available
choices are restricted to the same measured property. Converting mass to molar
concentration is deliberately unsupported: it requires analyte-specific evidence
and can require a different LOINC concept. Assay-specific arbitrary units are
not treated as interchangeable. `g/L` (mass) and `G/L` (legacy giga-count) are
distinct. Unit text takes precedence; absent unit text can fall back only to a
UCUM unit concept. Numeric strings and censored textual results are not parsed
as ordinary numbers.

Preferences are read once per response and never cached across requests or
workers. A change therefore recomputes all historical normalized values on the
next fetch. A client holding cached results must reload them; no mixed policy
revision is presented within a single response.

## Deployment and rollback

Migration `0249_canonical_loinc_units` adds configuration/audit tables and LOINC
metadata columns. It does not update clinical rows or enable any preference.
Deploy the migration before the application. Older LOINC imports continue to
work with the conservative concept-name fallback; reload LOINC metadata to
populate the authoritative property/scale fields.

To roll back a unit choice, select **Preserve source units** or restore the prior
unit using its current revision. Source facts remain unchanged. Reverting the
schema migration removes configuration history, so export those configuration
and audit rows before a schema rollback. No patient-data restore is required.

Validation covers equivalent scales, incompatible properties, case sensitivity,
zero, nonfinite inputs, temperature offsets, reference ranges, authorization,
revision conflicts, audit history, reset, and read-time conversion of both
historical and new measurements. Existing blood-count conversion regressions
verify fixed-unit integration contracts remain intact.
