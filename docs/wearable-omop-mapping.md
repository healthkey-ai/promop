# Wearable Data → OMOP Measurement / Observation Mapping

How Apple Watch and Garmin metrics are normalized into OMOP CDM v5.4 `measurement` and
`observation` rows, and what still needs to be built.

Companion documents:
- [concept-mapping.md](concept-mapping.md) — general LOINC/SNOMED/HemOnc → OMOP concept resolution
- [apple-wearable-patientinfo-fields.md](apple-wearable-patientinfo-fields.md) — the original
  issue proposing the derived `PatientRecord` summary columns that sit *above* this layer

---

## Design principle: one canonical metric, two device adapters

Device-specific vocabulary must not leak past the parser boundary. Apple exports HealthKit
type identifiers; Garmin exports FIT message/field pairs. Both are translated to a small set of
**canonical metric keys** at parse time, and only the canonical key reaches the OMOP writer.

```
Apple export.zip ──► parse_apple_health_export ──┐
                     (_APPLE_TYPE_MAP)           │
                                                 ├──► WearableSample ──► one concept ──► Measurement
Garmin .fit ────────► parse_garmin_fit ──────────┘   (metric_key,       (LOINC or       or Observation
                     (FIT message handlers)           date, value)       HK-Wearable)    (by domain_id)
```

`WearableSample` (`omop_core/services/wearable_parsers.py:18`) is the normalization contract —
`(metric_key, date, value)`, nothing else. The consequence is that **a metric is stored
identically no matter which device produced it**: same concept, same units, same table. A query
for resting heart rate never has to know whether the patient wears an Apple Watch or a Fenix.

Both parsers reduce to **one value per metric per calendar day** before returning. Cumulative
metrics (steps, active minutes, sleep, distance, flights, active energy, basal energy) are summed
across the day; rates and percentages (HR, SpO2, HRV, respiratory rate, walking speed, step
length, double support, walking HR, VO2 max, body mass) are averaged. The two parsers share the
same sum-vs-mean list, at `wearable_parsers.py:328` (Garmin) and `:489` (Apple) — a metric added
to one must be added to the other.

---

## The normalization table

The canonical registry is `WEARABLE_CONCEPT_CODE` (`omop_core/services/mappings.py:99`). One row
per metric key; the concept code is the join point between the two device adapters.

Despite most entries being LOINC, **this map is not LOINC-only** — four metrics have no LOINC
equivalent and are minted locally under the `HK-Wearable` vocabulary. Concept resolution is
therefore scoped by `(vocabulary_id, concept_code)` via `WEARABLE_CONCEPT_VOCAB`
(`mappings.py:122`); a bare `concept_code` is ambiguous, since 852 codes are reused across
vocabularies.

