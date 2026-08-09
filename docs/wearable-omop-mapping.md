# Wearable Data → OMOP Measurement / Observation Mapping

How Apple Watch and Garmin metrics are normalized into OMOP CDM v5.4 `measurement` and
`observation` rows, and what still needs to be built.

Companion documents:
- [concept-mapping.md](concept-mapping.md) — general LOINC/SNOMED/HemOnc → OMOP concept resolution
- [apple-wearable-patientinfo-fields.md](apple-wearable-patientinfo-fields.md) — the derived
  `PatientRecord` summary columns that sit *above* this layer

---

## Design principle: one canonical metric, two device adapters

Device-specific vocabulary must not leak past the parser boundary. Apple exports HealthKit
type identifiers; Garmin exports FIT message/field pairs. Both are translated to a small set of
**canonical metric keys** at parse time, and only the canonical key reaches the OMOP writer.

```
Apple export.zip ──► parse_apple_health_export ──┐
                     (_APPLE_TYPE_MAP)           │
                                                 ├──► WearableSample ──► one LOINC ──► Measurement
Garmin .fit ────────► parse_garmin_fit ──────────┘   (metric_key,        concept        or Observation
                     (FIT message handlers)           date, value)
```

`WearableSample` (`omop_core/services/wearable_parsers.py:18`) is the normalization contract —
`(metric_key, date, value)`, nothing else. The consequence is that **a metric is stored
identically no matter which device produced it**: same concept, same units, same table. A query
for resting heart rate never has to know whether the patient wears an Apple Watch or a Fenix.

Both parsers reduce to **one value per metric per calendar day** before returning. Cumulative
metrics (steps, distance, energy, flights, active minutes, sleep) are summed across the day;
rates and percentages (HR, SpO2, HRV, respiratory rate, speed, step length, double support,
VO2 max, body mass) are averaged. See `wearable_parsers.py:328` (Garmin) and `:489` (Apple).

---

## The normalization table

The canonical registry is `WEARABLE_LOINC` in `omop_core/services/mappings.py:91`. One row per
metric key; the LOINC code is the join point between the two device adapters.

| Metric key | LOINC | OMOP table | UCUM unit | Daily agg | Apple HealthKit type | Garmin FIT source |
|---|---|---|---|---|---|---|
| `steps` | 55423-8 | measurement | `/d` | sum | `HKQuantityTypeIdentifierStepCount` | `monitoring.steps` → `.cycles`; fallback `session.total_steps`/`total_cycles` |
| `active_minutes` | 77592-4 | measurement | `min` | sum | `HKQuantityTypeIdentifierAppleExerciseTime` | `monitoring.active_time` ÷ 60; `session.total_timer_time` ÷ 60 |
| `resting_hr` | 40443-4 | measurement | `/min` | mean | `HKQuantityTypeIdentifierRestingHeartRate` (or derived, below) | `monitoring_hr_data.resting_heart_rate` (or derived, below) |
| `hrv_sdnn` | 80404-7 | measurement | `ms` | mean | `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` | `hrv_status_summary.weekly_average`/`last_night_average`; `hrv_value.value`; legacy `hrv.sdnn` |
| `spo2` | 59408-5 | measurement | `%` | mean | `HKQuantityTypeIdentifierOxygenSaturation` | `spo2_data.reading_spo2`; fallback `session.saturated_hemoglobin_percent` |
| `respiratory_rate` | 9279-1 | measurement | `/min` | mean | `HKQuantityTypeIdentifierRespiratoryRate` | `respiration_rate.respiration_rate`; fallback `session.avg_respiration_rate` |
| `sleep_duration` | 93832-4 | **observation** | `h` | sum | `HKCategoryTypeIdentifierSleepAnalysis` (asleep spans only) | `sleep_level` timestamp spans; fallback `sleep_data.total_timer_time` ÷ 3600 |
| `vo2_max` | 94122-9 | measurement | `mL/kg/min` | mean | `HKQuantityTypeIdentifierVO2Max` | `session.enhanced_max_oxygen_consumption` → `vo2_max` |
| `distance` | 41953-1 | measurement | `km` | sum | `HKQuantityTypeIdentifierDistanceWalkingRunning` | `monitoring.distance` ÷ 1000; `session.total_distance` ÷ 1000 |
| `walking_speed` | 41909-3 | measurement | `km/hr` | mean | `HKQuantityTypeIdentifierWalkingSpeed` | *(no source — see gaps)* |
| `walking_step_length` | 96341-8 | measurement | `cm` | mean | `HKQuantityTypeIdentifierWalkingStepLength` | *(no source)* |
| `walking_double_support_pct` | 96343-4 | measurement | `%` | mean | `HKQuantityTypeIdentifierWalkingDoubleSupportPercentage` | *(no source)* |
| `walking_hr_avg` | 89270-3 | measurement | `/min` | mean | `HKQuantityTypeIdentifierWalkingHeartRateAverage` | *(no source)* |
| `flights_climbed` | 96340-0 | measurement | `{flights}` | sum | `HKQuantityTypeIdentifierFlightsClimbed` | *(no source)* |
| `active_energy` | 55424-6 | measurement | `kcal` | sum | `HKQuantityTypeIdentifierActiveEnergyBurned` | `monitoring.active_calories`; `session.total_calories` |
| `basal_energy` | 41982-0 | measurement | `kcal` | sum | `HKQuantityTypeIdentifierBasalEnergyBurned` | `monitoring_info.resting_metabolic_rate` |
| `body_mass` | 29463-7 | measurement | `kg` | mean | `HKQuantityTypeIdentifierBodyMass` | *(no source)* |

