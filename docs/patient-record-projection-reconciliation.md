# Reconciling legacy PatientRecord values

`PatientRecord` is a derived clinical read model.  It is not an alternate
clinical source of truth.  Some pre-#484 deployments may nevertheless contain
values entered directly into that projection.

Run `reconcile_patient_record_projection` first with no write options.  It
reports only mapped numeric lab/vital tuples:

- `RECONCILABLE`: a numeric lab/vital field with a defined LOINC mapping and no
  corresponding OMOP Measurement.  The report is still read-only: the
  projection does not contain a clinical event date.

Unmapped `PatientRecord` fields are projection-owned data and remain writable;
they are deliberately excluded from this inventory and migration.  The mapping
catalog used here distinguishes only API/repair-supported mapped tuples from
those unmapped fields—it is not a claim that all fields are OMOP-derived.

An operator who has recovered and verified the actual clinical event date may
create only the `RECONCILABLE` facts:

```sh
python manage.py reconcile_patient_record_projection --person-id 123 \
  --apply --event-date 2025-03-14
```

`--event-date` is mandatory with `--apply`; the command never substitutes the
run date or any other synthetic date.  It also requires the exact mapped LOINC
and the OMOP Lab measurement-type concepts to be loaded, and skips rather than
falls back to a generic concept.  Re-running is idempotent with respect to an
already-present Measurement for the person and LOINC.

After a successful repair, re-derive the affected records using the existing
`backfill_patient_records` command.  This process does not alter unmapped
projection-owned values.