| Metric key | Code | Vocab | Seeded concept_id | OMOP table | UCUM unit | Daily agg | Apple HealthKit type | Garmin FIT source |
|---|---|---|---|---|---|---|---|---|
| `steps` | 55423-8 | LOINC | 40758552 | **observation** | `/d` | sum | `HKQuantityTypeIdentifierStepCount` | `monitoring.steps` → `.cycles` (max — cumulative counter); fallback `session.total_steps`/`total_cycles` |
| `active_minutes` | 55411-3 | LOINC | 40758540 | **observation** | `min` | sum | `HKQuantityTypeIdentifierAppleExerciseTime` | `monitoring.active_time` ÷ 60; `session.total_timer_time` ÷ 60 |
| `resting_hr` | 40443-4 | LOINC | 3040891 | measurement | `/min` | mean | `HKQuantityTypeIdentifierRestingHeartRate` (or derived, below) | `monitoring_hr_data.resting_heart_rate` (or derived, below) |
| `hrv_sdnn` | 80404-7 | LOINC | 21491502 | measurement | `ms` | mean | `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` | `hrv_status_summary.weekly_average`/`last_night_average`; `hrv_value.value`; legacy `hrv.weekly_average`/`sdnn` |
| `spo2` | 59408-5 | LOINC | 40762499 | measurement | `%` | mean | `HKQuantityTypeIdentifierOxygenSaturation` | `spo2_data.reading_spo2`; fallback `session.saturated_hemoglobin_percent` |
| `respiratory_rate` | 9279-1 | LOINC | 3024171 | measurement | `/min` | mean | `HKQuantityTypeIdentifierRespiratoryRate` | `respiration_rate.respiration_rate`; fallback `session.avg_respiration_rate` |
| `sleep_duration` | 93832-4 | LOINC | 1002368 | **observation** | `h` | sum | `HKCategoryTypeIdentifierSleepAnalysis` (asleep spans only) | `sleep_level` timestamp spans; fallback `sleep_data.total_timer_time` ÷ 3600 |
| `vo2_max` | 94122-9 | LOINC | 1002246 | measurement | `mL/kg/min` | mean | `HKQuantityTypeIdentifierVO2Max` | `session.enhanced_max_oxygen_consumption` → `vo2_max` |
| `distance` | 41953-1 | LOINC | 3031111 | measurement | `km` | sum | `HKQuantityTypeIdentifierDistanceWalkingRunning` | `monitoring.distance` ÷ 1000; `session.total_distance` ÷ 1000 |
| `walking_speed` | 41957-2 | LOINC | 3032289 | measurement | `km/hr` | mean | `HKQuantityTypeIdentifierWalkingSpeed` | *(no source — see Gap A)* |
| `walking_step_length` | HK-WEAR-STEP-LENGTH | **HK-Wearable** | 2029606350 | measurement | `cm` | mean | `HKQuantityTypeIdentifierWalkingStepLength` | *(no source)* |
| `walking_double_support_pct` | HK-WEAR-DBL-SUPPORT | **HK-Wearable** | 2029606351 | measurement | `%` | mean | `HKQuantityTypeIdentifierWalkingDoubleSupportPercentage` | *(no source)* |
| `walking_hr_avg` | HK-WEAR-WALK-HR | **HK-Wearable** | 2029606352 | measurement | `/min` | mean | `HKQuantityTypeIdentifierWalkingHeartRateAverage` | *(no source — see Gap A)* |
| `flights_climbed` | 100304-5 | LOINC | 1761351 | **observation** | `{flights}` | sum | `HKQuantityTypeIdentifierFlightsClimbed` | *(no source — see Gap A)* |
| `active_energy` | 93819-1 | LOINC | 1001786 | measurement | `kcal` | sum | `HKQuantityTypeIdentifierActiveEnergyBurned` | `monitoring.active_calories`; `session.total_calories` |
| `basal_energy` | HK-WEAR-BASAL-ENERGY | **HK-Wearable** | 2029606353 | measurement | `kcal` | sum | `HKQuantityTypeIdentifierBasalEnergyBurned` | `monitoring_info.resting_metabolic_rate` |
| `body_mass` | 29463-7 | LOINC | 3025315 | measurement | `kg` | mean | `HKQuantityTypeIdentifierBodyMass` | *(no source — see Gap A)* |

Every code here has been verified against Athena to resolve to a concept whose `concept_name`
matches the metric. Do not add an entry without doing the same check — four codes in the original
version of this map resolved to BMI and body-fat-percentage concepts, and three were not valid
LOINC at all. That defect is fixed; the section below explains the rules that keep it fixed.

`spo2` (59408-5 / 40762499) and `body_mass` (29463-7 / 3025315) are seeded in the **vitals** block
of `seed_omop_concepts`, not the wearable block. They must not be seeded twice — a second row for
the same `(vocabulary_id, concept_code)` is exactly the duplication the seeding rules exist to
prevent.

### Table routing is by `domain_id`, not by a hard-coded metric list