### Why one metric lands in `observation` and the rest in `measurement`

`sleep_duration` is written to `observation`; everything else to `measurement`
(`patient_portal/api/views.py:3677`). The split is defensible under OMOP conventions — sleep
duration is a derived interval describing a state rather than a quantified specimen or
instrument reading — but it is applied inconsistently today (see Gap 2).

Arguments could be made for moving `active_minutes`, `flights_climbed`, and `steps` to
`observation` on the same reasoning. The recommendation here is the opposite: **keep everything
in `measurement` except sleep**, because every non-sleep metric is a numeric quantity with a
UCUM unit and benefits from `measurement`'s `unit_concept_id`, `range_low`/`range_high`, and
`operator_concept_id` columns, none of which exist on `observation`.

---

## Derived values (not directly exported by either device)

Two metrics are computed rather than read, and this is where the two adapters deliberately
converge on the same algorithm so the stored values stay comparable:

**Resting heart rate — 10th-percentile fallback.** Neither device reliably exports resting HR.
When no dedicated resting-HR record exists for a day, both parsers take the 10th percentile of
that day's all-day heart-rate samples as the proxy (`wearable_parsers.py:310` for Garmin,
`:473` for Apple). The absolute minimum is too noisy; the mean is inflated by activity.

Garmin skips the estimate on any date that already has a `monitoring_hr_data.resting_heart_rate`
value; Apple only runs the fallback when the export contains **no** `RestingHeartRate` records at
all — a coarser condition, so an export with sparse resting-HR coverage gets no fill-in on the
missing days.

**Sleep duration — span reconstruction.** Apple sums `HKCategoryTypeIdentifierSleepAnalysis`
records whose value contains `asleep`, discarding `InBed`. Garmin sorts `sleep_level` entries by
timestamp and sums the gaps following any entry with level > 0 (1=light, 2=deep, 3=REM), capping
each span at 4h to filter recording gaps. Both attribute the night to the **start** date.

---

## Artifact filtering

Values outside `WEARABLE_ARTIFACT_BOUNDS` (`mappings.py:112`) are discarded *before* the OMOP
row is created (`views.py:3666`) — rejected readings are never persisted, so the OMOP tables
hold only physiologically plausible values.

