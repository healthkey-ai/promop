# Synthea Breast Cancer Import — Field Completeness Gaps

**Date:** 2026-07-07
**File analysed:** `data/synthea_bc_200.json` (200 patients, 13 391 entries)
**Comparison orgs:** synthea vs ABC Foundation / BBC Foundation

---

## Executive Summary

The Synthea bundle is nearly empty of the clinical resources that PRomop depends on.
Out of the 13 391 FHIR entries across 200 patients, the bundle contains:

| ResourceType | Count |
|---|---|
| Immunization | 2 809 |
| Encounter | 1 866 |
| DiagnosticReport | 1 866 |
| DocumentReference | 1 866 |
| Claim | 1 866 |
| ExplanationOfBenefit | 1 866 |
| Location | 208 |
| Practitioner / PractitionerRole / Organization | 207 each |
| Patient | 200 |
| Provenance | 200 |
| Procedure | 22 |
| **Condition** | **1** |
| **Observation** | **0** |
| **MedicationRequest / MedicationStatement** | **0** |

None of the per-patient bundles contains a single `Observation` or medication resource.
Only 1 out of 200 patients has a `Condition` resource at all.

---

## Root Cause: Synthea Export Configuration

Synthea was run with a default or minimal export configuration that:

1. **Did not enable the `csv` exporter** — or the FHIR exporter was not configured with
   `exporter.fhir.export_conditions = true`, `export.fhir.export_observations = true`,
   `export.fhir.export_medications = true`.
2. The bundle appears to be a **Continuity-of-Care (C-CDA / clinical note) export** rather
   than a full FHIR R4 clinical-data export. The `DiagnosticReport` entries are plain text
   notes (base64-encoded `presentedForm`), not structured lab panels.
3. The Synthea `--exporter.fhir.export_path` likely used the default Synthea module which
   only emits encounters, immunisations, and document references for the "breast cancer"
   longitudinal simulation, not a mCODE-aligned or structured oncology export.

Concretely, Synthea _does_ generate Conditions, Observations, and MedicationRequests — but
only when the FHIR R4 exporter is told to include them, or when using the `--exporter.fhir.bulk_data=true` flag.

---

## Field-by-Field Gap Analysis

### 1. `disease` / `diagnosis_date` / `condition_clinical_status`

**What PRomop needs:**
- A FHIR `Condition` resource with SNOMED code `254837009`
  ("Malignant neoplasm of breast") or any condition whose display contains "breast".
- Handler: `views.py` lines 1000–1064 → `_get_disease_data()` in `patient_record_service.py`.

**What Synthea emits:**
- Only 1 `Condition` across all 200 patients (SNOMED 254837009 — correct code, but present
  for just 1 patient). The other 199 patients have zero `Condition` resources.

**Impact:** `disease`, `diagnosis_date`, and `condition_clinical_status` will be NULL for
199/200 Synthea patients.

---

### 2. CBC labs — `hemoglobin_g_dl`, `wbc_count_thousand_per_ul`, `platelet_count_thousand_per_ul`, `rbc_million_per_ul`, `hematocrit_percent`, `anc_thousand_per_ul`

**What PRomop needs:**
- FHIR `Observation` resources with LOINC codes:

  | Field | LOINC |
  |---|---|
  | `hemoglobin_g_dl` | 718-7 |
  | `hematocrit_percent` | 4544-3 or 20570-8 |
  | `wbc_count_thousand_per_ul` | 6690-2 |
  | `rbc_million_per_ul` | 789-8 |
  | `platelet_count_thousand_per_ul` | 777-3 |
  | `anc_thousand_per_ul` | 751-8 |
  | `alc_thousand_per_ul` | 731-0 |
  | `amc_thousand_per_ul` | 742-7 |

**What Synthea emits:** 0 `Observation` resources. No CBC data at all.

**Impact:** All eight CBC fields NULL for all 200 patients.

---

