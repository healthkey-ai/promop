# Issue: Add high-value Apple wearable summary fields to PatientInfo

Priority: High
Labels: `priority: high`, `schema`, `patientinfo`, `wearables`, `trial-matching`, `predictive-modeling`

## Background

The PHR mobile bridge syncs Apple wearable/HealthKit data into PROMOP. Raw time-series wearable samples belong in standard OMOP `Measurement` and `Observation` rows, but the flat `PatientInfo` read model currently exposes only a few coarse wearable-adjacent fields:

- `heartrate`
- `heartrate_variability`
- `exercise_frequency`
- `exercise_minutes_per_week`
- `sleep_hours_per_night`
- `sleep_quality`
- weight/height/BMI and blood pressure vitals

That is not enough for fast trial screening, standard-of-care recommendations, or model features without repeatedly scanning high-volume wearable records.

Important: the bridge doc at `https://github.com/healthkey-ai/phr-mobile-bridge/blob/main/docs/phr-bridge-app.md#what-gets-synced` should be verified during implementation. GitHub returned 404/unauthorized in the current workspace, so this issue is based on the referenced Apple wearable sync surface plus the current PROMOP schema.

## Goal

Add derived, clinically useful wearable summary attributes to `PatientInfo`. Keep raw Apple samples in OMOP tables, and refresh these columns from recent OMOP wearable/vital rows.

The columns should optimize for:

- Trial matching: performance status proxies, cardiopulmonary eligibility, mobility/activity restrictions, sleep or hypoxemia exclusions.
- Standard of care: functional decline, frailty risk, cardiotoxicity monitoring, sleep/respiratory risk, supportive care triggers.
- Predictive models: longitudinal baseline function, trend and variability features, data coverage/adherence features.

## Proposed PatientInfo fields

`PatientInfo` is a scarce, high-value matching surface. Limit this issue to the top 10 wearable-derived columns only.

| Field | Type | Source metric | Aggregation | Why it matters |
| --- | --- | --- | --- | --- |
| `wearable_last_sync_at` | `DateTimeField(null=True, blank=True)` | Bridge import metadata/latest wearable sample | Latest sync/sample timestamp | Prevents stale wearable features from silently driving trial/model decisions. |
| `wearable_coverage_ratio_30d` | `DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)` | Any wearable samples | Valid wearable days / 30 | Tells matching/modeling whether the 30-day features are reliable. |
| `median_daily_steps_30d` | `IntegerField(null=True, blank=True)` | Steps | Median daily total over valid days | Best compact proxy for function, frailty, ECOG-like performance, and outcomes. |
| `active_minutes_per_day_30d` | `DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)` | Exercise/activity minutes | Mean daily total over valid days | Captures exertional activity beyond step count; useful for standard-of-care activity recommendations. |
| `activity_trend_30d` | `CharField(max_length=20, null=True, blank=True)` | Steps or active minutes | `improving`, `stable`, `declining`, `insufficient_data` | High-value deterioration/recovery signal without storing several trend columns. |
| `resting_heart_rate_avg_30d` | `IntegerField(null=True, blank=True)` | Resting heart rate | Mean over valid days | Simple cardiophysiologic baseline; useful for infection, toxicity, deconditioning, and fitness signals. |
| `hrv_sdnn_avg_30d` | `DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)` | HRV SDNN | Mean over valid days | Autonomic stress/recovery marker with strong predictive-model potential. |
| `oxygen_saturation_min_30d` | `DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)` | SpO2 | Minimum valid measurement after artifact filtering | Compact pulmonary eligibility and hypoxemia risk signal. |
| `respiratory_rate_avg_30d` | `DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)` | Respiratory rate | Mean over valid measurements | Useful for infection/pulmonary deterioration risk and standard-of-care escalation. |
| `sleep_duration_hours_avg_30d` | `DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)` | Sleep analysis | Mean nightly asleep duration | Broadly predictive and actionable; better than self-reported `sleep_hours_per_night`. |

Keep existing `heartrate`, `heartrate_variability`, `exercise_minutes_per_week`, `sleep_hours_per_night`, and `sleep_quality` for backward compatibility and self-reported/current display values.

## OMOP storage and mapping guidance

Do not store raw wearable samples directly in `PatientInfo`.

Raw and normalized source data should remain in:

- `Measurement`: numeric HealthKit samples such as steps, activity/exercise minutes, heart rate, resting heart rate, HRV, SpO2, respiratory rate, body metrics, and blood pressure.
- `Observation`: sleep intervals, workout/activity categories when not naturally numeric, source/device metadata, user-entered lifestyle observations.
- `VisitOccurrence`/provenance/ownership records where applicable for import batches and traceability.

Implementation should extend the existing PatientInfo sync path:

- Add field mappings/constants near `omop_core/services/mappings.py`.
- Add a wearable summary refresh function in `omop_core/services/patient_record_service.py`.
- Run it from `refresh_patient_record` after raw OMOP wearable rows are imported.
- Ensure the bridge import path triggers refresh for affected persons.

Use standard vocabularies where available. Prefer LOINC/SNOMED/OMOP standard concepts when present; otherwise preserve Apple HealthKit source identifiers in `measurement_source_value` or `observation_source_value` while mapping to the best available standard concept.

## Aggregation rules

- Default lookback: 30 days ending at `wearable_last_sync_at` or the latest wearable sample date.
- Valid day: a day with enough samples to compute that metric; define per metric.
- Use valid days, not all calendar days, for metric means.
- Persist `wearable_coverage_ratio_30d` so consumers can judge feature quality.
- Do not overwrite a field with null unless the refresh intentionally marks insufficient coverage.
- Trend fields should compare first half vs second half of the lookback window with configurable thresholds.
- Include artifact filtering for impossible values, especially SpO2, HR, HRV, and respiratory rate.

## Recommended thresholds for flags

Initial configurable defaults:

- SpO2 low event: valid reading `< 90%`.
- Insufficient coverage: fewer than 7 valid days in the 30-day lookback for a metric.

Thresholds should live in a service-level config/constant, not hard-coded into model methods.

## Explicitly out of scope

Do not add these to `PatientInfo` unless a later use case justifies them:

- Per-sample timestamps, hourly bins, routes, GPS paths, device IDs, or workout names.
- Active energy/basal energy/calories unless a model or recommendation workflow explicitly requires them.
- VO2 max, walking heart rate, sleep-stage summaries, sedentary time, flights climbed, distance, SpO2 average/count, source, lookback-days, and separate valid-day count fields for this iteration.
- Mindfulness/audio/environmental exposure fields unless mapped to a concrete clinical workflow.
- Full sleep-stage timeline details. Keep those in OMOP `Observation`; only derived summaries belong in `PatientInfo`.

## Acceptance criteria

- New nullable fields are added to `PatientInfo` with a Django migration.
- Wearable summary fields are populated from OMOP `Measurement`/`Observation` data during PatientInfo refresh.
- Existing fields and APIs remain backward compatible.
- Serializer/API responses expose the new fields.
- Frontend `PatientInfoData` types include the new fields. UI display can be a follow-up unless needed by current consumers.
- Unit tests cover:
  - 30-day aggregation for steps/activity.
  - cardiovascular summaries.
  - sleep summaries.
  - SpO2 minimum artifact handling.
  - insufficient coverage behavior.
  - no regression for existing `heartrate`, `heartrate_variability`, `exercise_minutes_per_week`, and `sleep_hours_per_night`.
- Documentation updates `OMOP2PatientInfo.md` with wearable mapping and derivation rules.

## Implementation notes

Suggested file touch points:

- `omop_core/models.py`
- new migration under `omop_core/migrations/`
- `omop_core/services/mappings.py`
- `omop_core/services/patient_record_service.py`
- `patient_portal/api/serializers.py`
- `frontend/src/federation/patientInfoTypes.ts`
- `OMOP2PatientInfo.md`
- tests in `tests/` or `patient_portal/tests.py`, following existing PatientInfo sync test patterns

## Why this is high priority

Apple wearable data can provide functional status and physiologic trend signals that are not consistently captured in EHR data. These fields can materially improve:

- trial pre-screening for performance, cardiopulmonary reserve, and activity-related criteria;
- standard-of-care recommendations for frailty, deconditioning, sleep, and respiratory risk;
- predictive model features for deterioration, toxicity, hospitalization, and treatment tolerance.

Without these summaries in `PatientInfo`, consumers either ignore wearable data or perform expensive high-volume time-series queries during matching.
