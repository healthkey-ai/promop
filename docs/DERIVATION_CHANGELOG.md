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
| 5       | 2026-08-14 | Derive `date_of_birth` from OMOP `Person` birth fields, with partial/invalid dates normalized to January 1 (#456). This makes API `age` available for ETL-loaded patients and calculates it without the previous year-only off-by-one. |
| 6       | 2026-09-14 | Read explicit disease-profile source facts, preserve follicular grades 3A/3B, and compute FLIPI/GELF from assessed checklists (#1267). Preserve numeric historical FLIPI results when no factors are recorded; pending explicit clears continue to supersede their legacy OMOP source. |
| 7       | 2026-09-14 | Normalize ANC and platelets to 10³/µL across coded, source-only, legacy-name and approved mapping paths (#640). Latest missing/unsupported units and empty results suppress older values. ANC legacy values carry 10³/µL; integer platelet legacy values carry cells/µL. Preserve raw OMOP facts and pending edits. Review the [rollout plan](clinical-unit-policy.md#anc-and-platelet-rollout-640) before backfilling. |
| 8       | 2026-09-14 | Preserve the existing positive TP53 aggregate rule; return null instead of an unsupported false when no finding qualifies (#1240). No negative-result or del(17p) rule is added. Review [scope, consumer contract and rollout](tp53_aggregate_plan.md) before rederiving persisted rows. |
| 9       | 2026-09-15 | Correct Ki-67 percentage to LOINC 29593-1 and methodology to 85069-3. Stop ER results populating Oncotype/methodology and perineural invasion populating nodal status; block the same wrong-code curated recipes without rewriting curator records or clinical facts (#1227). Run `report_breast_mapping_impact` and review source evidence before any backfill; historical mislabeled facts are not automatically relabeled. |