Four metrics — `steps`, `active_minutes`, `sleep_duration`, `flights_climbed` — resolve to
**Observation-domain** concepts and are therefore written to `observation`. The other thirteen go
to `measurement`.

The write path does not hard-code that split. It reads `concept.domain_id` at runtime
(`patient_portal/api/views.py:3672`), so routing stays correct automatically if a code changes.
`WEARABLE_OBSERVATION_METRICS` (`mappings.py:131`) mirrors the same four keys, but exists only for
tests and fixtures — it is not consulted by the runtime writer.

This follows OMOP's own rule: a concept's `domain_id` determines its table. Arguments could be
made on clinical grounds for placing step counts in `measurement` — every non-sleep metric is a
numeric quantity with a UCUM unit, and `measurement` alone offers `unit_concept_id`,
`range_low`/`range_high`, and `operator_concept_id`. But the CDM does not leave that to local
judgement, and following the vocabulary's domain is what keeps the data passing Achilles/DQD
domain checks.

The read path is built to be indifferent to the split: `_get_wearable_data` merges `measurement`
and `observation` rows into a single index keyed by concept code
(`patient_record_service.py:2547`), so a metric reads the same way regardless of which table its
domain routed it to.

---

## Local concept minting rules

Where a metric has no LOINC, the local mint **must** follow the project's quarantine convention
(`omop_core/models.py:566`) — and all four wearable mints do:

- `vocabulary_id='HK-Wearable'` (declared in `seed_omop_concepts._VOCABULARIES`)
- `source='HealthKey'`
- an `HK-*`-shaped `concept_code`
- `concept_id >= 2,000,000,000` — OHDSI reserves that range for locally-authored concepts, and
  Athena never allocates there

The four ids are allocated contiguously from `_HK_WEARABLE_ID_BASE = 2_029_606_350`
(`seed_omop_concepts.py:140`), continuing the existing `HK-Labs` block. New wearable mints should
continue upward from there.

**Never mint a real LOINC code under `vocabulary_id='LOINC'`.** Doing so creates a duplicate
`(vocabulary_id, concept_code)` pair, and `concept_by_vocab` resolves duplicates arbitrarily
(`concept_cache.py:39` — `.first()` with no ordering). This is the defect tracked in **#415**.

For codes that Athena *does* own, the rule is:

> **Seed the genuine Athena `concept_id`. Never invent a new one for a code Athena already owns.**

`seed_omop_concepts` applies rows with `get_or_create(concept_id=..., defaults=row)` — keyed on
`concept_id`. A row seeded with the real id is therefore *created* on a bare database and
*matches the existing row* on an Athena-loaded one. No duplicate can arise, on any environment, by
construction. These rows are genuine external concepts, so `source` correctly stays NULL.

Local dev and test databases have no Athena load, so these concepts must be seeded for ingestion
to work there at all.

### Cleaning up rows written under the old mapping

Six retired `900xxxx` mints (`steps` 9001019 through `sleep_duration` 9001024) were used before
this mapping was corrected. Rows written under them, and rows carrying a code the old mapping used
wrongly, are removed by:

```bash
python manage.py purge_broken_wearable_rows              # dry run (default)
python manage.py purge_broken_wearable_rows --apply
python manage.py purge_broken_wearable_rows --apply --keep-mints
```

The command **deletes rather than migrates**, because wearable rows are reproducible — re-uploading
the device export regenerates them correctly under the corrected mapping. An earlier
`remap_wearable_concepts` tried to move rows across tables and silently dropped
`measurement_datetime`, `is_erroneous`, provider, visit, and the source/unit/value concept columns
in the process.

It deliberately does **not** match `body_mass` (29463-7) or `spo2` (59408-5) by code: both codes
were always correct, and both are also written by the vitals and FHIR ingestion paths, so matching
them would delete non-wearable clinical data. Rows for those two metrics are removed only when
they sit on a retired mint, which is unambiguous.

