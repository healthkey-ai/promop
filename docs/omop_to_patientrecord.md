# OMOP to PatientRecord mapping

`PatientRecord` is a derived read model.  It is never a clinical write target.
Every source row must carry a known event date (and a source/provenance record);
unknown-date facts are not projected.  On refresh, absence of a backing fact
clears the projection value.

## Column mapping and implementation plan

| PatientRecord columns | OMOP table and coding | Derivation / implementation work |
|---|---|---|
| `date_of_birth`, `gender`, `race`, `ethnicity`, `languages_skills`, `email`, `phone_number`, `facility_name`, `validated`, `validated_by`, `validation_date`, `suppress_demographics_for_others` | `Person`, `Location`, `PersonLanguageSkill` | Copy current Person/location attributes. Profile APIs write Person, then refresh. |
| `disease`, `disease_slug`, `diagnosis_date`, `condition_code_icd_10`, `condition_code_snomed_ct`, `no_other_active_malignancies`, `preexisting_conditions`, `myeloma_type`, `progression` | `ConditionOccurrence`; ICD10CM/SNOMED/ICDO3 standard concepts; dated status `Observation` | Select relevant current condition; derive negatives only from explicit dated assertion. Add concept-set definitions for disease subtype/progression. |
| `first_line_*`, `second_line_*`, `later_*`, `prior_therapy`, `line_of_therapy`, `relapse_count`, `treatment_refractory_status`, `therapy_intent`, `reason_for_discontinuation`, `washout_period_duration` | `DrugExposure`, `Episode`, `EpisodeEvent`; dated `Observation` (`LOT-N-intent`, `LOT-N-outcome`, `LOT-N-discontinuation`) or LOINC `Measurement` 42804-5/91379-3 | Derive lines via Artemis/episode inference. Explicit line facts win; a standard LOINC fact is assigned only when its date falls in exactly one Episode interval. Washout is the minimum positive gap between persisted Episode intervals. HealthTree rules are an additional inference input, never a PatientRecord writer. |
| `planned_therapies`, `supportive_*`, `remission_duration_min` | pending treatment assertion registry | **Not enabled by #506.** These need a dated, line-associated concept set and must not be inferred from free text or from a PatientRecord patch. |
| `inr`, `pt_seconds`, `ptt_seconds`, `cea_ng_ml`, `ca19_9_u_ml`, `psa_ng_ml`, `phosphorus`, `absolute_neutrophile_count`, `red_blood_cell_count`, `creatinine_clearance_rate`, `estimated_glomerular_filtration_rate`, legacy bilirubin/liver/LDH aliases | `Measurement`; LOINC and UCUM units | Latest valid dated result. Canonical codes: 6301-6, 5902-2, 3173-2, 2039-6, 25390-6, 2857-1, 2777-1; aliases derive only from canonical result. |
| `heartrate_variability`, `qtcf_value`, `ejection_fraction`, `pulmonary_function_test_result`, `bone_imaging_result` | `Measurement`/`Observation`; LOINC where available, SNOMED for coded imaging result | Latest dated result; document selected LOINC/SNOMED concept set in mapping registry before writer support. |
| `first_line_*`, `second_line_*`, `later_*`, `prior_therapy`, `line_of_therapy`, `planned_therapies`, `supportive_*`, `relapse_count`, `treatment_refractory_status`, `therapy_intent`, `reason_for_discontinuation`, `remission_duration_min`, `washout_period_duration` | `DrugExposure`, `Episode`, `EpisodeEvent`; dated LOINC/SNOMED `Observation` for intent, outcome, discontinuation, washout | Derive lines via Artemis/episode inference; associate assertions to line start/end date. HealthTree rules are an additional inference input, never a PatientRecord writer. |
| `inr`, `pt_seconds`, `ptt_seconds`, `cea_ng_ml`, `ca19_9_u_ml`, `psa_ng_ml`, `phosphorus`, `absolute_neutrophile_count`, `red_blood_cell_count`, `creatinine_clearance_rate`, `estimated_glomerular_filtration_rate`, legacy bilirubin/liver/LDH aliases | `Measurement`; LOINC and UCUM units | Latest valid dated result. Canonical codes: 6301-6, 5902-2, 3173-2, 2039-6, 25390-6, 2857-1, 2777-1; aliases derive only from canonical result. #504 adds aliases `liver_enzyme_levels_ast` ← AST 1920-8, `liver_enzyme_levels_alt` ← ALT 1742-6, and `liver_enzyme_levels_alp` ← ALP 6768-6. |
| `heartrate_variability`, `qtcf_value`, `ejection_fraction` | `Measurement`; LOINC | **Implemented in #504.** Latest dated numeric result: HRV SDNN 80404-7 (milliseconds), QTcF 8632-1 (milliseconds), and ejection fraction 8806-2 (percent). HRV SDNN is not interchangeable with RMSSD; the latter remains only in `hrv_rmssd_avg_30d`. |
| `liver_enzyme_levels` | none | Retired ambiguous legacy composite. It cannot represent AST, ALT, and ALP without losing analyte identity, so it is permanently cleared and read-only; consumers must read the three distinct OMOP-derived fields. |
| `pulmonary_function_test_result`, `bone_imaging_result` | `Measurement`/`Observation`; LOINC/SNOMED | Pending a reviewed concept set and result semantics before writer support. |
| `molecular_markers`, `genetic_mutations` | `Measurement`; LOINC 21636-6 (BRCA1), 21640-8 (BRCA2), 21739-8 (TP53), 48013-7 (KRAS), 62862-8 (EGFR), 60033-8 (PIK3CA) | A dated variant result is the canonical structured `genetic_mutations` value. `molecular_markers` is a display-only summary of that same projection, never an independently writable value. |
| `test_methodology`, `oncotype_dx_score`, `test_date` | `Measurement`; LOINC 85337-4 | Latest dated report fact. Its coded/string result supplies methodology, its numeric result supplies score when present, and `measurement_date` supplies the report date. |
| `test_specimen_type`, `report_interpretation`, `androgen_receptor_status` | `Measurement`; LOINC 31208-2, 69548-6, 82185-1 | Latest dated coded result. Values come from `value_as_concept` or the persisted source result, not an independent projection patch. |
| `lymph_node_status`, `metastasis_status`, `biopsy_grade_depr` | `Measurement`; LOINC 92837-4, 21907-1, 44648-4 | Latest dated numeric/coded result. `biopsy_grade_depr` is the legacy string representation of the canonical numeric grade. |
| `tumor_size` | `Measurement`; LOINC 21889-1 (`Size Tumor`) | Latest dated numeric result. A legacy lymph-node row using this code must carry `qualifier_source_value=lymph-node`; code-only rows never populate both size fields. |
| `mrd_status` | `Observation`; OMOP Genomic, NCIt, or SNOMED concept whose name is “minimal residual disease” | Latest dated vocabulary-backed assertion. Free-text/local concepts are intentionally not projected. |
| `largest_lymph_node_size`, `spleen_size` | `Measurement`; LOINC 21889-1, 44996-6 | Latest dated numeric result (existing CLL extractor). |
| `pregnancy_test_date`, `pregnancy_test_result_value`, `contraceptive_use`, `consent_capability`, `caregiver_availability_status`, `no_mental_health_disorder_status`, `no_substance_use_status`, `no_geographic_exposure_risk` | dated LOINC-coded `Observation` or `Measurement` | **Implemented in #505.** Latest explicit value from reviewed question codes only: 2106-3, 8659-8, 75985-6, 74014-2, 75618-3, 74204-0, 82593-5. `no_*` fields invert the source risk/presence answer. No fact means `null`, never a fabricated negative. |
| `no_pregnancy_or_lactation_status`, `pregnancy_test_result`, `no_concomitant_medication_status`, `substance_use_details`, `geographic_exposure_risk_details`, `no_active_infection_status` | dated coded `Observation` | Pending a reviewed concept set and, for free text, a structured OMOP qualifier/value representation. These fields remain outside the #505 read-only set until that derivation exists. |
| `pregnancy_test_*`, `contraceptive_use`, `consent_capability`, `caregiver_availability_status`, `no_pregnancy_or_lactation_status`, `no_mental_health_disorder_status`, `no_concomitant_medication_status`, `no_substance_use_status`, `substance_use_details`, `no_geographic_exposure_risk`, `geographic_exposure_risk_details`, `no_active_infection_status` | dated coded `Observation` | Latest explicit assertion only; no fact means unknown, never a fabricated negative. |
| `id`, `person`, `organization`, `created_at`, `updated_at`, `derived_at`, `derivation_version`, `user_edited_fields` | server lifecycle metadata | Never writable through PatientRecord. `user_edited_fields` is inert legacy metadata. |

## Write policy

There are **no writable concrete PatientRecord columns**. A compatibility
endpoint may update a Person attribute (for example a name), but it writes the
source row and rederives; it never writes PatientRecord directly. `patient_info`
is legacy-only and must not gain consumers.

## Delivery order

1. Finish LOINC Measurement derivations and aliases.
2. Add the observation concept-set registry for eligibility, pathology and
   treatment assertions, with tests for date/provenance and stale clearing.
3. Add missing vocabulary packages (OMOP Genomic, ICDO3, NCIt/CTCAE where used)
   before accepting writers for their fields.
4. Reject all PatientRecord PATCH fields; rederive affected records and verify
   every mapping with OMOP-only fixtures.
