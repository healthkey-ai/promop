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
| 3       | 2026-08-10 | Preserve hand-entered values for derived fields the write-through cannot push to OMOP (`user_edited_fields`); read ECOG/Karnofsky from `measurement` as well as `observation`; stop deriving `patient_age` from the `year_of_birth=1900` registration placeholder (#434) |
| 4       | 2026-08-11 | Split HRV into SDNN and RMSSD (#438). Adds `hrv_rmssd_avg_30d`; `hrv_sdnn_avg_30d` no longer receives Garmin values, which were RMSSD filed under a SDNN concept. Garmin-sourced records will show `hrv_sdnn_avg_30d` drop to null and `hrv_rmssd_avg_30d` populate only after the device export is re-uploaded — the mis-filed OMOP rows are not automatically repairable (see #442). Note `hrv_rmssd_avg_30d` is **not** exposed in the `patient_info` compatibility view, whose column list is frozen at migration 0104 (see #448) — external view consumers see Garmin HRV go null with no replacement |