---

## Derived values (not directly exported by either device)

Two metrics are computed rather than read, and this is where the two adapters deliberately
converge on the same algorithm so the stored values stay comparable:

**Resting heart rate — 10th-percentile fallback.** Neither device reliably exports resting HR.
When no dedicated resting-HR record exists for a day, both parsers take the 10th percentile of
that day's all-day heart-rate samples as the proxy (`wearable_parsers.py:310` for Garmin,
`:473` for Apple). The absolute minimum is too noisy; the mean is inflated by activity.

Garmin skips the estimate on any date that already has a `monitoring_hr_data.resting_heart_rate`
value. Apple only runs the fallback when the export contains **no** `RestingHeartRate` records at
all — a coarser condition, so an export with sparse resting-HR coverage gets no fill-in on the
missing days. Apple additionally requires ≥ 5 heart-rate readings on a day before estimating.

**Sleep duration — span reconstruction.** Apple sums `HKCategoryTypeIdentifierSleepAnalysis`
records whose value contains `asleep`, discarding `InBed`. Garmin sorts `sleep_level` entries by
timestamp and sums the gaps following any entry with level > 0 (1=light, 2=deep, 3=REM), capping
each span at 4h to filter recording gaps. Both attribute the night to the **start** date.

---

## Artifact filtering

Values outside `WEARABLE_ARTIFACT_BOUNDS` (`mappings.py:136`) are discarded *before* the OMOP
row is created (`views.py:3710`) — rejected readings are never persisted, so the OMOP tables
hold only physiologically plausible values. The same bounds are applied again on the read path
(`patient_record_service.py:2564`), so rows written before a bound was tightened cannot leak into
a summary.

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

Per `views.py:3725` (observation) and `:3738` (measurement):

| Column | Value | Notes |
|---|---|---|
| `person_id` | uploading patient's Person | resolved from `request.user` via `PatientUser` |
| `measurement_concept_id` / `observation_concept_id` | concept via `_cc_by_vocab(vocab, code)` | skipped if unresolved, but **logged and counted** |
| `measurement_date` / `observation_date` | sample date | date only; no `_datetime` — Gap D |
| `*_type_concept_id` | 32883, falling back to 32856 | both wrong — Gap C |
| `value_as_number` | daily aggregate, rounded to 2dp | |
| `*_source_value` | the concept code string | lets `_get_wearable_data` match rows whose concept FK is null |
| `unit_source_value` | UCUM string from the table above | `unit_concept_id` **not set** — Gap B |

Unresolvable concepts are reported rather than dropped silently: `upload_wearable` logs a WARNING
at `views.py:3629` naming the affected metrics, and the HTTP response carries `unmapped_samples`
and `unmapped_metrics` alongside `samples_created` and `duplicates_skipped`. A missing concept is
therefore distinguishable from "the device exported no data for this metric" — previously the two
were identical, and every local dev database silently discarded nine metrics while returning
HTTP 200 with a success count.

Dedup is `(metric_key, date, round(value, 2))` against existing rows (`views.py:3715`), reading
whichever table the concept's domain routes it to, so re-uploading an overlapping export is safe.
Rows carry `_skip_patient_record_refresh = True` and `refresh_patient_record(person)` is called
once after the bulk insert. Each upload is recorded in `WearableUpload` for patient-visible
history.

---

## The `PatientRecord` projection above this layer

`_get_wearable_data` (`patient_record_service.py:2484`) turns the daily OMOP rows into 21 flat
30-day summary columns on `PatientRecord` (`models.py:2420-2440`). The rules:

- **Row matching** is on **either** `*_concept__concept_code` **or** `*_source_value`, so a row
  whose concept FK is null or unmapped is still read.
- **Recency window.** Only rows from the last 90 days are considered; if none exist, the query
  re-runs unbounded so historical or synthetic data still produces a summary.