### 3. CMP labs — `serum_creatinine_mg_dl`, `egfr_ml_min_173m2`, `bun_mg_dl`, `sodium_meq_l`, `potassium_meq_l`, `serum_calcium_mg_dl`

**What PRomop needs:**
- FHIR `Observation` with LOINC codes:

  | Field | LOINC |
  |---|---|
  | `serum_creatinine_mg_dl` | 2160-0 or 38483-4 |
  | `egfr_ml_min_173m2` | 62238-1 or 33914-3 |
  | `bun_mg_dl` | 3094-0 or 6299-2 |
  | `sodium_meq_l` | 2951-2 or 2947-0 |
  | `potassium_meq_l` | 2823-3 or 6298-4 |
  | `serum_calcium_mg_dl` | 17861-6 or 49765-1 |

**What Synthea emits:** 0 `Observation` resources.

**Impact:** All CMP fields NULL for all 200 patients.

---

### 4. `ldh_u_l`

**What PRomop needs:** LOINC 2532-0 or 14804-9.

**What Synthea emits:** 0 `Observation` resources.

**Impact:** NULL for all 200 patients.

---

### 5. Receptor status — `estrogen_receptor_status`, `progesterone_receptor_status`, `her2_status`, `tnbc_status`

**What PRomop needs:**
- FHIR `Observation` (treated as `Measurement` in OMOP) with:
  - ER: LOINC 16112-5 → `valueCodeableConcept` with text "Positive" / "Negative"
  - PR: LOINC 16113-3
  - HER2: LOINC 48676-1
- Handler: `views.py` lines 1397–1408; `patient_record_service.py` `_get_biomarker_data()`.

**What Synthea emits:** 0 `Observation` resources. (Standard Synthea does not emit receptor
status observations in its default breast cancer module.)

**Impact:** `er_status`, `pr_status`, `her2_status`, `tnbc_status` all NULL. This is the
most clinically significant gap for a breast cancer cohort.

---

### 6. `stage` (TNM / clinical stage group)

**What PRomop needs (two paths):**
- Path A — `Condition.stage[].summary.text` e.g. "Breast Cancer Stage IIB".
- Path B — `Observation` with LOINC 21908-9 (mCODE TNM clinical stage group) →
  `valueCodeableConcept.text` e.g. "Stage 2B".
- Handler: `views.py` lines 1025–1042 and 1485–1492.

**What Synthea emits:**
- Only 1 `Condition` (for 1 patient), which contains no `stage` array.
- 0 TNM `Observation` resources.

**Impact:** `stage` NULL for all 200 patients.