| Metric | Lower | Upper | Metric | Lower | Upper |
|---|---|---|---|---|---|
| `spo2` | 70 | 100 % | `walking_speed` | 0.5 | 15 km/hr |
| `resting_hr` | 20 | 300 /min | `walking_step_length` | 20 | 200 cm |
| `hrv_sdnn` | 1 | 300 ms | `walking_double_support_pct` | 5 | 80 % |
| `respiratory_rate` | 4 | 60 /min | `walking_hr_avg` | 30 | 220 /min |
| `steps` | 0 | 100,000 | `flights_climbed` | 0 | 200 |
| `active_minutes` | 0 | 1,440 min | `active_energy` | 0 | 10,000 kcal |
| `sleep_duration` | 0 | 24 h | `basal_energy` | 500 | 5,000 kcal |
| `vo2_max` | 10 | 100 | `body_mass` | 20 | 300 kg |
| `distance` | 0 | 100 km | | | |

---

## Row shape written today

Per `views.py:3679` (observation) and `:3711` (measurement):

| Column | Value | Notes |
|---|---|---|
| `person_id` | uploading patient's Person | |
| `measurement_concept_id` / `observation_concept_id` | LOINC concept via `_cc_by_loinc()` | **skipped entirely if unresolved** |
| `measurement_date` / `observation_date` | sample date | date only; no `_datetime` |
| `*_type_concept_id` | 32883, falling back to 32856 | see Gap 4 |
| `value_as_number` | daily aggregate, rounded to 2dp | |
| `*_source_value` | the LOINC code string | lets `_get_wearable_data` match rows whose concept FK is null |
| `unit_source_value` | UCUM string from the table above | `unit_concept_id` **not set** — Gap 3 |

Dedup is `(metric_key, date, round(value, 2))` against existing rows (`views.py:3671`), so
re-uploading an overlapping export is safe. Rows carry `_skip_patient_record_refresh = True` and
`refresh_patient_record(person)` is called once after the bulk insert.

The read path (`patient_record_service._get_wearable_data`, `:2473`) matches on **either**
`measurement_concept__concept_code` **or** `measurement_source_value`, then requires
`WEARABLE_MIN_VALID_DAYS` (7) distinct valid days before emitting a `PatientRecord` column.

---

## Gaps and proposed work

### Gap 1 — 9 of 17 LOINC concepts are never seeded (blocking)

`seed_omop_concepts.py:270` seeds only six wearable concepts (9001019–9001024) plus `spo2` and
`body_mass`, which resolve against Athena. The nine metrics added in commit `401956d` have no
concept row. Verified against `promop_dev`:

```
vo2_max 94122-9, distance 41953-1, walking_speed 41909-3, walking_step_length 96341-8,
walking_double_support_pct 96343-4, walking_hr_avg 89270-3, flights_climbed 96340-0,
active_energy 55424-6, basal_energy 41982-0          → *** MISSING ***
```

This is not cosmetic. `views.py:3662` reads:

```python
concept = loinc_concepts.get(sample.metric_key)
if concept is None:
    continue
```

Parsed samples for all nine are **silently discarded** — no row, no warning, no counter. The
upload reports success and the `PatientRecord` columns stay null. On any database seeded only by
`seed_omop_concepts`, the nine new metrics cannot be ingested at all.

**Proposed:** extend the wearable block with concept_ids 9001025–9001033 (next free; the local
block currently ends at 9001024), domain `Measurement`, vocabulary `LOINC`, concept_class
`Clinical Observation`, standard_concept `S`. Prefer real Athena concept_ids where they exist —
`spo2` and `body_mass` already resolve to genuine ones (40762499, 3025315) and should not be
shadowed by local mints.

Independently, the silent `continue` should log at WARNING and surface an
`unmapped_metrics` count in the upload response, so a missing concept is visible rather than
indistinguishable from "device exported no data".

### Gap 2 — `sleep_duration` domain contradicts its write target

Seeded as `domain_id='Measurement'` (`seed_omop_concepts.py:275`) but written to `observation`
(`views.py:3677`). OMOP convention is that a concept's `domain_id` determines its table, so this
row violates the CDM's own routing rule and will fail Achilles/DQD domain checks.

**Proposed:** change the seeded `domain_id` to `Observation` and keep the write target. Requires
a data migration for any existing rows only if the concept row itself is rewritten — the
`observation` rows already sit in the right table.

### Gap 3 — `unit_concept_id` is never populated

Only `unit_source_value` is set. Standard OMOP consumers, and any downstream ETL to a research
warehouse, read `unit_concept_id`.

