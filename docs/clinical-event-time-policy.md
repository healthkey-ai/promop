# Clinical event-time policy

## Purpose

PROMOP stores clinical facts in OMOP and derives `PatientRecord` from those
facts. Temporal ordering in that projection is clinical ordering: a value with
a later `measurement_date` is considered newer than one with an earlier date.
Import time is never clinical time.

## Measurement policy

| Source temporal information | OMOP representation |
| --- | --- |
| Effective date and time known | Store the supplied date in `measurement_date` and the supplied timestamp in `measurement_datetime`. |
| Effective date known, time unknown | Store the supplied date in `measurement_date`; leave `measurement_datetime` null. |
| Effective date unknown | Do **not** create an OMOP `Measurement`. Quarantine/report the source fact for remediation or obtain a defensible source date. |

`measurement_date` is required by OMOP CDM. A sentinel date and the migration
or import date are both fabricated clinical assertions. They are prohibited:
either can make an unknown-vintage result incorrectly win or lose when
`PatientRecord` derives the latest value.

`measurement_type_concept` records the provenance/type of a measurement. It
does not make a fabricated `measurement_date` semantically valid and must not
be used as a workaround for an unknown clinical date.

## Importer behavior

The FHIR ingestion API and `bulk_import_fhir_bundle` skip an Observation with
neither `effectiveDateTime` nor `effectivePeriod.start`. The bulk importer
reports the skipped count on stderr. A date-only effective value is accepted
without inventing a midnight `measurement_datetime`.

An importer that encounters undated source values must preserve the source
record in its migration/audit workflow, report it to the data owner, and leave
it out of the OMOP clinical fact tables until a defensible effective date is
available. It must not write the value directly to `PatientRecord`.

## Migration checklist

1. Inventory values lacking an effective date before any backfill.
2. Import only facts with source-supported dates and retain their source
   provenance.
3. Quarantine and count undated values; resolve them with the source system.
4. Re-derive `PatientRecord` from the resulting OMOP facts.

This policy is the prerequisite for the projection-only-value inventory in
#487 and applies to new clinical writers as well as migrations.