- **Anchor date.** The window is the 30 days ending at the most recent day that has at least
  `WEARABLE_MIN_VALID_DAYS` (7) valid days behind it. If no such day exists, the latest valid day
  is used. `wearable_last_sync_at` records the anchor.
- **Coverage.** `wearable_coverage_ratio_30d` is the union of valid days across all metrics ÷ 30,
  so a consumer can judge whether the other 19 columns are trustworthy.
- **Aggregation.** Median for steps; mean of daily means for rates; mean of daily totals for
  cumulative metrics; `min` for `oxygen_saturation_min_30d`, since one low SpO2 reading is
  clinically significant in a way an average is not.
- **Trend.** `activity_trend_30d` compares the mean daily steps of the window's first and second
  halves, requiring ≥ 7 valid days in *each* half; ≥ +10% is `improving`, ≤ −10% is `declining`,
  otherwise `stable`. It defaults to `insufficient_data` rather than null.

---

## Gaps and proposed work

> **Environments.** Concept resolution claims here were verified against **staging**
> (`ctomop_dev`, `promop-staging.onrender.com`, full Athena load: 1,979,424 concepts /
> 277,790 LOINC rows) and against local `promop_dev` (partial `seed_omop_concepts` set only).
> Staging is the reference environment for this work.
>
> Note that CLAUDE.md's Database Selection table is stale: it references a `STAGING_DATABASE_URL`
> that is not defined in `.env`, and a production host that does not match the one `DATABASE_URL`
> actually points at — which is staging (`ctomop_dev`).

### Gap A — Garmin has no adapter for six metrics

`walking_speed`, `walking_step_length`, `walking_double_support_pct`, `walking_hr_avg`,
`flights_climbed`, and `body_mass` are Apple-only today. The normalization design is sound — these
are canonical metrics with a concept and a unit — but Garmin contributes nothing, so a Garmin-only
patient has permanent nulls in six `PatientRecord` columns.

FIT sources exist for four of them and should be added to `parse_garmin_fit`:

| Metric | Candidate FIT source |
|---|---|
| `flights_climbed` | `monitoring.ascent` / `total_ascent` (metres → flights, ÷ ~3.05 m) |
| `walking_speed` | `session.avg_speed` / `enhanced_avg_speed` on walk-type sessions (m/s → km/hr) |
| `walking_hr_avg` | `session.avg_heart_rate` filtered to walking sport type |
| `body_mass` | `weight_scale.weight` (Garmin Index scale) or `user_profile.weight` |

`walking_step_length` and `walking_double_support_pct` have no FIT equivalent — Garmin's Running
Dynamics reports ground contact time and vertical oscillation, which are not the same
measurements and should **not** be mapped onto these concepts. Leave them Apple-only.

### Gap B — `unit_concept_id` is never populated

Only `unit_source_value` is set. Standard OMOP consumers, and any downstream ETL to a research
warehouse, read `unit_concept_id`.

**Proposed:** add a `WEARABLE_UNIT_CONCEPT` dict in `mappings.py` beside `WEARABLE_CONCEPT_CODE`,
resolve it once at upload, and set `unit_concept_id`. The UCUM strings needing a concept are
`%`, `/min`, `ms`, `kcal`, `kg`, `min`, `h`, `km`, `cm`, `km/hr`, `mL/kg/min`, `/d`, and
`{flights}`.

Every one of these must be looked up in Athena rather than hard-coded from memory. The UCUM units
are not currently loaded in `promop_dev` — the standard unit vocabulary is absent from the
partial Athena load — so this work depends on `load_athena_vocabularies` having been run with
the unit domain included. Confirm each id against the loaded `concept` table before writing it
into `mappings.py`.

### Gap C — type concept 32883 is wrong

`views.py:3635` uses concept 32883 with a comment reading "wearable device = 32883 / Patient
self-report", falling back to 32856. Both are wrong:

| Concept | Actual `concept_name` (staging) |
|---|---|
| 32883 | **Survey** |
| 32856 | **Lab** |

A wearable reading is neither a survey response nor a laboratory result. Worse, 32883 is absent
from any database seeded only by `seed_omop_concepts` (verified on `promop_dev`), so the fallback
fires there and wearable rows are typed `Lab` outright.

**Proposed:** identify the correct device/patient-generated type concept in Athena, seed it by its
real concept_id, and drop the silent fallback in favour of an explicit failure — mislabelling
provenance is worse than refusing to write.

### Gap D — no `measurement_datetime`, provider, or visit

Only the date is stored. For sub-daily analyses (nocturnal SpO2 desaturation, circadian HR) the
current model cannot support the query. Deferred deliberately — the daily grain is what the
30-day `PatientRecord` summaries need — but noted so the limitation is not rediscovered later.

### Gap E — eleven derived `PatientRecord` columns are client-writable

`PatientRecordSerializer.read_only_fields` (`patient_portal/api/serializers.py:276`) protects the
original ten wearable columns, with the comment "Wearable summaries are written by the device-sync
service, never by the client API." The eleven columns added since were never added to that list:

`oxygen_saturation_avg_30d`, `vo2_max_avg_30d`, `distance_km_per_day_30d`,
`walking_speed_avg_30d`, `walking_step_length_avg_30d`, `walking_double_support_pct_avg_30d`,
`walking_hr_avg_30d`, `flights_climbed_per_day_30d`, `active_energy_per_day_30d`,
`basal_energy_per_day_30d`, `body_mass_avg_30d`.

A client can PATCH any of them, and the value survives until the next `refresh_patient_record`
overwrites it — so a derived column can disagree with the OMOP rows it claims to summarize, for an
unbounded window. Add all eleven to `read_only_fields`, and add a test asserting that every
`*_30d` column and every `wearable_*` column on `PatientRecord` is read-only, so the next column
added cannot repeat the omission.

### Gap F — daily-total units are dimensionally loose

`distance` is stored as `km` and `steps` as `/d`, but both values are daily totals. `distance`
should arguably be `km/d` to match. This affects nothing today because every consumer reads
`WEARABLE_CONCEPT_CODE` and knows the semantics, but it will confuse any external OMOP consumer.

### Gap G — `hrv_sdnn` may be conflating SDNN and RMSSD (#438)

LOINC 80404-7 is *R-R interval.standard deviation* — specifically **SDNN**. Apple's source
identifier is unambiguously SDNN (`HKQuantityTypeIdentifierHeartRateVariabilitySDNN`), so that
side of the map is sound.

The Garmin side is not verified. `parse_garmin_fit` files
`hrv_status_summary.weekly_average`/`last_night_average` (`wearable_parsers.py:231`) and the
legacy `hrv` message (`:277`) under `hrv_sdnn`, and names the local variable `sdnn`. But Garmin's
HRV Status feature is documented by Garmin as **RMSSD**-based. RMSSD and SDNN are different
statistics over the same R-R series; they are not interchangeable and produce different values
from identical input.

If that is right, Garmin HRV rows are filed under a concept that means something else — the same
class of defect as the original `walking_speed`→BMI mapping, and equally invisible to any query
that trusts `measurement_concept_id`.

**Before any further HRV work:**

1. Confirm what `hrv_status_summary` actually reports, against Garmin's FIT SDK / HRV Status
   documentation. `hrv_value.value` (5-minute readings, `:242`) needs the same check separately —
   it may not be the same statistic as the summary messages.
2. If it is RMSSD, add a second canonical metric (`hrv_rmssd`) with its own verified Athena
   concept, repoint the Garmin adapter at it, and give it its own `PatientRecord` column. Do not
   alias it onto 80404-7, and do not average the two into one summary.
3. Existing Garmin-sourced `hrv_sdnn` rows would need the `purge_broken_wearable_rows` treatment —
   delete and re-upload, since the corrected value is reproducible from the export.

