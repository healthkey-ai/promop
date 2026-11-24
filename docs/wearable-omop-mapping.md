# Wearable Data → OMOP Measurement / Observation Mapping

How Apple Watch and Garmin metrics are normalized into OMOP CDM v5.4 `measurement` and
`observation` rows, and what still needs to be built.

See [concept-mapping.md](concept-mapping.md) for general LOINC/SNOMED/HemOnc → OMOP
concept resolution.

---

## Design principle: one canonical metric, two device adapters

Device-specific vocabulary must not leak past the parser boundary. Apple exports HealthKit
type identifiers; Garmin exports FIT message/field pairs. Both are translated to a small set of
**canonical metric keys** at parse time, and only the canonical key reaches the OMOP writer.

```
Apple export.zip ──► parse_apple_health_export ──┐
                     (_APPLE_TYPE_MAP)           │
                                                 ├──► WearableSample ──► one concept ──► Measurement
Garmin .fit ────────► parse_garmin_fit ──────────┤   (metric_key,       (LOINC or       or Observation
                     (FIT message handlers)      │    date, value)       HK-Wearable)    (by domain_id)
Garmin Connect API ─► fetch_daily_samples ───────┘
                     (daily summaries)
```

The third input is not a third device: it is Garmin again, read through the Garmin Connect
Personal API instead of from a file. See [Garmin Connect sync](#garmin-connect-sync).

`WearableSample` (`omop_core/services/wearable_parsers.py:18`) is the normalization contract —
`(metric_key, date, value)`, nothing else. The consequence is that **a metric is stored
identically no matter which device produced it**: same concept, same units, same table. A query
for resting heart rate never has to know whether the patient wears an Apple Watch or a Fenix.

Both parsers reduce to **one value per metric per calendar day** before returning. Cumulative
metrics (steps, active minutes, sleep, distance, flights, active energy, basal energy) are summed
across the day; rates and percentages (HR, SpO2, HRV, respiratory rate, walking speed, step
length, double support, walking HR, VO2 max, body mass) are averaged. The two parsers share the
same sum-vs-mean list, at `wearable_parsers.py:346` (Garmin) and `:507` (Apple) — a metric added
to one must be added to the other.

---

## The normalization table

The canonical registry is `WEARABLE_CONCEPT_CODE` (`omop_core/services/mappings.py:99`). One row
per metric key; the concept code is the join point between the two device adapters.

Despite most entries being LOINC, **this map is not LOINC-only** — five metrics have no LOINC
equivalent and are minted locally under the `HK-Wearable` vocabulary. Concept resolution is
therefore scoped by `(vocabulary_id, concept_code)` via `WEARABLE_CONCEPT_VOCAB`
(`mappings.py:130`); a bare `concept_code` is ambiguous, since 852 codes are reused across
vocabularies.

| Metric key | Code | Vocab | Seeded concept_id | OMOP table | UCUM unit | Daily agg | Apple HealthKit type | Garmin FIT source |
|---|---|---|---|---|---|---|---|---|
| `steps` | 55423-8 | LOINC | 40758552 | **observation** | `/d` | sum | `HKQuantityTypeIdentifierStepCount` | `monitoring.steps` → `.cycles` (max — cumulative counter); fallback `session.total_steps`/`total_cycles` |
| `active_minutes` | 55411-3 | LOINC | 40758540 | **observation** | `min` | sum | `HKQuantityTypeIdentifierAppleExerciseTime` | `monitoring.active_time` ÷ 60; `session.total_timer_time` ÷ 60 |
| `resting_hr` | 40443-4 | LOINC | 3040891 | measurement | `/min` | mean | `HKQuantityTypeIdentifierRestingHeartRate` (or derived, below) | `monitoring_hr_data.resting_heart_rate` (or derived, below) |
| `hrv_sdnn` | 80404-7 | LOINC | 21491502 | measurement | `ms` | mean | `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` | legacy `hrv.sdnn` only |
| `hrv_rmssd` | HK-WEAR-HRV-RMSSD | **HK-Wearable** | 2029606354 | measurement | `ms` | mean | *(no source — Apple reports SDNN)* | `hrv_status_summary.weekly_average`/`last_night_average`; `hrv_value.value`; legacy `hrv.weekly_average` |
| `spo2` | 59408-5 | LOINC | 40762499 | measurement | `%` | mean | `HKQuantityTypeIdentifierOxygenSaturation` | `spo2_data.reading_spo2`; fallback `session.saturated_hemoglobin_percent` |
| `respiratory_rate` | 9279-1 | LOINC | 3024171 | measurement | `/min` | mean | `HKQuantityTypeIdentifierRespiratoryRate` | `respiration_rate.respiration_rate`; fallback `session.avg_respiration_rate` |
| `sleep_duration` | 93832-4 | LOINC | 1002368 | **observation** | `h` | sum | `HKCategoryTypeIdentifierSleepAnalysis` (asleep spans only) | `sleep_level` timestamp spans; fallback `sleep_data.total_timer_time` ÷ 3600 |
| `vo2_max` | 94122-9 | LOINC | 1002246 | measurement | `mL/kg/min` | mean | `HKQuantityTypeIdentifierVO2Max` | `session.enhanced_max_oxygen_consumption` → `vo2_max` |
| `distance` | 112427-0 | LOINC | 648874 | measurement | `km` | sum | `HKQuantityTypeIdentifierDistanceWalkingRunning` | `monitoring.distance` ÷ 1000; `session.total_distance` ÷ 1000 |
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
of `omop_core/concept_fixtures.py`, not the wearable block. They must not be seeded twice — a second row for
the same `(vocabulary_id, concept_code)` is exactly the duplication the seeding rules exist to
prevent.

### Table routing is by `domain_id`, not by a hard-coded metric list

Four metrics — `steps`, `active_minutes`, `sleep_duration`, `flights_climbed` — resolve to
**Observation-domain** concepts and are therefore written to `observation`. The other fourteen go
to `measurement`.

The write path does not hard-code that split. It reads `concept.domain_id` at runtime
(`patient_portal/api/views.py:3693`), so routing stays correct automatically if a code changes.
`WEARABLE_OBSERVATION_METRICS` (`mappings.py:139`) mirrors the same four keys, but exists only for
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
(`omop_core/models.py:566`) — and all seven wearable mints do:

- `vocabulary_id='HK-Wearable'` (declared in `concept_fixtures._VOCABULARIES`, and by migrations 0143 and 0343 for real deployments)
- `source='HealthKey'`
- an `HK-*`-shaped `concept_code`
- `concept_id >= 2,000,000,000` — OHDSI reserves that range for locally-authored concepts, and
  Athena never allocates there

The ids are allocated contiguously from `_HK_WEARABLE_ID_BASE = 2_029_606_350`
(`omop_core/concept_fixtures.py`), continuing the existing `HK-Labs` block. New wearable mints should
continue upward from there.

| concept_id | Code | Domain | Minted by | Used by |
|---|---|---|---|---|
| 2029606350 | HK-WEAR-STEP-LENGTH | Measurement | 0143 | runtime registry, OpenWearables seed |
| 2029606351 | HK-WEAR-DBL-SUPPORT | Measurement | 0143 | runtime registry, OpenWearables seed |
| 2029606352 | HK-WEAR-WALK-HR | Measurement | 0143 | runtime registry, OpenWearables seed |
| 2029606353 | HK-WEAR-BASAL-ENERGY | Measurement | 0143 | runtime registry, OpenWearables seed |
| 2029606354 | HK-WEAR-HRV-RMSSD | Measurement | 0143 | runtime registry, OpenWearables seed |
| 2029606355 | HK-WEAR-OW-WORKOUT-EFFORT | Observation | 0343 | OpenWearables seed only |
| 2029606356 | HK-WEAR-OW-CARDIO-AGE | Measurement | 0343 | OpenWearables seed only |

The last two are vendor scores (an estimated workout effort and a cardiovascular age) that are not
comparable across vendors, so they get a local concept rather than a generic LOINC or SNOMED one.
No runtime metric uses them. 0343 matches on `(vocabulary_id, concept_code)`, not on the id as
0143 does: a HealthKey-minted id is not guaranteed to agree across instances, so it keeps a concept
that is already present at whatever id it has and only fills in a missing `source`.

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

## OpenWearables: proposed code mappings

OpenWearables metrics do not go through the registry above. Each OpenWearables series type is a
source code on the Code Mapping page (source vocabulary `OpenWearables`, shown on the Wearables
tab), and `manage.py seed_openwearables_mappings` creates one **proposed**
`SourceCodeConceptMapping` per metric (`OPENWEARABLES_METRICS`, 94 of them). A curator approves
or changes each one; nothing here approves a mapping.

The targets follow the wearable mapping proposal ("wearable_metrics_mapping v4"), which was
reconciled with the HL7 PHR code list on 15 Nov 2026. Where the two disagreed, the HL7 PHR code is
the one seeded (#2595):

| OpenWearables code | Target | Was |
|---|---|---|
| `lean_body_mass` | LOINC 91557-9 Lean body weight | LOINC 88334-8 (Calculated) |
| `forced_vital_capacity` | LOINC 19870-5 Forced vital capacity | LOINC 19868-9 (by Spirometry) |
| `peak_expiratory_flow_rate` | LOINC 33452-4 Maximum expiratory gas flow | LOINC 19935-6 (by Peak flow meter) |
| `flights_climbed` | LOINC 100304-5 | none |
| `walking_step_length` | HK-WEAR-STEP-LENGTH | none |
| `walking_double_support_percentage` | HK-WEAR-DBL-SUPPORT | none |
| `walking_heart_rate_average` | HK-WEAR-WALK-HR | none |
| `heart_rate_variability_rmssd` | HK-WEAR-HRV-RMSSD | none |
| `air_temperature` | SNOMED 250825003 Ambient temperature | none |
| `blood_alcohol_content` | SNOMED 54791000237107 Ethanol mass concentration in blood | none |

The three gait rows and `flights_climbed` now name the same concept as the Apple HealthKit code
for the same quantity, and the RMSSD row the same as Garmin's, so one metric is one concept
whichever source sent it.

A metric the proposal has no verified code for is seeded with no destination and waits on the
Wearables tab for Suggest or a curator. `average_met` is one of them on purpose: SNOMED 16821004
"Metabolic requirement" may not mean an average MET over an interval, and it stays unmapped until
an expert has reviewed it.

### Re-running the seed

The command is `get_or_create` on the source code, so a plain re-run creates what is missing and
changes nothing else. A target that changes in `OPENWEARABLES_METRICS` therefore never reaches an
instance that was seeded earlier unless it is asked for:

```bash
python manage.py seed_openwearables_mappings --dry-run
python manage.py seed_openwearables_mappings
python manage.py seed_openwearables_mappings --update-seeded --dry-run
python manage.py seed_openwearables_mappings --update-seeded
```

`--update-seeded` re-points an existing row to the seed's target only while nothing but the seed
has set its destination or curated it:

- `status` is `proposed` (never an approved or rejected row),
- `origin` is `import` and `origin_system` is `open-wearables-seed`, and the row carries no
  Suggest candidate or score (Suggest stamps its own `origin_system` when it proposes a
  destination, which takes the row out),
- no curator has saved it: `decision_surface` is blank, `updated_by` is empty, and it has no
  curator note, LOINC axes or SNOMED attributes, and no destination unit other than one the
  seed assigns. The note, axes and attributes matter on an instance loaded by
  `copy_reference_data`, which clears `updated_by`.

A row Suggest tried and declined has no destination and is still the seed's; filling it drops the
note that run left. A target a curator has locked on the Concept to Code tab is not written to: a row the seed would create at a locked concept is created without a destination, and `--update-seeded` points it there once the concept is unlocked.

A target is used only when its concept is loaded, not retired, and in the domain the seed files
the metric under; otherwise it is reported (`Concept not found`, `Concept is retired`,
`Concept is in domain …`) and the row is created or left without it.

It never clears a destination. The run lists each row it does not bring into line:
`[LEFT ALONE]` where the seed names a target and the row keeps another destination, with the
reason, and `[KEPT]` where the seed names no target and the row has one.

### Units

Each metric's unit is seeded twice over, from the proposal's "Unit" and "UCUM Unit" columns
(`UCUM_BY_UNIT` in the seed: `bpm` → `/min`, `mmHg` → `mm[Hg]`, `°C` → `Cel`, `count` → `1`, …):

- as the row's **source unit** (`source_unit_evidence`, one entry with the UCUM code and the
  display unit), which is what the list's Units column shows. It carries no count, so the
  approval unit check does not treat it as a sighting, and no source, so a rebuild from patient
  data keeps it;
- as the mapping's **destination unit** (`destination_unit_concept`, a standard UCUM concept),
  set whether or not the metric has a destination concept yet. A unit whose UCUM concept is not
  loaded is left unset and listed at the end of the run by display unit and code (`U ([IU])`,
  `score ({score})` and `μS (uS)` on a standard Athena load).

The three temperature *deviation* metrics (`skin_temperature_deviation`,
`skin_temperature_trend_deviation`, `garmin_skin_temperature`) get a source unit and no
destination unit: the temperature conversion is for absolute readings, and applied to a
difference in °F it would be wrong.

`--update-seeded` also fills what is blank on a row that exists:

- a missing source unit, on any row, whatever its status (`[SOURCE UNIT]`). It is a sighting, so
  `updated_at` is not stamped;
- a missing destination unit (`[UNIT]`), on a proposed row no curator has saved. This does not
  need the row to be the seed's: a row whose destination Suggest proposed gets its unit too. It
  leaves alone a row that is approved or rejected, a row a curator has saved (including one
  with only LOINC axes or SNOMED attributes entered), a row whose destination a curator has
  locked, and any unit that is already set.

A run without the flag says how many rows it would fill.

A destination unit does nothing while the mapping is proposed. Once it is approved, for a
Measurement row whose unit is one promop converts between (`m`, `cm`, `kg`, `g`, `mg`, `L`,
`mL`, `min`, `h`, `Cel`, `mg/dL`), the bulk measurement write converts incoming values to it
(see AGENTS.md, "Write-time conversion of stored rows"), and the lookup API reports a source
unit it does not recognise as the same unit (`bpm` against `/min`, `mmHg` against `mm[Hg]`) as
`incompatible`. Both look a mapping up by source code alone, so an approved `weight` row would
also apply to another producer's rows whose source value is "weight". Check both before
approving a seeded row for a source that is about to send data.

The Apple and Garmin seed (`seed_wearable_device_mappings`) records the source unit its
adapters report in (`km`, `km/hr`, `bpm`, …), filling it on rows seeded before units were
recorded. It sets no destination unit: those rows are approved, and the proposal's metres and
m/s differ from the kilometres and km/h the adapters store.

### Category, measure class and metric key

Three more things the proposal says about each metric are seeded onto its mapping. All three
describe the **source** metric, not the destination concept: the same LOINC concept can be fed
by a direct sensor reading from one source and an algorithmic estimate from another.

- **Category** (`category`, the proposal's "Category" column): Activity, Heart & Cardiovascular,
  Sleep and so on. It is the first column of the Wearables tab. The allowed list
  (`WEARABLE_METRIC_CATEGORIES`) holds twenty values: the fifteen the proposal uses today and
  five (Symptoms, Reproductive Health, Mental Wellbeing, Self-care, Hearing) for HL7 PHR metrics
  that are about to join it.
- **Measure class** (`measure_class`, the "Measure Class" column): how the source produces the
  value. One of Direct measurement, Aggregate over interval, Algorithmic estimate, Vendor score,
  Environmental context, User-entered or device log. The list shows it as a second line under
  the category.
- **Metric key** (`metric_key`, the "Internal Metric Key" column): the vendor-neutral name of
  the metric, in lowercase snake case, shared by every source code that reports it.
  `resting_hr` is `HKQuantityTypeIdentifierRestingHeartRate` for Apple, `resting_hr` for Garmin
  and `resting_heart_rate` for OpenWearables. It is the Wearables tab's second column, and what
  ties the vocabularies together: OpenWearables does not cover every metric, and the HL7 PHR
  table maps from HealthKit and Health Connect directly. For the Apple and Garmin seeds it is
  the last element of each metric's tuple, which already held this key. An OpenWearables metric
  the proposal gives no key is keyed by its own code.

The values live beside each metric list (`OPENWEARABLES_CATEGORIES`,
`OPENWEARABLES_MEASURE_CLASSES`, `OPENWEARABLES_METRIC_KEYS`, and the `HL7_PHR_*`, `APPLE_*` and
`GARMIN_*` equivalents; `PHR_SOURCE_CATEGORIES` and `PHR_SOURCE_MEASURE_CLASSES` by metric key),
not inside the metric tuples. A new row gets all three.
`--update-seeded` fills whichever is blank on a row that exists, whatever its status, reports
each as `[CATEGORY]`, `[MEASURE CLASS]` or `[METRIC KEY]`, counts them at the end of the summary
line, and never changes one that is set. A plain re-run fills nothing and says how many rows
`--update-seeded` would fill. `seed_wearable_device_mappings` fills blanks on every run, as it
does for the source unit. A curator can change any of the three in the Edit Mapping dialog; a
mapping that is already approved (every Apple and Garmin row) takes an org admin or staff to
change, as for any other field.

A blank means "not yet described", not "none": a curator who clears a category, measure class
or metric key gets it back from the next fill, which for the device seed is every vocabulary
load (`load_athena_vocabularies` runs it). A value a curator changed is never replaced.

**After deploying the columns.** Nothing runs `seed_openwearables_mappings` or
`seed_hl7_phr_mappings` automatically, so on an instance seeded earlier those rows show a blank
Category and Metric until both commands are run with `--update-seeded`:

```bash
python manage.py seed_openwearables_mappings --update-seeded --dry-run
python manage.py seed_openwearables_mappings --update-seeded
python manage.py seed_hl7_phr_mappings --update-seeded
python manage.py seed_wearable_device_mappings   # or wait for the next vocabulary load
```

### Downloading the tab

The Wearables tab has "Download all wearable mappings", as a CSV or as a Word document with one
table per category: every mapping on the tab, proposed and approved, without rejected rows,
ordered so that the codes of one metric sit together. Anyone who can read the Code Mapping page
can download it. The endpoint, the columns and what the file leaves out are in
[code-mapping-api.md](code-mapping-api.md#downloading-the-wearables-tab).

## HL7 PHR: HL7 PHR, HealthKit and Health Connect codes

The HL7 PHR Code Mapping table gives each PHR code value the Apple HealthKit type and the
Android Health Connect record it is mapped from. The spec's authors map from those two, not
from OpenWearables, so the mapping table carries all three. `manage.py seed_hl7_phr_mappings`
creates one **proposed** mapping per code, in three source vocabularies, all on the Wearables
tab (`WEARABLE_SOURCE_VOCABULARIES`):

| Source vocabulary | Source code | Example | Rows |
|---|---|---|---|
| `HL7-PHR` | the HL7 PHR code value | `stepCount` | 122 |
| `Apple` | the HealthKit identifier | `HKQuantityTypeIdentifierHeartRate` | 60 |
| `HealthConnect` | the Health Connect record type | `HeartRateRecord` | 35 |

Every row has status `proposed`, provenance (`origin_system`) `HL7 PHR` — the table is where
the code's association with a metric comes from — and no reviewer. The command never approves.

### What is seeded

Two lists in the command module:

- `HL7_PHR_METRICS`, 17 HL7 PHR codes for metrics no device vocabulary carried: total energy
  burned, six dietary nutrients, steps cadence, R-R interval, three heart rhythm events, basal
  body temperature, hip circumference, bone mass, sleep arousal index and snore index.
- `PHR_SOURCE_METRICS`, one row per **wearable metric** of the mapping proposal, with its HL7
  PHR code, HealthKit identifier and Health Connect record. It is a literal copy of the
  proposal's sheet, not a runtime read of it.

**Scope: wearable metrics only.** `PHR_SOURCE_METRICS` leaves out the proposal's Nutrition,
Symptoms, Reproductive Health, Self-care, Mental Wellbeing and Hearing rows, and `food` and
`medication_adherence`. The six dietary codes of `HL7_PHR_METRICS` stay; they get no HealthKit
or Health Connect row.

A code is seeded only where the proposal names a single code for the metric:

- A HealthKit identifier `seed_wearable_device_mappings` already seeds is skipped. Those 17
  rows are approved, are what the Apple adapter resolves through, and are never read or written
  by this command. The 60 `Apple` rows it adds are proposed, so `resolve_wearable_mappings`
  does not see them.
- An HL7 cell that is a note (`sleepAHI (related)`), a pair (`cyclingCadence / stepsCadence`)
  or a FHIR profile is not a code value and seeds nothing.
- One HL7 code is one mapping. The HL7 table has a single glucose code, `bloodGlucose`, which
  the proposal lists against both `blood_glucose` and `cgm_glucose`; it is seeded for
  `blood_glucose`.
- A code is seeded as the HL7 table spells it: `exerciiseRepetitions` (sic), and
  `HKElectrocardiogramType`, which is a HealthKit class name rather than a type identifier.

### Destination, table and unit

- **Destination**: the proposal's target for the metric, when it names one that is loaded on
  the instance, not retired, in the metric's domain and a standard concept
  (`require_standard_target`; a local `HK-Wearable` concept is accepted, as it is the proposal's
  answer for a metric with no LOINC). Otherwise the row has no destination and waits for Suggest
  or a curator; the run reports why (`Concept not found`, `Concept is not standard`, …). Average
  MET (`met`) stays unmapped: the proposal's SNOMED 16821004 is marked unverified, as for the
  OpenWearables code.
- **Table**: from the proposal's Domain.
- **Unit**: the proposal's unit, as source unit and as destination unit, by the rules under
  [Units](#units). A metric whose key is in `NO_DESTINATION_UNIT` gets no destination unit
  whichever vocabulary its code is in. The unit is the metric's in the proposal, not what each
  platform reports in (Health Connect's `HeightRecord` is in metres), so check it against the
  source before approving a row.
- **Category, measure class, metric key**: the proposal's, so the HealthKit, Health Connect,
  HL7 PHR, OpenWearables and Garmin codes for one metric carry one key. Where the proposal has
  no Internal Metric Key (its OpenWearables-only rows) the key is the one
  `OPENWEARABLES_METRIC_KEYS` gives the metric's OpenWearables code.

The metrics the proposal added from the HL7 table last have no domain, unit or measure class
there. The seed assigns them by the nearest metric the proposal does describe:

| Kind of metric | Domain | Unit | Measure class |
|---|---|---|---|
| speed | Measurement | m/s | Direct measurement |
| distance, elevation gained | Measurement | m | Aggregate over interval |
| cadence | Measurement | rpm (`/min`) | Direct measurement |
| accumulated time (move time, stand time) | Observation | min | Aggregate over interval |
| temperature | Measurement | °C (`Cel`) | Direct measurement |
| other numeric quantity (pace, minute volume, AHI, …) | Measurement | none | by its nearest metric |
| event, episode or category | Observation | none | Aggregate over interval |
| workout log record (event, lap, session) | Observation | none | User-entered or device log |

No unit is guessed beyond those: a blank unit seeds no source unit and no destination unit.

### Running it

It is the OpenWearables command run once per vocabulary, in one transaction, so everything
above about re-running, `--dry-run`, `--update-seeded`, units, category, measure class and
metric key applies, with `HL7 PHR` as the provenance that marks a row as this seed's. A plain
run creates what is missing and changes no row that exists. The report has a line per
vocabulary and a total:

```
DRY RUN [HL7-PHR]: would have 122 created, 0 already existed, 79 without target concept (of 122 total).
DRY RUN [Apple]: would have 60 created, 0 already existed, 42 without target concept (of 60 total).
DRY RUN [HealthConnect]: would have 35 created, 0 already existed, 16 without target concept (of 35 total).
DRY RUN total: would have 217 created, 0 already existed, 137 without target concept (of 217 total).
```

(The "without target concept" counts are those of an instance with every proposal target
loaded; a missing or non-standard concept adds to them.)

Nothing ingests under these codes yet: the rows record the agreed destination so a source that
starts sending HealthKit, Health Connect or HL7 PHR codes finds a curated mapping waiting.

---

## Garmin Connect sync

A Garmin patient can sync from the Wearables tab instead of downloading `.fit` files (#2794).
Garmin lets any Garmin Connect user create a read-only **Personal API key** for their own account
(Garmin Connect on the web: Settings, Account, Personal API Token) and publishes a Python client,
`garmin-connect-personal-api-python`. The patient pastes the key, picks 7, 30 or 90 days, and the
server reads one summary per day.

```
POST /api/v1/patient-records/sync-garmin/
     {"api_key": "...", "remember": true, "days": 30, "end_date": "2026-11-22", "person_id": 16813}
  200 {"samples_created": 210, "duplicates_skipped": 30, "replaced": 12,
       "unmapped_samples": 0, "unmapped_metrics": [],
       "start_date": "2026-10-24", "end_date": "2026-11-22", "datasets_failed": [],
       "key_saved": true}
```

Code: `omop_core/services/garmin_connect.py` reads Garmin and returns `WearableSample`s;
`PatientRecordViewSet.sync_garmin` is the endpoint; `_store_wearable_samples` is the writer it
shares with `upload_wearable`, so concepts, artifact bounds, domain routing, the type concept and
the upload history are the file upload's own. The sync is a `WearableUpload` row with
`device_type` `garmin` and a filename of `Garmin Connect sync <start> to <end>`, listed and
deletable like a file.

- **Who.** The person, signed in (a session or a partner token, `is_interactive_session`), with
  an account linked to a patient record that is not switched off. A service credential is
  refused, and so is an OAuth token the patient delegated to an application, which could
  otherwise write to their record with a key of its own. `GarminSyncThrottle` allows 20
  attempts an hour per user.
- **Whose record.** The rows go to the caller's own record. The tab therefore shows its Garmin
  section (this sync under "Sync past days", beneath the linked account's "Automatic updates", #2832)
  only to a person on their own record (`canSyncOwnDevice`: patient mode, or a provider-console
  page whose person is the one the account's patient link names, `roles.isOwnRecord`, #2816, so
  an org admin who is also a patient gets it on their own page; a read-only embed and a
  clinician on a patient's page get none), and sends the `person_id` the page is
  showing: the server answers 403 when that is not the caller's own, so a sync started from
  someone else's page is refused and not filed quietly under the caller.
  The file upload follows the same rule (#2819): the Upload button, the drop zone, the upload
  history and its deletes are offered only on the person's own record (`ownRecord`; the
  embedded patient page passes it, where the host does not set `federated`, which hides the
  tab), the history is not read anywhere else, and
  `upload-wearable` refuses a `person_id` that is not the caller's own with a 403. On somebody
  else's record the tab shows the wearable fields and says the patient adds the data.
- **The key is saved for the next sync, encrypted, where the patient lets it be (#2834).** Once
  Garmin has accepted a key the server keeps it under `GARMIN_TOKEN_KEY` (the Fernet key the
  linked account's tokens use, which is in no database) when the request says `remember: true`,
  which the tab sends while "Save this key for next time" is ticked. The default is false, so a
  page loaded before this existed, which says the key is not stored, stays right. A later sync
  then sends no key and the saved one is used, by the account that saved it and no other. No
  endpoint returns it: `GET /api/v1/patient-records/garmin-key/` says only
  `{can_save, saved, saved_at, last_used_at}`, and `DELETE` there ("Remove saved key") deletes it.
  It is also removed when Garmin answers 401 to a sync that used it, and with the person or their
  record. A key that cannot be decrypted reads as not saved and is left in place. Every sync
  answer carries `key_saved`, failures included, so the tab knows where the key stands. A
  deployment with no `GARMIN_TOKEN_KEY` saves nothing: the key is then used for the one sync and
  dropped, as it was before. Nothing syncs on a schedule; a saved key is read only by a sync its
  patient starts.
- **The key is in no log line, error message or queued task.** That is why the sync runs inside the
  request and not on a worker, where the key would sit in the broker. Garmin's own
  `PersonalApiAuthenticator` is not used, because it caches the bearer token in a plaintext file
  with no way to turn that off; `_exchange_key` makes the same request and holds the token in
  memory. Garmin's error text is never relayed: a failure is reported by status alone. The key
  goes only to an `https` base URL, and the token request follows no redirect. In the browser
  the field is a text input drawn as dots, not a password field, so no password manager offers
  to fill or save it, and it is emptied when a sync succeeds.
- **Bounded, because it is in the request.** At most 90 days (`MAX_SYNC_DAYS`). No call is
  started after 15 seconds (`SYNC_BUDGET_SECONDS`), half of gunicorn's 30, because the write and
  the record's re-derivation come after; each call has 3 seconds to connect and 6 per read. It
  is a budget checked between calls, not a hard stop. `end_date` defaults to yesterday, since
  today's summary is still filling in; the tab sends the browser's own yesterday, which the
  server's clock cannot know.
- **What ends a sync, and what does not.** Nothing is written when the sync ends early. A key
  Garmin refuses is a 400 with fixed wording and a rate limit a 429. Garmin unreachable, erring
  or slow is a 424, not a 5xx: it is the patient's to retry, and this deployment reports its own
  5xx responses as errors. An answer the reader cannot parse is a 502, because that one means
  Garmin changed. The daily summaries are required. HRV, sleep and VO2 max are read after them;
  one that fails, is refused (a 403: the account lacks it), or runs out of time is named in
  `datasets_failed` and the rest is kept.
- **Ranged calls go in 28-day chunks**, the shortest limit the API documents (steps and stress;
  sleep allows 31, daily summaries a year), newest first, so the days lost to the budget are the
  oldest.

| Metric key | Personal API field |
|---|---|
| `steps` | daily summary `movement.steps.value` |
| `distance` | daily summary `movement.steps.distanceInMeters`, stored in km |
| `active_minutes` | daily summary `intensityMinutes.moderate + vigorous` |
| `active_energy` | daily summary `calories.burnedActive` |
| `basal_energy` | daily summary `calories.burnedResting` |
| `resting_hr` | daily summary `heartRate.restingValue` |
| `respiratory_rate` | daily summary `respiration.avgValue` |
| `spo2` | daily summary `pulseOx.avgValue` |
| `hrv_rmssd` | HRV daily summary `lastNightAvg` (Garmin's overnight HRV is RMSSD, Gap G) |
| `sleep_duration` | daily sleep summary `sleepTimeSeconds`, stored in hours; naps are apart |
| `vo2_max` | daily VO2 max `generic.vo2MaxPreciseValue`, else `vo2MaxValue`, else cycling |

A day whose summary says `includesWellnessData: false` is skipped whole: a day the watch recorded
no all-day data for is not a day of zero steps. Floors climbed and weight are in the API too, but
have no approved `Garmin` mapping, so they wait on Gap A.

### The upload history names the rows

Each `sample_summary` entry written since #2794 carries `row_id` and `row_table`: the row that
upload wrote for the sample, or a null `row_id` when it wrote none (a duplicate, an artifact, an
unmapped metric). A sync lists only the samples it wrote a row for, because it re-reads the same
days every time and listing what was already stored would grow the history by the whole range on
each sync. Entries from before have neither key.

- **Deleting an upload** removes the rows its entries name and no others. Syncs overlap, and a
  value an upload only found already stored belongs to the upload that wrote it. An entry
  without `row_id` is matched by concept, date and value, as it always was. The delete is one
  transaction and one derivation.
- The history row is written in the same transaction as the rows, for the file upload too.

### A day's total replaces what Garmin uploads wrote for the day

The derivation sums steps, active minutes and sleep over a day's rows, because a day reaches the
file upload as several `.fit` files. The API's value is already the day's total, so written
beside those rows it would double the day. The sync therefore replaces them
(`replace_daily=True`), on evidence, since a row records no device (#442):

- A stored row is **Garmin's** when a `garmin` upload's history names it (`row_id`). For
  history from before rows were named, it is Garmin's when it carries the wearable type concept
  and its `(metric, date, value)` is listed by a `garmin` upload and by no other device's, and
  no other device's history names the row.
- For each incoming value, one stored row that already says the same is kept (counted in
  `duplicates_skipped`); Garmin's other rows for that metric and day are deleted (counted in
  `replaced`); the value is inserted when no row says it.
- Nothing else is touched: an Apple upload's row, a row another ingest path wrote, a row no
  upload history accounts for, a row marked entered in error, or a value both devices recorded.

So a later sync also corrects an earlier one made before the watch had sent the whole day. The
file upload still stacks. The write holds a lock on the patient's `person` row
(`FOR NO KEY UPDATE`) from before it reads what is stored, so two syncs at once cannot both plan
against the old rows, and it asks again under that lock whether the record is switched off. The
history is read unnested in SQL, for the days being written only.

**Known limits.**

- A `.fit` file uploaded for a day *after* that day was synced stacks a partial on the total
  again, until the next sync removes it. Preventing that needs the device on the row (#442).
- Deleting a sync does not bring back the rows it replaced.
- The record is re-derived inside the request, as for a file upload. For a patient whose
  derivation is slow the request can outlive gunicorn's 30 seconds after the rows are written;
  the rows and their history are then in place and the next derivation catches the record up.
- A sync holds a web worker while it reads Garmin, and nothing caps how many run at once.
- Nothing ties a key to the patient: a sync with somebody else's key files their data, as an
  upload of somebody else's `.fit` file does.

## Garmin account by OAuth

The sync above pulls with a key the patient pastes. A deployment that holds a Garmin Connect
Developer Program app offers a second way (#2802): the patient approves the app once on Garmin's
consent page, and Garmin then **pushes** their data to this server whenever their watch syncs with
Garmin Connect. The Starter tier of the program is push only, so there is no on-demand read and no
history: data starts with the first sync after connecting.

```
Wearables tab ── POST garmin/oauth/start/ ──► Garmin consent page
                                                   │ (patient approves)
record page ◄── /?garmin=connected ◄── GET garmin/oauth/callback/?code&state
                                                   │ code → tokens, user id, permissions
                                                   ▼
                                           GarminConnection (per patient)

watch ─► Garmin Connect ─► POST garmin/webhooks/<secret>/ ─► read_push ─► the shared writer
                           {"dailies":[{userId, ...}], ...}   by userId     (replace_daily)
```

### Setting it up

| Variable | What |
|---|---|
| `GARMIN_CLIENT_ID`, `GARMIN_CLIENT_SECRET` | the app's consumer key and secret from the developer portal |
| `GARMIN_WEBHOOK_SECRET` | a random string of at least 32 characters: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GARMIN_TOKEN_KEY` | the key patients' Garmin tokens, and a saved Personal API key (#2834), are stored under: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

In the developer portal, register the redirect URL `<APP_BASE_URL>/api/v1/garmin/oauth/callback/`
and, for each push type (Dailies, Sleeps, HRV, Pulse Ox, Respiration, User Metrics,
Deregistrations, User Permissions), the URL
`<APP_BASE_URL>/api/v1/garmin/webhooks/<GARMIN_WEBHOOK_SECRET>/`, with delivery set to push. The
Wearables tab's Garmin section shows "Automatic updates" (Connect Garmin) only when all four
variables are set; without them it holds the key sync, "Sync past days", alone (#2832). A webhook secret shorter
than 32 characters counts as unset: the URL is the webhook's only credential.

### What a push becomes

| Push type | Field | Metric key | Note |
|---|---|---|---|
| `dailies` | `steps` | `steps` | |
| `dailies` | `distanceInMeters` | `distance` | stored in km |
| `dailies` | `moderateIntensityDurationInSeconds` + `vigorousIntensityDurationInSeconds` | `active_minutes` | minutes |
| `dailies` | `activeKilocalories` | `active_energy` | |
| `dailies` | `bmrKilocalories` | `basal_energy` | |
| `dailies` | `restingHeartRateInBeatsPerMinute` | `resting_hr` | |
| `sleeps` | deep + light + REM seconds, else `durationInSeconds` | `sleep_duration` | hours |
| `hrv` | `lastNightAvg` | `hrv_rmssd` | Garmin's overnight HRV is RMSSD (#438) |
| `pulseox` | `timeOffsetSpo2Values` | `spo2` | the day's mean, see below; on-demand readings left out |
| `allDayRespiration` | `timeOffsetEpochToBreaths` | `respiratory_rate` | the day's mean, see below |
| `userMetrics` | `vo2Max` | `vo2_max` | |

An entry's day is its `calendarDate`, else the local day its `startTimeInSeconds` falls on; a day
more than a year old or after tomorrow is dropped. Garmin sends a day's summary again as the day
goes on; each one replaces the last through the same
[replace rule](#a-days-total-replaces-what-garmin-uploads-wrote-for-the-day) the sync uses. A daily
summary with no steps yet (the first sync after midnight) stores no steps, distance, active minutes
or active energy: it is not a day of zero movement. Other push types are answered and ignored.

Pulse ox and respiration arrive as readings in several summaries across a day, over several pushes,
so one push is never the day's average. Each summary's sum and count are kept on the connection for
14 days, by Garmin's `summaryId`, and the stored value is the mean of every reading held for the
day; a summary sent again, grown, replaces its own earlier totals.

A month's pushes fold into one row of the upload history, "Garmin Connect push <YYYY-MM>", which
lists each metric and day once with the value now stored. Deleting it removes that month's pushed
rows.

These field names are Garmin's published Health API names. The full specification sits behind the
developer portal, so check the reader against the portal's sample pushes before relying on a type.

### Limits

- A push carries no signature. The webhook answers only at its secret path and routes by Garmin
  user id to the patient connected as that user; entries for anyone else are dropped. The secret is
  kept out of the audit trail and Sentry, but the platform's access log records request paths:
  treat that log as secret.
- Connecting finishes only in the browser that started it, signed in to this site. A patient signed
  in through a partner host cannot connect.
- The tokens are stored encrypted under `GARMIN_TOKEN_KEY` and used only to tell Garmin when a
  patient disconnects, or when their record is erased.
- No history, no activities, body composition or stress. A `.fit` upload after a push for the same
  day stacks beside it, as it does after a sync (#442).

## Phone sync through the Open Wearables SDK

Apple HealthKit and Google Health Connect can only be read by an app on the
phone. [Open Wearables](https://github.com/the-momentum/open-wearables)
publishes SDKs for that (Swift, Kotlin, Flutter, React Native), built to talk
to an Open Wearables server. PRomop does not run one. It answers the SDK's own
paths itself, so the SDK is configured with a PRomop deployment as its host
and syncs straight into the patient's record (#2810).

### For the people writing the app

1. Add the Open Wearables SDK for your platform and configure it with the
   deployment's address: `configure(host: "https://<deployment>")`. The SDK
   appends `/api/v1/...` itself.
2. Ask the patient for the connection code they see on the Wearables tab of
   their record, and exchange it:

   ```http
   POST https://<deployment>/api/v1/invitation-code/redeem
   Content-Type: application/json

   {"code": "A3K9M7X2"}
   ```

   ```json
   {"access_token": "...", "token_type": "bearer", "refresh_token": "owr_...",
    "expires_in": 3600, "user_id": "0b0c9a2e-..."}
   ```

   The code works once and lasts 15 minutes. Any code that does not work is a
   404.
3. Sign the SDK in with what came back:
   `signIn(userId: user_id, accessToken: access_token, refreshToken: refresh_token, apiKey: nil)`.
   From there the SDK requests HealthKit or Health Connect permission, syncs,
   and refreshes its access token on its own.
4. Ask the SDK only for the types PRomop stores (the table below). Heart-rate
   samples in particular are large and are not read; a page of them over
   32 MB is refused, and the SDK sends a refused page again for ever.

There is no API key and no app secret: the patient's code is the only
credential an app is ever given, and its tokens can do one thing, add wearable
values to that patient's record.

### What the SDK calls

| Call | Answer |
|---|---|
| `POST /api/v1/sdk/users/{user_id}/sync` with `Authorization: Bearer <access_token>` | 202 `{status_code, response, user_id, stored, repeated, late, unmapped, rejected, unrecognized}` |
| `POST /api/v1/token/refresh` `{refresh_token}` | 200 `{access_token, token_type, expires_in}`; 401 when the token no longer works. The refresh token is not replaced |
| `GET /api/v1/sdk/users/{user_id}/sync/status?provider=apple` | 200 `{provider, historicalSyncCompleted: null, syncedUntil, dataTypes: []}` |
| `POST /api/v1/sdk/users/{user_id}/logs` | 202; the body is not read or kept |
| `DELETE /api/v1/users/{user_id}/connections/{provider}` | 204; this phone is signed out |

`provider` is `apple`, `health_connect` (or its older name `google`) or
`samsung`. The Android SDK reads one store and picks Samsung Health on a
Galaxy phone unless the app chooses Health Connect; both are read the same
way here.

### What a sync becomes

The body is the SDK's `SyncRequest`: `data.records` (one per HealthKit or
Health Connect sample), `data.sleep` (one per sleep stage) and
`data.workouts`.

| In the body | Becomes |
|---|---|
| a record whose `type` is a HealthKit identifier in `_APPLE_TYPE_MAP`, or one of the Health Connect names below | one reading for that metric on the record's local day |
| `STEP_COUNT`, `RESTING_HEART_RATE`, `HEART_RATE_VARIABILITY` (RMSSD), `OXYGEN_SATURATION`, `RESPIRATORY_RATE`, `VO2_MAX`, `FLOORS_CLIMBED`, `ACTIVE_CALORIES_BURNED`, `WEIGHT` | steps, resting heart rate, HRV RMSSD, SpO2, respiratory rate, VO2 max, flights climbed, active energy, body mass |
| `DISTANCE` (Android) | not stored: it is walking, running and cycling together |
| a sleep stage `sleeping`, `light`, `deep` or `rem` | hours of sleep on the day the stage started; `in_bed` and `awake` are not sleep |
| any other record type, and every workout | not stored; record types are listed back in `unrecognized` |

Readings are added up per device, metric and day, each record once, and the
day's value follows the `export.zip` parser's rule: the sum for steps, active
minutes, sleep, distance, flights and the two energies, the mean for
everything else. A record sent again changes nothing; a Health Connect record
sent again at a new value moves the day by the difference. That value is
written through the wearable writer, so the row, its unit and its place in the
upload history are those of the [row shape](#row-shape-written-today) below.

The iPhone SDK does not send Apple Exercise Time or Walking Heart Rate
Average, so active minutes and walking heart rate come only from an
`export.zip`.

Units are converted for the metrics that arrive in more than one: distance
(`m`, `km`, `mi` to km), walking speed (`m/s` to km/hr), step length (`m` to
cm), energy (`Cal`, `kcal`, `kJ` to kcal), weight (`kg`, `g`, `lb` to kg),
exercise time (`min`, `s`, `h` to min). A percentage sent as a fraction is
multiplied by 100. Anything else is rejected and counted.

### Beside the file uploads

- For an averaged metric the sync's value for a day stands, and an
  `export.zip` uploaded later skips that metric and day.
- For a summed metric the larger figure for the day stands, whichever arrived
  first: a phone's running total is partial until it has sent the whole day.
- Another device's value for the same day is left alone.
- Deleting a sync's row from the upload history removes those days for good;
  the phone's later records for them are ignored until it is disconnected and
  connected again.

### Limits

- Android records are stored from the first sync, unresolved or provisional
  until the `HealthConnect` mappings are approved (#2835). A day already
  stored is resolved by that approval, by Generate OMOP, or by the phone's
  next record, not by the same records sent again. HRV (RMSSD) has no seeded
  mapping: its first reading queues `HeartRateVariabilityRmssdRecord`.
- A day's steps are every source's samples added up, as in the export parser,
  so an iPhone and a Watch together can be counted twice (#2823).
- Distance, walking speed and step length are converted to the stored unit
  here and not by the export parser, so the two can differ for the same
  records.
- The general heart rate series is not read, so resting heart rate comes only
  from the watch's own resting value.
- A sleep stage is filed under the day it started on, as in the export
  parser, so a night that begins before midnight is split.
- The day a record belongs to is taken from the patient's browser time zone
  when the record gives no offset; the zone is the one they first connected
  in and does not follow them when they travel.
- Not verified against a live phone.

## Derived values (not directly exported by either device)

Two metrics are computed rather than read, and this is where the two adapters deliberately
converge on the same algorithm so the stored values stay comparable:

**Resting heart rate — 10th-percentile fallback.** Neither device reliably exports resting HR.
When no dedicated resting-HR record exists for a day, both parsers take the 10th percentile of
that day's all-day heart-rate samples as the proxy (`wearable_parsers.py:328` for Garmin,
`:491` for Apple). The absolute minimum is too noisy; the mean is inflated by activity.

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

Values outside `WEARABLE_ARTIFACT_BOUNDS` (`mappings.py:144`) are discarded *before* the OMOP
row is created (`views.py:3731`) — rejected readings are never persisted, so the OMOP tables
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

A reading comes in as a **source code**, as a hospital's result does (#2835).
The importer (`_write_wearable_samples` in `patient_portal/api/views.py`, with
`omop_core/services/wearable_rows.py`) files the row under the device's own
code and records the code in the patient's source-code inventory; what the
row's concept is, and whether it is accepted or provisional, is the mapping's
to say, through the same resolver every other code goes through.

| Column | Value | Notes |
|---|---|---|
| `person_id` | the patient's Person | |
| `*_source_value` | the **device's code**: Garmin's metric key (`steps`), the HealthKit identifier, the Health Connect record type | cut to the 50-character column; the mapping and the inventory keep the whole code |
| `measurement_concept_id` / `observation_concept_id` | the approved mapping's concept; else 0 until a mapping answers | a qualifying proposal's concept on a provisional row |
| `confidence` | null on an accepted or unresolved row; the proposal's percent on a provisional one | a curated, unscored device proposal stands at `PROVISIONAL_CONFIDENCE_FLOOR` |
| `measurement_date` / `observation_date` | sample date | date only; no `_datetime` (Gap D) |
| `*_type_concept_id` | 32865 *Patient self-report* | no fallback: a missing concept returns HTTP 503 |
| `value_as_number` | daily aggregate, rounded to 2dp | |
| `unit_source_value` | UCUM string from the table above | `unit_concept_id` **not set** (Gap B) |

The table is the code's mapping's (`omop_table`), which follows its concept's
domain; a code no mapping holds yet goes where its metric usually does
(`default_table`).

### What a mapping makes of a row

| The code's mapping | The row | 30-day metrics |
|---|---|---|
| approved (global, this device's vocabulary and code) | **accepted**: at the mapping's concept, no confidence | read as approved data |
| proposed with a destination, `PROVISIONAL_DATA_ENABLED` on | **provisional**: at the proposal's concept, with its confidence | shown marked Provisional, only where no accepted reading decides the metric |
| anything else: no mapping row, no destination, rejected, a ranker's score below the floor, a hospital-scoped mapping, the switch off | **unresolved**: concept 0, kept | not read |

The mapping can change afterwards and the rows follow it by the resolver's own
rules: approval re-points unresolved rows and makes provisional ones accepted;
rejection sends provisional rows back to unresolved and keeps the readings;
moving a mapping between measurement and observation relocates its rows. A
patient's next import and Generate OMOP both resolve whatever is still
waiting. See
[AGENTS.md](../AGENTS.md#wearables--readings-come-in-as-source-codes) for the
three rules that keep a device's code apart from a hospital's of the same
spelling.

The HTTP response carries `samples_created` and `duplicates_skipped`, plus,
when above zero: `provisional_samples` (of the rows this upload stored, those a
proposal gave a concept), `unresolved_samples` / `unresolved_metrics` (those
still waiting for a mapping; the two are counted apart) and `settled_rows`
(stored rows, this upload's or earlier ones', that a mapping settled during
it). `unmapped_samples` / `unmapped_metrics` now mean only a metric the device
has no code for, which is not stored.

Dedup is `(metric_key, date, round(value, 2))` against the rows already filed
under the code, and against rows at the code's concept (those written before
rows carried their device code, and another device's reading of the same
metric), so re-uploading an overlapping export is safe. Rows carry
`_skip_patient_record_refresh = True` and `refresh_patient_record(person)` is
called once after the bulk insert. Each upload is recorded in `WearableUpload`
for patient-visible history.

Rows written before #2835 carried their destination concept's code as the
source value. Migration `0371` re-files the ones an upload's history names and
writes their inventory entries; older ones stay as they are and are still read
by concept.

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

> **Environments.** Concept resolution claims here were verified against a
> captured staging vocabulary snapshot (1,979,424 concepts / 277,790 LOINC rows)
> and against local `promop_dev` (partial `seed_omop_concepts` set only).
> Staging is the reference environment for this work; the PostgreSQL database
> name and hostname are deployment-specific and come from `DATABASE_URL`.
>
> Do not infer a database name or host from this document. Use the deployment's
> configured `DATABASE_URL` for migrations and validation.

### Gap A — Garmin has no adapter for six metrics (#444)

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

### Gap B — `unit_concept_id` is never populated (#443)

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

### Gap C — type concept was 'Survey', falling back to 'Lab' — **fixed** (#441)

The write path used concept 32883 with a comment reading "wearable device = 32883 / Patient
self-report", falling back to 32856. Both were wrong:

| Concept | Actual `concept_name` |
|---|---|
| 32883 | **Survey** |
| 32856 | **Lab** |

A wearable reading is neither a survey response nor a laboratory result. Worse, 32883 is absent
from any database seeded only by `seed_omop_concepts`, so the fallback fired there and wearable
rows were typed `Lab` outright.

**Resolution.** `WEARABLE_TYPE_CONCEPT_ID = 32865` (*Patient self-report*, code `OMOP4976938`) in
`mappings.py`, seeded by its genuine Athena concept_id.

OMOP's `Type Concept` vocabulary has **81 rows and contains no device or wearable type** — the
full list was reviewed — so *Patient self-report* is the closest faithful fit for data the
patient's own device produced. It is a judgement call, and it is recorded here rather than left
implicit in a constant.

**The fallback is gone deliberately.** If the concept is missing, the upload returns HTTP 503 and
logs at ERROR rather than substituting a type. Refusing to write beats writing a row that
misstates where the data came from, and an unseeded database is a setup error the operator needs
to see.

Historical rows still carry the old type. Backfilling them is safe in principle — the metric,
value, and units were always correct, so only the type column is wrong — but it must be scoped to
wearable metric concepts, since 32856 is legitimately used by the lab and FHIR paths. Tracked in
#441.

### Gap D — no `measurement_datetime`, provider, or visit

Only the date is stored. For sub-daily analyses (nocturnal SpO2 desaturation, circadian HR) the
current model cannot support the query. Deferred deliberately — the daily grain is what the
30-day `PatientRecord` summaries need — but noted so the limitation is not rediscovered later.

### Gap E — eleven derived `PatientRecord` columns were client-writable — **fixed** (#440)

`PatientRecordSerializer.read_only_fields` protected the original ten wearable columns, with the
comment "Wearable summaries are written by the device-sync service, never by the client API." The
eleven columns added since were never added to that list, so a client could PATCH any of them and
the value survived until the next `refresh_patient_record` — an unbounded window, since refresh
only fires on OMOP writes for that person.

**Resolution.** The list is no longer hand-maintained. `_derived_wearable_fields()` enumerates
`PatientRecord` for concrete fields named `*_30d` or `wearable_*` and appends them to
`read_only_fields`, so a new column is protected the day it is added rather than the day someone
notices.

Matching on the naming convention deliberately errs toward over-protection: a future settings
field that happened to match would be wrongly read-only, which surfaces immediately as a rejected
write, whereas a derived column silently accepting client values is invisible.

Two tests guard it — one asserting no such column is writable through the serializer, one issuing
a PATCH naming every one of them and asserting none changed. Both enumerate the model, so neither
can drift the way the tuple did; the first also asserts the discovery predicate still matches the
full set, so it cannot pass vacuously.

### Gap F — daily-total units are dimensionally loose (#443)

`distance` is stored as `km` and `steps` as `/d`, but both values are daily totals. `distance`
should arguably be `km/d` to match. The wearable mapping proposal lists 112427-0 in metres
("normalise km to m at ingest"), and the OpenWearables seed describes its distances in `m`; the
Apple and Garmin write path still stores kilometres. This affects nothing today because every consumer reads
`WEARABLE_CONCEPT_CODE` and knows the semantics, but it will confuse any external OMOP consumer.

### Gap G — HRV conflated SDNN with RMSSD — **fixed** (#438)

LOINC 80404-7 is *R-R interval.standard deviation* — specifically **SDNN**. Apple's identifier is
unambiguously SDNN (`HKQuantityTypeIdentifierHeartRateVariabilitySDNN`), so that side was always
sound. Garmin's HRV Status is **RMSSD**-based (confirmed against Garmin's own documentation: it is
the root mean square of successive differences over overnight readings, displayed as a 7-day
rolling mean — exactly `hrv_status_summary.weekly_average`). It was being filed under the SDNN
code.

RMSSD and SDNN are different statistics over the same R-R series. They are not interchangeable and
produce different values from identical input, so this was the same defect class as the original
`walking_speed`→BMI mapping.

**Resolution.** `hrv_rmssd` is now a separate canonical metric:

- LOINC has **no** RMSSD concept — verified against a full Athena load (1,979,416 concepts):
  searching for `RMSSD` and for `successive difference` returns nothing, and the complete
  `R-R interval` family carries only mean, min, max, standard deviation (80404-7 / 76643-6) and
  coefficient of variation. So it is minted under `HK-Wearable` at 2,029,606,354, per the
  quarantine rules above.
- Garmin's `hrv_status_summary` and `hrv_value` paths now write `hrv_rmssd`.
- The legacy `hrv` message routes **per field**: `weekly_average` is HRV Status (RMSSD) while a
  field named `sdnn` is the standard-deviation form, so each goes to its own metric rather than
  whichever is checked first.
- `hrv_rmssd_avg_30d` is a separate `PatientRecord` column. The two are never averaged together —
  a merged value would be neither statistic.

**Known limitation.** Historical Garmin rows already written under 80404-7 are *not* repaired.
`purge_broken_wearable_rows` cannot match them: Apple rows under the same code are correct, and a
wearable OMOP row records nothing about which device produced it (**#442**). Garmin patients get
correct HRV only after re-uploading their export. This is called out in the purge command's
docstring so the limitation is not rediscovered.

Consumers should treat `hrv_sdnn_avg_30d` and `hrv_rmssd_avg_30d` as distinct measurements, not as
fallbacks for one another. A patient normally has one or the other depending on their device.

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
| `wearable_parsers.py` | The new parser. Reuse the shared sum-vs-mean list (`:346`, `:507`) — don't invent a third copy. |
| `views.py:3569` | `device_type` allow-list, currently the literal `('garmin', 'apple')` |
| `views.py:3579-3582` | Per-device file-extension validation |
| `views.py:3605` | Parser dispatch, plus the error-message ternary at `:3613` |
| `WearableTab.tsx:56` | `detectDeviceType`, and the upload-history label at `:364` — a two-way ternary that renders any third device as "Apple" |
| `mappings.py` | Only if the vendor measures something the 18 canonical metrics don't cover |

`WearableUpload.device_type` is `max_length=10`, which accommodates `fitbit`, `whoop`, and `oura`
without a migration.

**Cloud-API vendors need a sync path, not an upload path.** Apple and Garmin arrive as file
uploads; Fitbit, Whoop, and Oura are OAuth-gated REST APIs requiring token storage, refresh
handling, and scheduled pulls. That work sits *above* the parser boundary and is shared across
those vendors rather than duplicated per vendor — `upload_wearable`'s `request.FILES` handling is
the wrong entry point for them, but everything from `WearableSample` onward is reused unchanged.
[Garmin Connect sync](#garmin-connect-sync) is the first such path and shows the split: its own
entry point (`sync_garmin`), the shared writer (`_store_wearable_samples`). It refreshes no token
and pulls on no schedule: the patient's key is used when they press Sync, saved encrypted between
syncs if they choose (#2834). A vendor that needs scheduled pulls still has that work to do.

**Do not map a vendor field onto a concept it doesn't mean** to make a column look populated. The
gait metrics (`walking_step_length`, `walking_double_support_pct`, `walking_speed`,
`walking_hr_avg`) have no equivalent on a ring or a chest strap, and Garmin's Running Dynamics
measures different quantities entirely (see Gap A). A null is correct;
`wearable_coverage_ratio_30d` exists so consumers can tell the difference between "no data" and
"low values". Gap G is the HRV version of this trap — subtler, and it reached production before
it was caught.

---

## Adding a new wearable metric

1. `mappings.py` — add to `WEARABLE_CONCEPT_CODE` and `WEARABLE_ARTIFACT_BOUNDS`. If the concept
   is Observation-domain, add it to `WEARABLE_OBSERVATION_METRICS` too (fixtures/tests only —
   the runtime writer reads `domain_id`).
2. `seed_omop_concepts.py` — seed the concept, **or ingestion discards the metric** (it will be
   logged and counted, but no row is written). Seed the genuine Athena `concept_id` if the code
   exists there; otherwise mint via `_hk()` under `HK-Wearable`.
3. `wearable_parsers.py` — add the Apple `_APPLE_TYPE_MAP` entry and the Garmin FIT handler;
   add the key to the sum-vs-mean list at `:346` **and** `:507` if it is cumulative.
4. `views.py` — add the `unit_map` entry in `_store_wearable_samples`, the writer
   `upload_wearable` and `sync_garmin` share. If Garmin's Personal API reports the metric, read
   it in `garmin_connect.py` too.
5. `patient_record_service.py` — add the 30-day aggregation in `_get_wearable_data`, including the
   `_metric_daily` call, the `_within_window` call, and both `all_valid_days` unions.
6. `models.py` + migration — add the `PatientRecord` column.
7. `serializers.py` — nothing to do if the column is named `*_30d` or `wearable_*`;
   `_derived_wearable_fields()` picks it up automatically (Gap E). Any other name must be added
   to `read_only_fields` by hand.
8. `frontend/src/types/patient.ts` + `WearableTab.tsx` — expose it.
9. Tests at every layer, per CLAUDE.md.

---

## CDISC Biomedical Concepts

A CDISC Biomedical Concept (BC, identified by its NCIt code) is mapped the way
every other code is: as a **source code with one OMOP concept as its
destination**. BC rows are `SourceCodeConceptMapping` rows in the `CDISC-BC`
vocabulary (the code is the BC id), shown on the Wearables tab in the group of
the wearable metric they match, so they are proposed, voted on, approved and
copied like any other row there. `BiomedicalConcept` is the catalog behind
them: the 1,588 BCs of the CDISC Library package of 2026-11-02, shipped by
migration `0354`. Migration `0355` seeds 21 proposed BC rows.

A wearable code has no BC of its own. It **reaches the BC that shares its OMOP
destination**, so several codes (the HealthKit, Garmin and OpenWearables
identifiers of one metric) reach one BC, and nothing is stored about the pair.
A BC is broader than a LOINC concept and has one destination, so it is reached
only by the codes on that concept: the Heart Rate BC points at general heart
rate, and resting heart rate does not reach it. A metric with no BC (steps,
sleep, exercise time, RMSSD) has none rather than a near miss. The OMOP/LOINC
mapping stays the full one; the BC view is a partial complement.

The Wearables download takes `destination=cdisc-bc` (the "Map codes to" menu
beside it) for a view-only CSV or Word file of the wearable codes that reach a
BC, a line per BC. `services/biomedical_concepts.biomedical_concept_mappings`
gives the same answer for one code mapping.

A vote share's public page offers the same two downloads
(`POST /api/v1/public/mapping-votes/export/`, the link's token in the body),
with the provenance and source-unit columns left empty: the public page shows
neither.
