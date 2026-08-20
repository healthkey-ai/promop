# Reconciling legacy PatientRecord values

`PatientRecord` is a derived clinical read model.  It is not an alternate
clinical source of truth.  Some pre-#484 deployments may nevertheless contain
values entered directly into that projection.

Run `reconcile_patient_record_projection` first with no write options.  It
reports only mapped numeric lab/vital tuples:

- `RECONCILABLE`: a numeric lab/vital field with a defined LOINC mapping and no
  corresponding OMOP Measurement.  The report is still read-only: the
  projection does not contain a clinical event date.

Unmapped legacy `PatientRecord` fields are deliberately excluded from this
clinical repair inventory. Profile/admin values displayed on PatientRecord are
owned by HealthKey extension columns on `Person`; direct PatientRecord writes
are not a source-of-truth path.

An operator who has recovered and verified the actual clinical event date may
create only the `RECONCILABLE` facts:

```sh
python manage.py reconcile_patient_record_projection --person-id 123 --field hemoglobin_g_dl \
  --apply --event-date 2025-03-14
```

`--event-date` is mandatory with `--apply`; the command never substitutes the
run date or any other synthetic date.  It also requires the exact mapped LOINC
and the OMOP Lab measurement-type concepts to be loaded, and skips rather than
falls back to a generic concept.  Re-running is idempotent with respect to an
already-present Measurement for the person and LOINC.

`--apply` also requires exactly one `--person-id` and one mapped `--field`.
Bulk, whole-person, and whole-database repairs are deliberately prohibited;
each migration must be an operator-reviewed clinical attestation.

After a successful repair, re-derive the affected records using the existing
`backfill_patient_records` command.