This gap becomes blocking as soon as a Fitbit, Whoop, or Oura adapter is written: all three report
RMSSD-derived HRV, so a `hrv_rmssd` concept is needed regardless, and resolving it correctly for
those vendors settles Garmin at the same time.

---

## Adding a new device adapter

The parser boundary is what keeps a new vendor cheap. An adapter's only obligation is to emit
`WearableSample(metric_key, date, value)` tuples using the existing canonical metric keys, at one
value per metric per calendar day, in the units given in the normalization table.

Everything below that boundary — concept resolution, domain routing, artifact bounds, dedup,
`PatientRecord` aggregation — is vendor-independent and must not be touched to accommodate a
device.

What a new vendor does touch:

| Layer | Change |
|---|---|
| `wearable_parsers.py` | The new parser. Reuse the shared sum-vs-mean list (`:328`, `:489`) — don't invent a third copy. |
| `views.py:3568` | `device_type` allow-list, currently the literal `('garmin', 'apple')` |
| `views.py:3578-3581` | Per-device file-extension validation |
| `views.py:3604` | Parser dispatch, plus the error-message ternary at `:3612` |
| `WearableTab.tsx:56` | `detectDeviceType`, and the upload-history label at `:364` — a two-way ternary that renders any third device as "Apple" |
| `mappings.py` | Only if the vendor measures something the 17 canonical metrics don't cover |

`WearableUpload.device_type` is `max_length=10`, which accommodates `fitbit`, `whoop`, and `oura`
without a migration.

**Cloud-API vendors need a sync path, not an upload path.** Apple and Garmin arrive as file
uploads; Fitbit, Whoop, and Oura are OAuth-gated REST APIs requiring token storage, refresh
handling, and scheduled pulls. That work sits *above* the parser boundary and is shared across
those vendors rather than duplicated per vendor — `upload_wearable`'s `request.FILES` handling is
the wrong entry point for them, but everything from `WearableSample` onward is reused unchanged.

**Do not map a vendor field onto a concept it doesn't mean** to make a column look populated. The
gait metrics (`walking_step_length`, `walking_double_support_pct`, `walking_speed`,
`walking_hr_avg`) have no equivalent on a ring or a chest strap, and Garmin's Running Dynamics
measures different quantities entirely (see Gap A). A null is correct;
`wearable_coverage_ratio_30d` exists so consumers can tell the difference between "no data" and
"low values". See Gap G for the HRV version of this trap, which is subtler and already live.

---

## Adding a new wearable metric

1. `mappings.py` — add to `WEARABLE_CONCEPT_CODE` and `WEARABLE_ARTIFACT_BOUNDS`. If the concept
   is Observation-domain, add it to `WEARABLE_OBSERVATION_METRICS` too (fixtures/tests only —
   the runtime writer reads `domain_id`).
2. `seed_omop_concepts.py` — seed the concept, **or ingestion discards the metric** (it will be
   logged and counted, but no row is written). Seed the genuine Athena `concept_id` if the code
   exists there; otherwise mint via `_hk()` under `HK-Wearable`.
3. `wearable_parsers.py` — add the Apple `_APPLE_TYPE_MAP` entry and the Garmin FIT handler;
   add the key to the sum-vs-mean list at `:328` **and** `:489` if it is cumulative.
4. `views.py` — add the `unit_map` entry in `upload_wearable`.
5. `patient_record_service.py` — add the 30-day aggregation in `_get_wearable_data`, including the
   `_metric_daily` call, the `_within_window` call, and both `all_valid_days` unions.
6. `models.py` + migration — add the `PatientRecord` column.
7. `serializers.py` — add the column to `read_only_fields` (see Gap E).
8. `frontend/src/types/patient.ts` + `WearableTab.tsx` — expose it.
9. Tests at every layer, per CLAUDE.md.
