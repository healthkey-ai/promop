# Derivation Changelog

Tracks changes to PatientRecord derivation logic in
`omop_core/services/patient_record_service.py`. Bump `DERIVATION_VERSION`
whenever aggregation or computation logic changes, then add a row here.

After bumping, run `python manage.py backfill_patient_records` to re-derive
stale records.

| Version | Date       | Description                                      |
|---------|------------|--------------------------------------------------|
| 1       | 2026-07-31 | Baseline — all existing derivation logic          |
| 2       | 2026-08-03 | Add per-line therapy-class ("type") concept_ids (`*_therapy_type_ids`) via HemOnc `Is a`→Component Class expansion (ADR 0002) |
| 3       | 2026-08-11 | Populate `date_of_birth` from Person year/month/day when full precision is available, and compute `patient_age` with the birthday-passed adjustment (issue #456) |
