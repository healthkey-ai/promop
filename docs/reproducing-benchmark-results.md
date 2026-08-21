# Reproducing Benchmark Results

This guide explains how to reproduce the benchmarks published in the PRomop paper, either
against the exact published cohort or against one you generate locally. Both cohorts are
fully synthetic — no real patient data is involved either way.

---

## The two benchmarks

### 1. Trial-eligibility benchmark (`benchmark_trial_eligibility`)

Measures how long it takes to fetch the **20 fields** most commonly required
for clinical trial eligibility screening, via two query paths:

| Path | Mechanism |
|------|-----------|
| **PatientRecord** | Single indexed lookup on the `patient_record` table — the fast path used in production |
| **Raw OMOP** | Reconstructs the same 20 fields live from `person`, `measurement`, `observation`, and `condition_occurrence` using 17 correlated subqueries |

### 2. Full PatientRecord benchmark (`benchmark_patient_record`)

Measures how long it takes to retrieve the **full PatientRecord** (~120
OMOP-derived fields) via two paths:

| Path | Mechanism |
|------|-----------|
| **PatientRecord read** | Single `SELECT *` on `patient_record` with `select_related('person')` |
| **Live OMOP derivation** | Runs all 19 section-extractor functions from `patient_record_service.py`, issuing multiple queries each against the underlying OMOP tables |

Both benchmarks include a warm-up pass before timing to avoid cold-cache bias.

---

## Prerequisites

### 1. Install PROMOP

```bash
git clone https://github.com/<your-org>/promop.git
cd promop
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set up a local PostgreSQL database

```bash
# macOS — postgresql@14 via Homebrew
brew services start postgresql@14

PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U $(whoami) -d postgres \
  -c "CREATE ROLE postgres WITH SUPERUSER CREATEDB CREATEROLE LOGIN;"

PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U postgres -d postgres \
  -c "CREATE DATABASE promop_dev OWNER postgres;"
```

### 3. Apply migrations

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py migrate --noinput
```

### 4. Choose a cohort route

There are two, and Step 1 covers both:

| | Route A — published cohort | Route B — generate locally |
|---|---|---|
| Source | Zenodo [10.5281/zenodo.21430170](https://doi.org/10.5281/zenodo.21430170) | Synthea, on your machine |
| Download | `synthea_bc_1000.json`, ~249 MB | none |
| Needs the Synthea JAR | no | yes |
| Reproduces published numbers | yes — same 1,000 patients | ratio yes, absolute values no |

**Route A** is the one to use when checking the paper's figures: it is the exact cohort those
numbers came from. **Route B** needs no download and no citation, and is the better choice for
adapting the benchmark to a different disease or cohort size. For Route B, see
[sample-patient-data.md](sample-patient-data.md) for obtaining
`synthea-with-dependencies.jar`; pass its location with `--jar-path` if it is not on the
default search path.

---

## Step 1 — Load the cohort

### Route A — import the published cohort

Download `synthea_bc_1000.json` from the Zenodo record above, then:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py import_org_patients --input synthea_bc_1000.json
```

The export records the org it came from (`synthea-bc`), so that org is created and
used by default. Pass `--org <slug> --create-org` to load it somewhere else.

**What happens:**
- Creates the `synthea-bc` organization
- Inserts one `Person` per patient, with all OMOP CDM rows (`Measurement`, `Observation`,
  `ConditionOccurrence`, `DrugExposure`, …) under fresh sequence-backed PKs
- Derives each `PatientRecord` from those rows via `refresh_patient_record` — the same path
  every other write in the system uses
- Concept FKs absent from the local `Concept` table are remapped to `concept_id=0`; the
  `*_source_value` fields the benchmarks use as their LOINC fallback are always preserved

**Options:**

| Option | Effect |
|--------|--------|
| `--create-org` | Create the target org if it does not exist |
| `--replace` | Delete and reimport patients whose `person_id` already exists |
| `--dry-run` | Parse and validate without writing |
| `--snapshot-patient-record` | Write the exported projection verbatim instead of deriving it |

Use `--snapshot-patient-record` only if you need a field that derivation cannot reconstruct —
one enriched at export time with no OMOP row behind it. Everything the two benchmarks read
derives correctly without it.

Expected summary:

```
Import complete
  Patients in file  :     1000
  Imported          :     1000
  Replaced          :        0
  Skipped (exists)  :        0
  Errors            :        0
```

### Route B — generate the cohort locally

`generate_import_enrich_synthea_bc` runs the whole pipeline as one command: generate a
breast-cancer Synthea bundle, import it into OMOP under a target org, then run the
breast-cancer enrichment pass so `PatientRecord` can derive its fields from the imported
OMOP rows.

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py generate_import_enrich_synthea_bc \
    --count 100 \
    --output /tmp/synthea_bc_100.json \
    --org-slug synthea-bc \
    --seed 20260704 \
    --wipe-existing
```

**What happens:**
- Creates the `synthea-bc` organization (`--wipe-existing` first deletes any existing org of
  that slug and cascades to its patients, so the command is safe to re-run)
- Generates 100 breast-cancer patients as a FHIR R4 Bundle at `--output`
- Imports the bundle through `import_fhir_bundle`, writing OMOP CDM rows
  (`Measurement`, `Observation`, `ConditionOccurrence`, `DrugExposure`, …)
- Runs the enrichment pass, after which the signal chain has derived one `PatientRecord`
  row per patient
- Concept FK values absent from the local `Concept` table resolve to `concept_id=0`; the
  `*_source_value` fields the benchmarks use as fallback are always preserved

**Useful options:**

| Option | Default | Effect |
|--------|---------|--------|
| `--count N` | 100 | Cohort size |
| `--org-slug SLUG` | `synthea-bc` | Org to create and import into |
| `--seed N` | — | Synthea random seed |
| `--jar-path PATH` | — | Location of `synthea-with-dependencies.jar` |
| `--wipe-existing` | off | Delete the target org and its patients before generating |
| `--import-batch-size N` | 1 | Patients per import batch |

> **On exact reproducibility with Route B.** Regenerating does not reproduce the published
> cohort value-for-value: parts of the enrichment step are probabilistic (wearable readings,
> best-response assignment, some behavioral fields), and absolute latency is hardware- and
> cache-dependent in any case. Use `--count 1000` to match the published cohort size, which
> is what governs the ratio. **The reproducible result on Route B is the relative speedup
> between the `PatientRecord` and raw-OMOP paths**, not the absolute millisecond figures.
> Route A reproduces both.

---

## Step 2 — Verify the cohort

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py shell -c "
from omop_core.models import PatientRecord
qs = PatientRecord.objects.filter(organization__slug='synthea-bc')
print('Patients:          ', qs.count())
print('disease set:       ', qs.exclude(disease=None).count())
print('hemoglobin set:    ', qs.exclude(hemoglobin_g_dl=None).count())
print('ER status set:     ', qs.exclude(estrogen_receptor_status=None).count())
print('ECOG set:          ', qs.exclude(ecog_performance_status=None).count())
"
```

All four counts should be 100.

---

## Step 3 — Trial-eligibility benchmark

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py benchmark_trial_eligibility \
    --org-slugs synthea-bc \
    --repeat 3 \
    --output trial-eligibility-results.json
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--org-slugs` | `synthea-mm` | Comma-separated org slugs |
| `--person-ids` | — | Override cohort with explicit `person_id` list |
| `--limit N` | all | Cap cohort size |
| `--repeat N` | 1 | Repeat timed passes N times (more samples) |
| `--output FILE` | — | Write raw JSON stats to file |

**The 20 eligibility fields and their raw OMOP source:**

| PatientRecord field | OMOP table | Lookup key |
|---------------------|------------|-----------|
| `patient_age` | `person` | `year_of_birth` (computed) |
| `gender` | `person` | `gender_concept.concept_name` |
| `disease` | `condition_occurrence` | concept name contains cancer/neoplasm/carcinoma/… |
| `stage` | `measurement` / `observation` | LOINC 21908-9 |
| `ecog_performance_status` | `observation` | concept name icontains `ecog` |
| `karnofsky_performance_score` | `observation` | concept name icontains `karnofsky` |
| `hemoglobin_g_dl` | `measurement` | LOINC 718-7 |
| `platelet_count_thousand_per_ul` | `measurement` | LOINC 777-3 |
| `anc_thousand_per_ul` | `measurement` | LOINC 751-8 |
| `wbc_count_thousand_per_ul` | `measurement` | LOINC 6690-2 |
| `serum_creatinine_mg_dl` | `measurement` | LOINC 2160-0 or 38483-4 |
| `creatinine_clearance_ml_min` | `measurement` | LOINC 2164-2 |
| `serum_calcium_mg_dl` | `measurement` | LOINC 17861-6 or 49765-1 |
| `bilirubin_total_mg_dl` | `measurement` | LOINC 1975-2 |
| `ast_u_l` | `measurement` | LOINC 1920-8 |
| `alt_u_l` | `measurement` | LOINC 1742-6 |
| `albumin_g_dl` | `measurement` | LOINC 1751-7 |
| `her2_status` | `measurement` | LOINC 48676-1 |
| `estrogen_receptor_status` | `measurement` | LOINC 16112-5 |
| `progesterone_receptor_status` | `measurement` | LOINC 16113-3 |

The raw OMOP path issues one correlated subquery per field against the
appropriate table. The PatientRecord path reads all 20 fields in a single
`.values()` call on the indexed `patient_record` table.

**Example output.** Verbatim from a run against the 1,000-patient reference cohort
(PostgreSQL 14, Apple Silicon, warm cache, `--repeat 3`):

```
Benchmarking 1000 patient(s), 3 repeat pass(es) per path...

patient_record pull: {'n': 3000, 'mean_ms': 0.313, 'median_ms': 0.294, 'p95_ms': 0.385, 'min_ms': 0.275, 'max_ms': 1.148}
OMOP pull:           {'n': 3000, 'mean_ms': 11.239, 'median_ms': 11.031, 'p95_ms': 13.07, 'min_ms': 7.381, 'max_ms': 67.685}

patient_record is ~35.9x faster than the OMOP pull (mean 0.313ms vs 11.239ms)
Avg populated eligibility fields: patient_record=15.0/20, OMOP=15.0/20
```

This is the measurement behind the paper's headline figure (≈0.30 ms vs ≈11.0 ms, ~37×).
Cohort size matters here: the ratio grows with the size of the `measurement` table the raw-OMOP
path has to search, because the `PatientRecord` path is a single indexed row read regardless.
The 1,000-patient cohort carries ~217k measurement rows; a 100-patient cohort carries ~17k and
produces a correspondingly smaller ratio. Use `--count 1000` in Step 1 to compare against the
published number.

The JSON output (`trial-eligibility-results.json`) has this shape:

```json
{
  "patient_record": {"n": 300, "mean_ms": 1.4, "median_ms": 1.2, "p95_ms": 2.8, "min_ms": 0.9, "max_ms": 6.1},
  "omop":           {"n": 300, "mean_ms": 9.7, "median_ms": 8.9, "p95_ms": 18.3, "min_ms": 5.4, "max_ms": 42.6},
  "person_ids":     [1001, 1002, "..."],
  "repeat":         3,
  "org_slugs":      ["synthea-bc"],
  "criteria_fields": ["patient_age", "gender", "..."]
}
```

---

## Step 4 — Full PatientRecord benchmark

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py benchmark_patient_record \
    --org-slugs synthea-bc \
    --disease-filter "" \
    --repeat 3 \
    --output full-patient-record-results.json
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--org-slugs` | `abc-foundation,bbc-foundation` | Comma-separated org slugs |
| `--person-ids` | — | Override cohort with explicit `person_id` list |
| `--disease-filter` | `breast` | PatientRecord.disease substring filter; pass `""` for all diseases |
| `--limit N` | all | Cap cohort size |
| `--repeat N` | 1 | Repeat timed passes N times |
| `--output FILE` | — | Write raw JSON stats to file |

**What the live OMOP derivation runs:**

The OMOP-direct path calls these 19 section functions (in order), each issuing
independent queries against the OMOP tables:

| # | Section function | OMOP tables queried |
|---|------------------|-------------------|
| 1 | `_get_demographics` | `person`, `person_language_skill` |
| 2 | `_get_location_data` | `location` (via `person.location_id`) |
| 3 | `_get_disease_data` | `condition_occurrence` |
| 4 | `_get_treatment_data` | `drug_exposure`, `episode`, `episode_event` |
| 5 | `_get_vitals_data` | `measurement` |
| 6 | `_get_biomarker_data` | `measurement`, `observation` |
| 7 | `_get_staging_data` | `measurement`, `observation` |
| 8 | `_get_social_data` | `observation` |
| 9 | `_get_behavior_data` | `measurement` |
| 10 | `_get_infection_data` | `condition_occurrence` |
| 11 | `_get_assessment_data` | `observation` |
| 12 | `_get_laboratory_data` | `measurement` |
| 13 | `_get_performance_data` | `observation` |
| 14 | `_get_genetic_mutations` | `measurement` |
| 15 | `_get_cll_data` | `measurement`, `observation`, `drug_exposure`, `condition_occurrence` |
| 16 | `_get_lymphoma_data` | `measurement`, `observation`, `condition_occurrence` |
| 17 | `_get_bc_clinical_data` | `observation` |
| 18 | `_get_prior_procedures` | `procedure_occurrence` |
| 19 | `_get_wearable_data` | `measurement`, `observation` |

After all sections run, `_compute_derived_fields` adds `bmi` (height + weight)
and `tp53_disruption` (from genetic mutations) without additional DB queries.

**Example output:**

```
Benchmarking 100 patient(s), 3 repeat pass(es) per path...

patient_record read: {'n': 300, 'mean_ms': 1.8, 'median_ms': 1.5, 'p95_ms': 3.4, ...}
OMOP-direct derive:  {'n': 300, 'mean_ms': 84.2, 'median_ms': 79.6, 'p95_ms': 131.7, ...}

patient_record is ~46.8x faster than live OMOP derivation (mean 1.8ms vs 84.2ms)
Avg populated fields per OMOP derivation: 67.3 (out of 19 sections called)
```

The full-derivation speedup is much larger than the trial-eligibility speedup
because 19 sections × multiple queries each compounds the OMOP overhead, while
the PatientRecord read cost is essentially constant regardless of how many
fields are requested.

The JSON output (`full-patient-record-results.json`) has this shape:

```json
{
  "patient_record": {"n": 300, "mean_ms": 1.8, ...},
  "omop_direct":    {"n": 300, "mean_ms": 84.2, ...},
  "person_ids":     [1001, 1002, "..."],
  "repeat":         3,
  "disease_filter": ""
}
```

---

## OMOP → PatientRecord field reference

This table documents every OMOP source that feeds a PatientRecord column.
All "most recent" lookups order by `*_date DESC` and take the first row.

### Demographics — `_get_demographics`

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `patient_age` | `person` | `year_of_birth` (subtracted from current year) |
| `gender` | `person` | `gender_concept.concept_name` → M/F/U; fallback: `gender_source_value` |
| `race` | `person` | `race_concept.concept_name`; fallback: `race_source_value` |
| `ethnicity` | `person` | `ethnicity_concept.concept_name`; fallback: `ethnicity_source_value` |
| `languages_skills` | `person_language_skill` | Comma-joined `language_concept.concept_name: skill_level` pairs |

### Location — `_get_location_data`

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `country` | `location` | `location.country` (via `person.location_id`) |
| `region` | `location` | `location.state` |
| `city` | `location` | `location.city` |
| `postal_code` | `location` | `location.zip` |
| `latitude` | `location` | `location.latitude` |
| `longitude` | `location` | `location.longitude` |

### Disease — `_get_disease_data`

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `disease` | `condition_occurrence` | Most-recent row where `condition_concept.concept_name` icontains cancer / neoplasm / malignant / lymphoma / leukemia / myeloma / carcinoma / sarcoma / tumor |
| `diagnosis_date` | `condition_occurrence` | `condition_start_date` of the matched cancer condition; fallback: earliest `condition_start_date` for any condition |
| `condition_clinical_status` | `condition_occurrence` | Most-recent `condition_status_concept.concept_name` or `condition_status_source_value` mapped to: active / remission / relapse / resolved |
| `disease_slug` | — | URL-safe slug derived from `disease` field |

### Treatment lines — `_get_treatment_data`

Primary path uses `episode` + `episode_event` (OMOP Oncology extension) when
available; fallback groups `drug_exposure` rows into regimen windows.

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `first_line_therapy` | `drug_exposure` / `episode` | Regimen name for episode_number=1 (or earliest drug window) |
| `first_line_therapy_id` | `episode` / `concept` | HemOnc concept_id for the regimen |
| `first_line_date` | `episode` / `drug_exposure` | `episode_start_date` or `drug_exposure_start_date` |
| `first_line_start_date` | `episode` | `episode_start_date` |
| `first_line_end_date` | `episode` | `episode_end_date` |
| `second_line_therapy` | `drug_exposure` / `episode` | Regimen name for episode_number=2 |
| `second_line_therapy_id` | `episode` / `concept` | HemOnc concept_id |
| `second_line_date` | `episode` / `drug_exposure` | Start date |
| `second_line_start_date` | `episode` | `episode_start_date` |
| `second_line_end_date` | `episode` | `episode_end_date` |
| `later_therapy` | `drug_exposure` / `episode` | First regimen with episode_number ≥ 3 |
| `later_date` | `episode` / `drug_exposure` | Start date of first later-line regimen |
| `later_therapies` | `drug_exposure` / `episode` | JSON array of all regimens with episode_number ≥ 3 |
| `later_therapy_ids` | `episode` / `concept` | JSON array of HemOnc concept_ids |
| `concomitant_medications` | `drug_exposure` | Up to 5 most-recent drug concept names |

### Vitals — `_get_vitals_data`

| PatientRecord field | OMOP table | LOINC |
|---------------------|------------|-------|
| `systolic_blood_pressure` | `measurement` | 8480-6 |
| `diastolic_blood_pressure` | `measurement` | 8462-4 |
| `heartrate` | `measurement` | 8867-4 |
| `weight` | `measurement` | 29463-7 |
| `height` | `measurement` | 8302-2 |
| `temperature` | `measurement` | 8310-5 |

(`weight_units`, `height_units` are derived from `unit_source_value` on the
same measurement row.)

### Biomarkers — `_get_biomarker_data`

| PatientRecord field | OMOP table | LOINC / concept filter |
|---------------------|------------|----------------------|
| `estrogen_receptor_status` | `measurement` | 16112-5 |
| `progesterone_receptor_status` | `measurement` | 16113-3 |
| `her2_status` | `measurement` | 48676-1 |
| `ki67_proliferation_index` | `measurement` | 85319-2 |
| `histologic_type` | `measurement` | 59847-4 |
| `pd_l1_tumor_cells` | `measurement` / `observation` | 85147-7 / concept name icontains `pd-l1` |
| `pd_l1_ic_percentage` | `measurement` | 85309-3 |
| `pd_l1_combined_positive_score` | `measurement` | 85310-1 |
| `pd_l1_assay` | `observation` | concept name icontains `pd-l1 assay` |
| `menopausal_status` | `observation` | concept name icontains `menopausal` |
| `tnbc_status` | `observation` | concept name icontains `triple negative` |
| `hr_status` | `observation` | concept name icontains `hormone receptor` |
| `hrd_status` | `observation` | concept name icontains `homologous recombination` |

### Staging — `_get_staging_data`

| PatientRecord field | OMOP table | LOINC |
|---------------------|------------|-------|
| `stage` | `measurement` / `observation` | 21908-9 (overall clinical stage) |
| `tumor_stage` | `measurement` / `observation` | 21905-5 (T-stage) |
| `nodes_stage` | `measurement` / `observation` | 21906-3 (N-stage) |
| `distant_metastasis_stage` | `measurement` / `observation` | 21901-4 (M-stage) |
| `metastatic_status` | `observation` | concept name icontains `metastatic` |
| `bone_only_metastasis_status` | `observation` | concept name icontains `bone only` |
| `staging_modalities` | `observation` | concept name icontains `staging` |

### Laboratory results — `_get_laboratory_data`

All "most recent non-null `value_as_number`" from `measurement`, matched first
by `measurement_concept.concept_code` (LOINC), then by
`measurement_source_value` if no concept match is found.

#### Complete blood count (CBC)

| PatientRecord field | Primary LOINC | Alternate LOINC | Source-value alias |
|---------------------|--------------|----------------|-------------------|
| `hemoglobin_g_dl` | 718-7 | — | Hemoglobin [Mass/volume] in Blood |
| `hematocrit_percent` | 20570-8 | 4544-3 | Hematocrit [Volume Fraction] of Blood |
| `wbc_count_thousand_per_ul` | 6690-2 | — | Leukocytes [#/volume] in Blood · White blood cell count |
| `rbc_million_per_ul` | 789-8 | — | Erythrocytes [#/volume] in Blood · Red blood cell count |
| `platelet_count_thousand_per_ul` | 777-3 | — | Platelets [#/volume] in Blood · Platelets |
| `anc_thousand_per_ul` | 751-8 | — | Neutrophils [#/volume] in Blood · Absolute Neutrophil Count |
| `alc_thousand_per_ul` | 731-0 | — | Lymphocytes [#/volume] in Blood · Absolute Lymphocyte Count |
| `amc_thousand_per_ul` | 742-7 | — | Monocytes [#/volume] in Blood · Absolute Monocyte Count |

#### Comprehensive metabolic panel (CMP)

| PatientRecord field | Primary LOINC | Alternate LOINC | Source-value alias |
|---------------------|--------------|----------------|-------------------|
| `serum_calcium_mg_dl` | 17861-6 | 49765-1 | Calcium [Mass/volume] in Serum or Plasma · Serum Calcium · Calcium |
| `serum_creatinine_mg_dl` | 2160-0 | 38483-4 | Creatinine [Mass/volume] in Serum or Plasma · Serum Creatinine · Creatinine |
| `creatinine_clearance_ml_min` | 2164-2 | — | Creatinine Clearance |
| `egfr_ml_min_173m2` | 62238-1 | 33914-3 | GFR/BSA pred CKD-EPI ArA |
| `bun_mg_dl` | 3094-0 | 6299-2 | Urea nitrogen [Mass/volume] in Serum or Plasma · Blood Urea Nitrogen |
| `sodium_meq_l` | 2951-2 | 2947-0 | Sodium [Moles/volume] in Serum or Plasma · Sodium |
| `potassium_meq_l` | 2823-3 | 6298-4 | Potassium [Moles/volume] in Serum or Plasma · Potassium |
| `magnesium_mg_dl` | 2601-3 | — | Magnesium [Mass/volume] in Serum or Plasma · Magnesium |
| `glucose_mg_dl` | 2345-7 | 2339-0 | Glucose [Mass/volume] in Serum or Plasma · Glucose |

#### Liver function / cardiac / other

| PatientRecord field | Primary LOINC | Alternate LOINC | Source-value alias |
|---------------------|--------------|----------------|-------------------|
| `bilirubin_total_mg_dl` | 1975-2 | — | Bilirubin.total [Mass/volume] in Serum or Plasma · Total Bilirubin |
| `alt_u_l` | 1742-6 | — | Alanine aminotransferase … · ALT |
| `ast_u_l` | 1920-8 | — | Aspartate aminotransferase … · AST |
| `alkaline_phosphatase_u_l` | 6768-6 | — | Alkaline phosphatase … · Alkaline Phosphatase |
| `albumin_g_dl` | 1751-7 | — | Albumin [Mass/volume] in Serum or Plasma · Albumin |
| `total_protein` | 2885-2 | — | Protein [Mass/volume] in Serum or Plasma |
| `troponin_ng_ml` | 10839-9 | — | Troponin I.cardiac … |
| `bnp_pg_ml` | 42637-9 | — | BNP [Mass/volume] in Serum or Plasma |
| `hba1c_percent` | 4548-4 | — | Hemoglobin A1c/Hemoglobin.total in Blood · HbA1c |
| `ldh_u_l` | 2532-0 | — | Lactate dehydrogenase … · LDH |
| `beta2_microglobulin` | 1952-1 | — | Beta-2-Microglobulin … |
| `c_reactive_protein` | 1988-5 | — | C reactive protein … |
| `esr` | 30341-2 | — | Erythrocyte sedimentation rate |

#### Multiple myeloma disease burden

| PatientRecord field | LOINC | Description |
|---------------------|-------|-------------|
| `monoclonal_protein_serum` | 51435-6 | Serum M-spike (M-protein) |
| `monoclonal_protein_urine` | 32730-5 | Urine M-spike 24 h |
| `kappa_flc` | 33944-8 | Kappa free light chains |
| `lambda_flc` | 33945-5 | Lambda free light chains |
| `clonal_plasma_cells` | 26098-4 | Plasma cells % in bone marrow |

### Behavior / lifestyle — `_get_behavior_data`

All from `measurement` matched by LOINC code; `value_as_string` for
categorical fields, `value_as_number` for numeric.

| PatientRecord field | LOINC | Type |
|---------------------|-------|------|
| `smoking_status` | 72166-2 | str |
| `pack_years` | 63640-7 | float |
| `alcohol_use` | 74013-4 | str |
| `drinks_per_week` | 11286-7 | int |
| `exercise_frequency` | 68516-4 | str |
| `exercise_minutes_per_week` | 89555-7 | int |
| `diet_type` | 88365-2 | str |
| `sleep_quality` | 93831-6 | str |
| `stress_level` | 73985-4 | str |
| `social_support` | 93033-9 | str |
| `employment_status` | 74165-2 | str |
| `education_level` | 82589-3 | str |
| `marital_status` | 45404-1 | str |
| `insurance_type` | 76513-1 | str |
| `number_of_dependents` | 63512-8 | int |
| `annual_household_income` | 77243-3 | int |

### Performance status — `_get_performance_data`

| PatientRecord field | OMOP table | Concept name filter |
|---------------------|------------|-------------------|
| `ecog_performance_status` | `observation` | concept name icontains `ecog` |
| `karnofsky_performance_score` | `observation` | concept name icontains `karnofsky` |

### Assessment — `_get_assessment_data`

| PatientRecord field | OMOP table | Concept name filter |
|---------------------|------------|-------------------|
| `measurable_disease_by_recist_status` | `observation` | concept name icontains `measurable_disease` |

### Genetic mutations — `_get_genetic_mutations`

| PatientRecord field | OMOP table | LOINC codes |
|---------------------|------------|------------|
| `genetic_mutations` | `measurement` | 21636-6 (BRCA1), 21637-4 (BRCA2), 21667-1 (TP53), 12375-4 (KRAS), 81704-2 (EGFR), 62318-1 (PIK3CA) |

Result is a JSON array: `[{gene, origin, interpretation}, …]`

`_compute_derived_fields` adds `tp53_disruption` (boolean) derived from
`genetic_mutations` without further DB queries.

### Wearable / device data — `_get_wearable_data`

30-day rolling window; artifact filtering applied before aggregation.
Metrics require ≥ 7 valid days (`WEARABLE_MIN_VALID_DAYS`) to be emitted.

| PatientRecord field | OMOP table | LOINC | Unit |
|---------------------|------------|-------|------|
| `median_daily_steps_30d` | `measurement` | 55423-8 | steps/day |
| `active_minutes_per_day_30d` | `measurement` | 77592-4 | min/day |
| `resting_heart_rate_avg_30d` | `measurement` | 40443-4 | bpm |
| `hrv_sdnn_avg_30d` | `measurement` | 80404-7 | ms |
| `oxygen_saturation_min_30d` | `measurement` | 59408-5 | % (min over window) |
| `respiratory_rate_avg_30d` | `measurement` | 9279-1 | breaths/min |
| `sleep_duration_hours_avg_30d` | `observation` | 93832-4 | hours |
| `wearable_last_sync_at` | `measurement` | any of the above | most-recent date |
| `wearable_coverage_ratio_30d` | `measurement` | any of the above | fraction of 30 days with ≥ 1 reading |
| `activity_trend_30d` | `measurement` | 55423-8 | improving / declining / stable (vs. first-half average) |

**Artifact bounds applied before aggregation:**

| Metric | Min | Max |
|--------|-----|-----|
| SpO₂ | 70.0 % | 100.0 % |
| Resting HR | 20 bpm | 300 bpm |
| HRV SDNN | 1 ms | 300 ms |
| Respiratory rate | 4 /min | 60 /min |
| Steps | 0 | 100,000 |
| Active minutes | 0 | 1,440 |
| Sleep duration | 0 h | 24 h |

### MM-specific — `_get_mm_specific_data`

| PatientRecord field | OMOP table | LOINC |
|---------------------|------------|-------|
| `plasma_cell_leukemia` | `measurement` | 47082-2 |
| `bone_lesions` | `measurement` | 24646-7 |
| `meets_crab` | `measurement` | 89599-5 |
| `meets_slim` | — | derived from `kappa_flc`, `lambda_flc`, `clonal_plasma_cells` |

### CLL-specific — `_get_cll_data`

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `absolute_lymphocyte_count` | `measurement` | LOINC 731-0 |
| `serum_beta2_microglobulin_level` | `measurement` | LOINC 1952-1 / 48094-6 |
| `binet_stage` | `observation` | concept name icontains `binet` |
| `tumor_burden` | `observation` | concept name icontains `tumor burden` |
| `disease_activity` | `observation` | concept name icontains `disease activity` |
| `bone_marrow_involvement` | `observation` | concept name icontains `bone marrow` |
| `hepatomegaly` | `observation` | concept name icontains `hepatomegaly` |
| `splenomegaly` | `observation` | concept name icontains `splenomegaly` |
| `lymphadenopathy` | `observation` | concept name icontains `lymphadenopathy` |
| `btk_inhibitor_refractory` | `drug_exposure` | drug name icontains ibrutinib / acalabrutinib / zanubrutinib |
| `bcl2_inhibitor_refractory` | `drug_exposure` | drug name icontains venetoclax |
| `lymphocyte_doubling_time` | `observation` | concept name icontains `doubling time` |

### Lymphoma-specific — `_get_lymphoma_data`

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `flipi_score` | `observation` | concept name icontains `flipi` |
| `gelf_criteria_status` | `observation` | concept name icontains `gelf` |
| `tumor_grade` | `observation` / `measurement` | concept name icontains `tumor grade` |
| `lugano_stage` | `observation` | concept name icontains `lugano` |

### Breast cancer clinical — `_get_bc_clinical_data`

| PatientRecord field | OMOP table | Concept filter |
|---------------------|------------|---------------|
| `peripheral_neuropathy_grade` | `observation` | concept name icontains `peripheral neuropathy` |
| `toxicity_grade` | `observation` | concept name icontains `toxicity` |
| `renal_adequacy_status` | `observation` | concept name icontains `renal adequacy` |

### Infection — `_get_infection_data`

| PatientRecord field | OMOP table | Concept filter |
|---------------------|------------|---------------|
| `infection_status` | `condition_occurrence` | concept name icontains infection / sepsis / fever / pneumonia / covid |
| `infection_type` | `condition_occurrence` | `condition_concept.concept_name` |
| `infection_date` | `condition_occurrence` | `condition_start_date` |

### Prior procedures — `_get_prior_procedures`

| PatientRecord field | OMOP table | Source |
|---------------------|------------|--------|
| `prior_procedures` | `procedure_occurrence` | JSON array of `procedure_concept.concept_name` values, ordered by `procedure_date` |

### Social — `_get_social_data`

| PatientRecord field | OMOP table | Concept name filter |
|---------------------|------------|-------------------|
| `marital_status` | `observation` | icontains `marital` |
| `insurance_type` | `observation` | icontains `insurance` |
| `housing_status` | `observation` | icontains `housing` |
| `annual_household_income` | `observation` | icontains `income` |
| `employment_status` | `observation` | icontains `employment` |
| `caregiver_type` | `observation` | icontains `caregiver` |

### Computed (no additional DB queries)

| PatientRecord field | Source |
|---------------------|--------|
| `bmi` | Derived from `weight` (kg) and `height` (cm): weight / (height/100)² |
| `tp53_disruption` | Boolean: true if `genetic_mutations` contains a TP53 entry |

---

## Running the steps separately

`generate_import_enrich_synthea_bc` is a wrapper. To run its three stages individually — to
import a bundle you already have, for instance:

```bash
# Generate only
python manage.py generate_synthea_bc --count 100 --output /tmp/synthea_bc_100.json

# Import an existing FHIR Bundle into OMOP under an org (creates the org if needed)
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py import_fhir_bundle \
    --file /tmp/synthea_bc_100.json \
    --org synthea-bc \
    --batch-size 10
```

`import_fhir_bundle` also takes `--directory` for a tree of per-patient bundles and
`--start-from N` to resume after a failure.

---

## Troubleshooting

**`No matching patients found` from a benchmark command** — verify the import:
```bash
DATABASE_URL="..." python manage.py shell -c "
from omop_core.models import PatientRecord
print(PatientRecord.objects.filter(organization__slug='synthea-bc').count())
"
```

**High concept remapping count during import** — expected on a fresh DB with
no Athena vocabulary loaded. The benchmarks use `*_source_value` as fallback
for all LOINC lookups, so concept remapping does not affect benchmark validity.

**Absolute latency higher than published numbers** — query latency is
hardware- and cache-dependent. The published numbers used an Apple M-series
MacBook Pro with PostgreSQL 14 and a warm cache (`--repeat 3`). The
**relative speedup ratio** between the two paths is the reproducible result;
absolute millisecond values will vary.
