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

> ⚠️ **The codes above document what the code does today, not what is correct.** Four resolve to
> unrelated concepts and three are not valid LOINC at all — see Gap 1 and Gap 1b. Do not copy
> `walking_speed`, `walking_hr_avg`, `basal_energy`, `flights_climbed`, `walking_step_length`, or
> `walking_double_support_pct` from this table into new work.

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

> **Environments audited.** All findings below were verified against **staging**
> (`ctomop_dev`, `promop-staging.onrender.com`, full Athena load: 1,979,424 concepts /
> 277,790 LOINC rows) and against local `promop_dev` (partial `seed_omop_concepts` set only).
> Staging is the reference environment for this work. Production trails the `dev` branch by a
> long way and is reconciled by a separate `dev` → `main` merge; it is out of scope here and is
> not a prerequisite for any fix below.
>
> Note that CLAUDE.md's Database Selection table is stale: it references a `STAGING_DATABASE_URL`
> that is not defined in `.env`, and a production host that does not match the one `DATABASE_URL`
> actually points at — which is staging (`ctomop_dev`).

### Gap 1 — four LOINC codes resolve to unrelated concepts (blocking)

Where a full Athena vocabulary is loaded, 14 of 17 codes resolve — but four resolve to the
**wrong concept**, so wearable data is filed under unrelated clinical meanings:

| Metric | Code in `WEARABLE_LOINC` | What it actually is |
|---|---|---|
| `walking_speed` | 41909-3 | **Deprecated Body mass index (BMI)** — also `standard_concept=None` |
| `walking_hr_avg` | 89270-3 | **Body mass index (BMI) [Ratio] Estimated** |
| `basal_energy` | 41982-0 | **Percentage of body fat Measured** |
| `active_energy` | 55424-6 | Calories burned in unspecified time **Pedometer** — approximate |

This is worse than dropping the data: the rows look valid, and any query trusting
`measurement_concept_id` reads walking speed and basal energy as BMI and body fat.

Verified replacements: `walking_speed` → **41957-2** (Walking speed 24 hour mean Calculated,
std=S) — same 419xx family as `distance` 41953-1, which is correct, suggesting a mis-picked
neighbour; `flights_climbed` → **100304-5** (Flights climbed [#] Reporting Period, std=S).
For `basal_energy` the only candidate is 50042-1 "Basal metabolic rate **index**", which may not
be kcal/day — review before adopting.

### Gap 1b — three codes are not valid LOINC, and minting must be quarantined

`96340-0`, `96341-8`, `96343-4` do not exist in the loaded release. This is not a vocabulary
vintage problem — 111 concepts in the `963xx` range are present, and searching LOINC for
`step length` or `double support` returns nothing at all.

Where a metric has no LOINC, the local mint **must** follow the project's quarantine convention
(`omop_core/models.py:566`): `source='HealthKey'`, in an `HK-*` vocabulary, with an `HK-*`-shaped
`concept_code`. A new `HK-Wearable` vocabulary is the right home for `walking_step_length`,
`walking_double_support_pct`, and `walking_hr_avg`.

**Never mint a real LOINC code under `vocabulary_id='LOINC'`.** Doing so creates a duplicate
`(vocabulary_id, concept_code)` pair, and `concept_by_loinc` resolves duplicates arbitrarily
(`concept_cache.py:39`, `.first()` with no ordering). All six existing wearable mints
(9001019–9001024) already collide with genuine Athena concepts this way, and all 24 `900xxxx`
mints are written `source=NULL` because `seed_omop_concepts._c()` has no `source` parameter.
Tracked separately in **#415** — 115 duplicate pairs table-wide.

### How to seed wearable concepts correctly

Local dev and test databases have no Athena load, so the concepts must be seeded for ingestion to
work there at all. The rule that keeps that safe:

> **Seed the genuine Athena `concept_id`. Never invent a new one for a code Athena already owns.**

`seed_omop_concepts` applies rows with `get_or_create(concept_id=..., defaults=row)` — keyed on
`concept_id`. So a row seeded with the real id is *created* on a bare database and *matches the
existing row* on an Athena-loaded one. No duplicate can arise, on any environment, by
construction. These rows are genuine external concepts, so `source` correctly stays NULL.

Minting a fresh `900xxxx` id for the same code is what produces the duplicate pair and the
arbitrary resolution described in #415.

| Metrics | Action |
|---|---|
| The 11 codes that are already correct | Seed the real Athena concept_id |
| `walking_speed`, `flights_climbed` | Correct the code first (41957-2, 100304-5), then seed the real id |
| `basal_energy` | Resolve the correct code first — 41982-0 is body-fat-percentage |
| `walking_step_length`, `walking_double_support_pct`, `walking_hr_avg` | No LOINC exists — mint in `HK-Wearable` with `source='HealthKey'` and an `HK-*` concept_code |
| Existing 9001019–9001024 | Retire; remap dependent `measurement`/`observation` FKs to the Athena ids |

This also requires adding a `source` parameter to `_c()` (`seed_omop_concepts.py:94`), which
currently cannot express `'HealthKey'` at all.

### Gap 1c — unresolvable concepts are skipped silently

`views.py:3661`:

```python
concept = loinc_concepts.get(sample.metric_key)
if concept is None:
    continue
```

No log, no counter. On a database seeded only by `seed_omop_concepts` — which is every local dev
and test database — nine metrics have no concept at all and are discarded while the upload still
returns HTTP 200 with a success count. The failure is indistinguishable from "the device
exported no data". This should log at WARNING and return an `unmapped_metrics` count.

### Gap 2 — `sleep_duration` domain contradicts its write target

Seeded as `domain_id='Measurement'` (`seed_omop_concepts.py:275`) but written to `observation`
(`views.py:3677`). OMOP convention is that a concept's `domain_id` determines its table, so this
row violates the CDM's own routing rule and will fail Achilles/DQD domain checks.

Genuine Athena 93832-4 (concept_id 1002368) carries `domain_id='Observation'`, which **confirms
the write target is correct and the local seed is the wrong side**. Seeding the real concept_id
per the recipe above fixes this automatically — the hand-written `Measurement` domain disappears
along with the mint.

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
self-report", falling back to 32856. Both are wrong:

| Concept | Actual `concept_name` (staging) |
|---|---|
| 32883 | **Survey** |
| 32856 | **Lab** |

A wearable reading is neither a survey response nor a laboratory result. Worse, 32883 is absent
from any database seeded only by `seed_omop_concepts` (verified on `promop_dev`), so the fallback
fires there and wearable rows are typed `Lab` outright.

**Proposed:** identify the correct device/EHR-derived type concept in Athena, seed it by its real
concept_id, and drop the silent fallback in favour of an explicit failure — mislabelling
provenance is worse than refusing to write.

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