Note: Standard Synthea does not emit TNM observations; it records stage only as a SNOMED
`Condition` code representing the staged entity (e.g. SNOMED 413448000 — "Breast cancer,
stage IIa"). The handler does not parse these stage-specific SNOMED codes; it only looks at
`Condition.stage[].summary.text` and LOINC 21908-9.

---

### 7. `first_line_therapy` / `second_line_therapy` / therapy dates

**What PRomop needs:**
- FHIR `MedicationStatement` or `MedicationRequest` resources with drug names resolvable
  via RxNorm/RxNav or the HemOnc concept table.
- Handler: `views.py` lines 773–777 (groups medications per patient); then
  `patient_record_service.py` `_get_treatment_data()` rebuilds therapy lines from
  `DrugExposure` OMOP rows.

**What Synthea emits:** 0 `MedicationRequest` or `MedicationStatement` resources. (The
DiagnosticReport note text for one patient says "No Active Medications".)

**Impact:** `first_line_therapy`, `second_line_therapy`, `later_therapy`, all date fields,
`therapy_lines_count` — all NULL for all 200 patients.

---

### 8. `tumor_grade`

**What PRomop needs:** A `Measurement` row where `measurement_concept.concept_name` contains
"grade" and "lymphoma" (lymphoma-specific in current service); or an observation with text
matching "grade" in the lymphoma service path.

**What Synthea emits:** 0 `Observation` resources. Synthea also does not emit grading
observations in its standard breast cancer module.

**Impact:** NULL for all 200 patients. (This field is currently only populated via the
lymphoma path in `patient_record_service.py`; a separate breast cancer grading path does
not yet exist.)

---

### 9. Vitals — `weight`, `height`, `systolic_blood_pressure`, `diastolic_blood_pressure`, `heartrate`

**What PRomop needs:**
- Path A — `Patient.extension` with URLs under `https://healthkey.ai/fhir/StructureDefinition/`
  (proprietary extensions used by the internal FHIR generator).
- Path B — `Observation` with LOINC codes 29463-7 (weight), 8302-2 (height), 8480-6
  (systolic BP), 8462-4 (diastolic BP), 8867-4 (heart rate).
- Handler: `views.py` lines 862–930 (Patient extension path) and
  `patient_record_service.py` `_get_vitals_data()` (LOINC Observation path).

**What Synthea emits:**
- Patient extensions use US Core URLs (`http://hl7.org/fhir/us/core/StructureDefinition/us-core-race` etc.), not the internal `healthkey.ai` extension URLs. The handler _does_ correctly parse US Core race/ethnicity (lines 901–919), but not US Core vital-sign extensions.
- Synthea standard export does not emit vital-sign observations without the `observations` module enabled.

**Impact:** `weight`, `height`, BP, and heart rate NULL for all 200 patients. `race` and
`ethnicity` _will_ populate correctly (US Core extensions are handled).

---

### 10. `ecog_performance_status`

**What PRomop needs:** LOINC 89247-1 in an `Observation`, or `Patient.extension`
`https://healthkey.ai/fhir/StructureDefinition/ecog-performance-status`.

**What Synthea emits:** Neither. Synthea does not emit ECOG observations.

**Impact:** NULL for all 200 patients.

---

### 11. Genetic mutations (BRCA1/2, TP53, PIK3CA)

**What PRomop needs:** `Observation` / `Measurement` with LOINC codes:
21636-6 (BRCA1), 21637-4 (BRCA2), 21667-1 (TP53), 62318-1 (PIK3CA).

**What Synthea emits:** 0 `Observation` resources. Synthea's genomics module exists but
requires explicit configuration; it is not enabled in the standard breast cancer export.

**Impact:** `genetic_mutations` empty list for all 200 patients.

---

### 12. Procedures (Mammography, Mastectomy, etc.)

**What PRomop needs:** `ProcedureOccurrence` rows → `prior_procedures` JSONField.

**What Synthea emits:** 22 `Procedure` resources across all 200 patients — only
SNOMED 25656009 ("Physical examination, complete") and 71651007 ("Mammography"). Both are
correctly parseable by the handler (SNOMED codes are written to `ProcedureOccurrence`).
Mastectomy (SNOMED 172043006), chemotherapy administration (SNOMED 367336001), and
radiation therapy (SNOMED 33195004) are absent.

**Impact:** `prior_procedures` will be sparse (examinations and mammography only), with
no surgical or oncologic treatment procedures.

---

## Summary Table

| PatientRecord field | Expected source | Synthea emits? | Result for synthea org |
|---|---|---|---|
| `disease` | Condition (SNOMED 254837009) | 1/200 patients | NULL for 199/200 |
| `diagnosis_date` | Condition.onsetDateTime | 1/200 patients | NULL for 199/200 |
| `stage` | Condition.stage.summary OR LOINC 21908-9 | Neither | NULL for all |
| `estrogen_receptor_status` | LOINC 16112-5 Observation | No | NULL for all |
| `progesterone_receptor_status` | LOINC 16113-3 Observation | No | NULL for all |
| `her2_status` | LOINC 48676-1 Observation | No | NULL for all |
| `tnbc_status` | Derived from ER/PR/HER2 | No | NULL for all |
| `hemoglobin_g_dl` | LOINC 718-7 Observation | No | NULL for all |
| `wbc_count_thousand_per_ul` | LOINC 6690-2 Observation | No | NULL for all |
| `platelet_count_thousand_per_ul` | LOINC 777-3 Observation | No | NULL for all |
| `serum_creatinine_mg_dl` | LOINC 2160-0 Observation | No | NULL for all |
| `egfr_ml_min_173m2` | LOINC 62238-1/33914-3 Observation | No | NULL for all |
| `ldh_u_l` | LOINC 2532-0/14804-9 Observation | No | NULL for all |
| `first_line_therapy` | MedicationStatement/Request | No | NULL for all |
| `second_line_therapy` | MedicationStatement/Request | No | NULL for all |
| `ecog_performance_status` | LOINC 89247-1 / Patient ext | No | NULL for all |
| `weight` / `height` | LOINC 29463-7/8302-2 OR Patient ext | No | NULL for all |
| `genetic_mutations` | LOINC 21636-6/21637-4 etc. Measurement | No | Empty list for all |
| `prior_procedures` | Procedure (SNOMED) | Partial (2 codes) | Sparse |
| `race` / `ethnicity` | US Core Patient extension | Yes | **Populated** |
| `gender` / `date_of_birth` | Patient demographics | Yes | **Populated** |

---

## Recommended Fix

**Fix the Synthea export, not the import handler.** The import handler correctly implements
the FHIR R4 / mCODE mapping. The data is simply absent from the bundle.

### Option A — Re-run Synthea with correct flags (preferred)

```bash
# Synthea CLI — enable all clinical modules
java -jar synthea-with-dependencies.jar \
  -p 200 \
  --exporter.fhir.export=true \
  --exporter.fhir.bulk_data=false \
  --exporter.fhir.export_conditions=true \
  --exporter.fhir.export_observations=true \
  --exporter.fhir.export_medications=true \
  --exporter.fhir.export_procedures=true \
  -m breast_cancer \
  Massachusetts
```

Or edit `src/main/resources/synthea.properties`:
```
exporter.fhir.export = true
exporter.fhir.export_conditions = true
exporter.fhir.export_observations = true
exporter.fhir.export_medications = true
exporter.fhir.export_procedures = true
```

### Option B — Use a mCODE-aligned Synthea export

Run Synthea with the `--igs hl7.fhir.us.mcode#2.1.0` flag, which enables the
mCODE Implementation Guide module and emits TNM staging observations (LOINC 21908-9),
receptor status observations (LOINC 16112-5/16113-3/48676-1), ECOG (LOINC 89247-1),
and genomics panel measurements. This is the format the import handler was designed for.

```bash
java -jar synthea-with-dependencies.jar \
  -p 200 \
  --igs hl7.fhir.us.mcode#2.1.0 \
  -m breast_cancer \
  Massachusetts
```

### Option C — Post-process existing bundle (workaround)

Write a script to augment the existing 200-patient bundle with synthetic clinical
observations (using realistic ranges for breast cancer patients). This avoids re-running
Synthea but produces synthetic data that is less correlated than a proper Synthea run.
Not recommended unless the Synthea source cannot be re-run.

---

## Handler Gaps (minor fixes needed regardless of data source)

Even after fixing the Synthea export, two handler gaps should be addressed:

1. **Stage-specific SNOMED codes on Condition resources.** Synthea emits stage as a
   SNOMED code on the `Condition.code` (e.g. 413448000 = "Breast cancer, stage IIa")
   rather than in `Condition.stage[].summary.text`. The handler only reads the `.stage[]`
   array and LOINC 21908-9; it does not map stage SNOMED codes on `Condition.code`.

2. **`tumor_grade` is lymphoma-only** in `patient_record_service.py` (`_get_lymphoma_data()`).
   Breast cancer histologic grade (Nottingham grade, LOINC 44648-4) is not extracted.
   A separate breast-cancer-grade path is needed.