**Proposed:** add a `WEARABLE_UNIT_CONCEPT` dict in `mappings.py` beside `WEARABLE_LOINC`,
resolve it once at upload, and set `unit_concept_id`. The UCUM strings needing a concept are
`%`, `/min`, `ms`, `kcal`, `kg`, `min`, `h`, `km`, `cm`, `km/hr`, `mL/kg/min`, `/d`, and
`{flights}`.

Every one of these must be looked up in Athena rather than hard-coded from memory. The UCUM
units are not currently loaded in `promop_dev` — the standard unit vocabulary is absent from the
partial Athena load — so this work depends on `load_athena_vocabularies` having been run with
the unit domain included. Confirm each id against the loaded `concept` table before writing it
into `mappings.py`.

### Gap 4 — type concept 32883 is unverified

`views.py:3625` uses concept 32883 with a comment reading "wearable device = 32883 / Patient
self-report", falling back to 32856. Verified against `promop_dev`: **32883 does not exist in the
concept table**, so every wearable row written there is currently typed 32856 — whose
`concept_name` is literally `Lab` (vocabulary `Type Concept`). Wearable readings are being
provenance-labelled as laboratory results.

The pairing of 32883 with "patient self-report" should be confirmed against Athena; a wearable
reading is neither a lab result nor, strictly, self-reported. If a device-derived type concept
exists it is the correct choice, it should be seeded so the lookup resolves, and the silent
fallback to `Lab` should be dropped in favour of an explicit failure.

### Gap 5 — Garmin has no adapter for six metrics

`walking_speed`, `walking_step_length`, `walking_double_support_pct`, `walking_hr_avg`,
`flights_climbed`, and `body_mass` are Apple-only today. The normalization design is sound —
these are canonical metrics with a LOINC and a unit — but Garmin currently contributes nothing,
so a Garmin-only patient has permanent nulls in six columns.

FIT sources exist for at least four and should be added to `parse_garmin_fit`:

| Metric | Candidate FIT source |
|---|---|
| `flights_climbed` | `monitoring.ascent` / `total_ascent` (metres → flights, ÷ ~3.05 m) |
| `walking_speed` | `session.avg_speed` / `enhanced_avg_speed` on walk-type sessions (m/s → km/hr) |
| `walking_hr_avg` | `session.avg_heart_rate` filtered to walking sport type |
| `body_mass` | `weight_scale.weight` (Garmin Index scale) or `user_profile.weight` |

`walking_step_length` and `walking_double_support_pct` have no FIT equivalent — Garmin's Running
Dynamics reports ground contact time and vertical oscillation, which are not the same
measurements and should **not** be mapped onto these LOINCs. Leave them Apple-only.

### Gap 6 — daily-total units are dimensionally loose

`distance` is stored as `km` and `steps` as `/d`, but both values are daily totals. `distance`
should arguably be `km/d` to match. This affects nothing today because every consumer reads
`WEARABLE_LOINC` and knows the semantics, but it will confuse any external OMOP consumer.

### Gap 7 — no `measurement_datetime`, provider, or visit

Only the date is stored. For sub-daily analyses (nocturnal SpO2 desaturation, circadian HR) the
current model cannot support the query. Deferred deliberately — the daily grain is what the
30-day `PatientRecord` summaries need — but noted so the limitation is not rediscovered later.

---

## Adding a new wearable metric

1. `mappings.py` — add to `WEARABLE_LOINC` and `WEARABLE_ARTIFACT_BOUNDS`.
2. `seed_omop_concepts.py` — seed the LOINC concept, **or ingestion silently no-ops** (Gap 1).
3. `wearable_parsers.py` — add the Apple `_APPLE_TYPE_MAP` entry and the Garmin FIT handler;
   add the key to the sum-vs-mean list at `:328` and `:489` if it is cumulative.
4. `views.py` — add the `unit_map` entry in `upload_wearable`.
5. `patient_record_service.py` — add the 30-day aggregation.
6. `models.py` + migration — add the `PatientRecord` column.
7. `frontend/src/types/patient.ts` + `WearableTab.tsx` — expose it.
8. Tests at every layer, per CLAUDE.md.
