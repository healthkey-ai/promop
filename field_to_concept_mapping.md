# UI fields → PatientRecord → OMOP

Snapshot: 2026-09-11. UI and projection code: `dev` commit `7065a3f`; vocabulary and mapping metadata queried from staging. This document does not describe or change the separate, unmerged genomics work. No patient values are included.

For the active field-and-answer mapping work, see the
[enhancement plan](field_concept_mapping_enhancements.md), including its current
checkpoint and inventory prerequisite (#1223).

## How edits flow

1. Patient clinical edits are PATCHed to PatientRecord (`/api/v1/patient-records/{person_id}/`; the current provider UI uses its legacy `/api/patient-info/` alias). The UI name below is the payload key; a concrete field writes the same-named PatientRecord column unless an exception is stated.
2. The backend saves the validated value and tracks pending user edits. An approved FieldConceptMapping, or an existing built-in recipe, supplies a projection. Unmapped fields remain saved and protected on PatientRecord.
3. Scalar projection updates the non-erroneous OMOP row matching **person, concept, source value, and today's local date**, or creates that row. Earlier dates remain history. Numeric/boolean values use `value_as_number`; text/date values use `value_as_string` in this application's scalar writer. Mapping concepts populate `measurement_concept_id` or `observation_concept_id`; they are references to existing Concept rows, not new concepts created on each save. Units and type concepts come from the mapping recipe.
4. Successful scalar projection acknowledges the pending edit. Failed or unsupported projection retains the edit. An external OMOP refresh reads supported scalar mappings back; direct UI saves do not run a full OMOP refresh.
5. Mapping approval normally triggers backfill of pending user-edited values through the same projection service. It does not turn all historical derived values into user assertions.

The current generic writer dates a fact **today**. A date in PatientRecord is not automatically an event date override. The date pickers visible in some tabs do not establish a linked event-date contract by themselves. Explicitly listed companion dates require that contract.

A scalar mapping does not implement value-dependent diagnoses, drug selection, multi-row arrays, coded-answer conversions, or episode linkage. Those requirements remain explicit below. Negative/cleared answers must not create affirmative ConditionOccurrence, DrugExposure or ProcedureOccurrence rows.

Profile information also enters through the PatientRecord PATCH, with backend handling for Person/Location. Name is a virtual `patient_name` input targeting Person. Language and treatment-course dialogs use dedicated resources; their displayed summaries do not independently write PatientRecord.

## Mapping provenance and approval

`FieldConceptMapping.provenance` identifies who supplied the current recipe: **System Generated** or **Curator**. The accompanying UI change places it in the first column of the field-mapping list; that UI requires deployment. The database migration and initial approval set have been applied to staging. Manual creation/correction records Curator; automatic proposals record System Generated. Approval alone preserves recipe origin. The API cannot impersonate a system-generated recipe. Older rows without recorded origin display **Unrecorded**; their history is not guessed. Provenance is also retained during curation transfer.

The initial approval set below mirrors existing, validated projection recipes; existing approved/rejected/reviewed mappings were preserved. Suspect existing mappings are documented as REVIEW rather than silently endorsed. Ambiguous candidates, computed fields, aliases, companion units/dates and unsupported structures were excluded from initial approval.

## Reading the tab tables

Each row names the actual UI control and PatientRecord key, the current OMOP destination, the loaded concept, and any remaining gap. A destination shown beside a warning is a **current-state audit**, not an endorsement. Disease-specific sections are conditional on the selected disease. The numeric concept IDs are staging vocabulary identifiers; use vocabulary/code when resolving on another environment, especially for local concepts.


## General

Source: `frontend/src/components/PatientInfo/tabs/GeneralTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Date of Birth | `date_of_birth` | person | No clinical concept required | PatientRecord PATCH; backend updates year_of_birth / month_of_birth / day_of_birth / birth_datetime. Role restrictions still apply. |
| Gender | `gender` | person | Resolved by profile vocabulary | PatientRecord PATCH; backend updates gender_concept + gender_source_value. Role restrictions still apply. |
| Email | `email` | person | No clinical concept required | PatientRecord PATCH; backend updates email. Role restrictions still apply. |
| Phone Number | `phone_number` | person | No clinical concept required | PatientRecord PATCH; backend updates phone_number. Role restrictions still apply. |
| Treating Institution | `facility_name` | person | No clinical concept required | PatientRecord PATCH; backend updates facility_name. Role restrictions still apply. |
| Country | `country` | location | No clinical concept required | PatientRecord PATCH; backend updates Location.country. Role restrictions still apply. |
| City | `city` | location | No clinical concept required | PatientRecord PATCH; backend updates Location.city. Role restrictions still apply. |
| Region/State | `region` | location | No clinical concept required | PatientRecord PATCH; backend updates Location.state. Role restrictions still apply. |
| Latitude | `latitude` | location | No clinical concept required | PatientRecord PATCH; backend updates Location.latitude. Role restrictions still apply. |
| Longitude | `longitude` | location | No clinical concept required | PatientRecord PATCH; backend updates Location.longitude. Role restrictions still apply. |
| Validated | `validated` | person | No clinical concept required | PatientRecord PATCH; backend updates validated. Role restrictions still apply. |
| Validated By | `validated_by` | person | No clinical concept required | PatientRecord PATCH; backend updates validated_by. Role restrictions still apply. |
| Validation Date | `validation_date` | person | No clinical concept required | PatientRecord PATCH; backend updates validation_date. Role restrictions still apply. |
| Race | `race` | person | Resolved by profile vocabulary | PatientRecord PATCH; backend updates race_concept + race_source_value. Role restrictions still apply. |
| Ethnicity | `ethnicity` | person | Resolved by profile vocabulary | PatientRecord PATCH; backend updates ethnicity_concept + ethnicity_source_value. Role restrictions still apply. |
| ECOG Performance Status | `ecog_performance_status` | measurement | 36305384; LOINC `89247-1` — ECOG Performance Status score | Current projection. Existing approved; historical origin unrecorded. source `89247-1`; type 32817; number; unit `{score}` |
| ECOG Assessment Date | `ecog_assessment_date` | Observation | 36305384; LOINC `89247-1` — ECOG Performance Status score | Companion date: measurement.measurement_date for ecog_performance_status (LOINC 89247-1; 36305384). Needs linked event-date handling; do not store a score concept with a date answer. |
| Karnofsky Performance Score | `karnofsky_performance_score` | measurement | 36303287; LOINC `89243-0` — Karnofsky Performance Status score | Current projection. Existing approved; historical origin unrecorded. source `89243-0`; type 32817; number; unit `{score}` |
| Pre-existing Conditions | `preexisting_conditions` | Observation | 4010833; SNOMED `102478008` — Pre-existing condition | Structured list: resolve each category to a condition_occurrence concept, with dated occurrence evidence. Do not comma-join diagnoses under one generic question. |
| Peripheral Neuropathy Grade | `peripheral_neuropathy_grade` | observation | 46235473; LOINC `75691-6` — Peripheral sensory neuropathy grade NCICTC | Current projection. Existing approved; historical origin unrecorded. source `75691-6`; type 32817; number |
| No Other Active Malignancies | `no_other_active_malignancies` | Contract required | No verified concept | Unresolved: explicit absence of other active malignancies; needs question and negative-answer concepts, not a generic malignant diagnosis. |
| No Active Infection | `no_active_infection_status` | observation (candidate) | 4232893; SNOMED `405009004` — Infection status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. Infection status question. True in this PatientRecord field means absence; coded negative-answer/polarity handling must be reviewed before approval. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| HIV Positive | `hiv_status` | observation | 439727; SNOMED `86406008` — Human immunodeficiency virus infection | Current projection. Existing approved; historical origin unrecorded. source `86406008`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| No HIV | `no_hiv_status` | observation (candidate) | 4149958; SNOMED `278977008` — HIV status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. HIV status question; True means absence, not HIV infection. Coded negative-answer/polarity handling required. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Hepatitis B Positive | `hepatitis_b_status` | observation | 4281232; SNOMED `66071002` — Type B viral hepatitis | Current projection. Existing approved; historical origin unrecorded. source `66071002`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| No Hepatitis B | `no_hepatitis_b_status` | observation (candidate) | 4150742; SNOMED `278969009` — Hepatitis B status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. Hepatitis B status question; True means absence. Coded negative-answer/polarity handling required. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Hepatitis C Positive | `hepatitis_c_status` | observation | 4150744; SNOMED `278973007` — Hepatitis C status | Current projection. Existing approved; historical origin unrecorded. source `278973007`; type 32817; string |
| No Hepatitis C | `no_hepatitis_c_status` | observation (candidate) | 4150744; SNOMED `278973007` — Hepatitis C status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. Hepatitis C status question; True means absence. Coded negative-answer/polarity handling required. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Weight (kg) | `weight` | measurement | 3025315; LOINC `29463-7` — Body weight | Current projection. Existing approved; historical origin unrecorded. source `29463-7`; type 32817; number; unit `kg` |
| Height (cm) | `height` | measurement | 3036277; LOINC `8302-2` — Body height | Current projection. Existing approved; historical origin unrecorded. source `8302-2`; type 32817; number; unit `cm` |
| BMI | `bmi` | — | — | Excluded: Computed from height, weight. |
| Systolic Blood Pressure (mmHg) | `systolic_blood_pressure` | measurement | 3004249; LOINC `8480-6` — Systolic blood pressure | Current projection. Existing approved; historical origin unrecorded. source `8480-6`; type 32817; number; unit `mm[Hg]` |
| Diastolic Blood Pressure (mmHg) | `diastolic_blood_pressure` | measurement | 3012888; LOINC `8462-4` — Diastolic blood pressure | Current projection. Existing approved; historical origin unrecorded. source `8462-4`; type 32817; number; unit `mm[Hg]` |
| Heart Rate (bpm) | `heartrate` | measurement | 3027018; LOINC `8867-4` — Heart rate | Current projection. Existing approved; historical origin unrecorded. source `8867-4`; type 32817; number; unit `/min` |
| Death Date | `death_date` | observation | 0 — no matching standard concept | Current projection. Existing approved; historical origin unrecorded. source `patient-record:death_date`; type 32817; date |
| Patient Name | `name` | Person.given_name / family_name | No concept required | UI sends patient_name via the PatientRecord PATCH; backend writes Person and renders the name. No PatientRecord.name column. |
| Age | `age` | — | — | Computed from Person date of birth; excluded from independent mapping. |
| Postal Code / Zip Code | `postal_code` | location | No clinical concept required | PatientRecord PATCH; backend updates Location.zip. Role restrictions still apply. |

## Disease

Source: `frontend/src/components/PatientInfo/tabs/DiseaseTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Lymph Node Status | `lymph_node_status` | measurement | 37020683; LOINC `92837-4` — Perineural invasion [Presence] in Cancer specimen | Current projection. Approved · System Generated. source `92837-4`; type 32856; string |
| Metastasis Status | `metastasis_status` | measurement | 3006575; LOINC `21907-1` — Distant metastases.clinical [Class] Cancer | Current projection. Approved · System Generated. source `21907-1`; type 32856; string |
| PD-L1 Combined Positive Score | `pd_l1_combined_positive_score` | measurement | 1094398; LOINC `LA34414-5` — PD-L1 CPS | Current projection. Existing approved; historical origin unrecorded. source `LA34414-5`; type 32817; number; unit `{score}`  REVIEW: stored table Measurement differs from concept domain Meas Value. |
| PD-L1 IC (%) | `pd_l1_ic_percentage` | measurement | 1092026; LOINC `105305-7` — Tumor area infiltrated with PD-L1 expressing immune cells/Total tumor area [Area Fraction] in Tissue by Immune stain | Current projection. Existing approved; historical origin unrecorded. source `105305-7`; type 32817; number; unit `%` |
| Histologic Type | `histologic_type` | measurement | 40762908; LOINC `59847-4` — Histology and Behavior ICD-O-3 Cancer | Current projection. Existing approved; historical origin unrecorded. source `59847-4`; type 32817; string |
| Menopausal Status | `menopausal_status` | observation | 4172857; SNOMED `276477006` — Menopause finding | Current projection. Existing approved; historical origin unrecorded. source `276477006`; type 32817; string |
| Tumor Stage | `tumor_stage` | measurement | 3008841; LOINC `21905-5` — Primary tumor.clinical [Class] Cancer | Current projection. Existing approved; historical origin unrecorded. source `21905-5`; type 32856; string  REVIEW: stored table Measurement,Observation differs from concept domain Measurement. |
| Nodes Stage | `nodes_stage` | measurement | 3007727; LOINC `21906-3` — Regional lymph nodes.clinical [Class] Cancer | Current projection. Existing approved; historical origin unrecorded. source `21906-3`; type 32856; string  REVIEW: stored table Measurement,Observation differs from concept domain Measurement. |
| Staging Modalities | `staging_modalities` | observation | 4219603; SNOMED `399390009` — TNM stage grouping | Current projection. Existing approved; historical origin unrecorded. source `399390009`; type 32817; string REVIEW: TNM stage grouping is a stage, not a modality. |
| Distant Metastasis Stage | `distant_metastasis_stage` | measurement | 3018082; LOINC `21901-4` — Distant metastases.pathology [Class] Cancer | Current projection. Existing approved; historical origin unrecorded. source `21901-4`; type 32856; string  REVIEW: stored table Measurement,Observation differs from concept domain Observation. |
| Bone-Only Metastasis | `bone_only_metastasis_status` | Measurement | 36309629; LOINC `LA4202-3` — Distant recurrence of an invasive tumor in bone only | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| Measurable Disease by RECIST | `measurable_disease_by_recist_status` | observation | 2100000005; SNOMED `711259004` — Measurable disease by RECIST | Current projection. Existing approved; historical origin unrecorded. source `711259004`; type 32817; boolean  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Estrogen Receptor (ER) Status | `estrogen_receptor_status` | measurement | 3004390; LOINC `16112-5` — Estrogen receptor [Interpretation] in Tissue | Current projection. Existing approved; historical origin unrecorded. source `16112-5`; type 32856; string |
| Progesterone Receptor (PR) Status | `progesterone_receptor_status` | measurement | 3003289; LOINC `16113-3` — Progesterone receptor [Interpretation] in Tissue | Current projection. Existing approved; historical origin unrecorded. source `16113-3`; type 32856; string |
| HER2 Status | `her2_status` | measurement | 3048223; LOINC `48676-1` — HER2 [Interpretation] in Tissue | Current projection. Existing approved; historical origin unrecorded. source `48676-1`; type 32856; string |
| HR Status | `hr_status` | observation | 44791967; SNOMED `310871000000100` — Tumour hormone receptor status | Current projection. Existing approved; historical origin unrecorded. source `310871000000100`; type 32817; string |
| HRD Status | `hrd_status` | observation | 1469934; LOINC `107286-7` — Homologous recombination deficiency status analysis [Presence] in Tissue by Molecular genetics method | Current projection. Existing approved; historical origin unrecorded. source `107286-7`; type 32817; string  REVIEW: stored table Observation differs from concept domain Measurement. |
| Androgen Receptor Status | `androgen_receptor_status` | measurement | 3032783; LOINC `49457-5` — Androgen receptor Ag [Presence] in Tissue by Immune stain | Current projection. Existing approved; historical origin unrecorded. source `49457-5`; type 32817; string |
| Ki-67 Proliferation Index (%) | `ki67_proliferation_index` | measurement | 3018217; LOINC `29593-1` — Cells.Ki-67 nuclear Ag/cells in Tissue by Immune stain | Current projection. Existing approved; historical origin unrecorded. source `29593-1`; type 32817; number; unit `%` |
| PD-L1 Status (%) | `pd_l1_tumor_cells` | measurement | 1092047; LOINC `105304-0` — Tumor cells.Programmed cell death ligand 1/Viable tumor cells in Tissue by Immune stain | Current projection. Existing approved; historical origin unrecorded. source `105304-0`; type 32817; number; unit `%` |
| Oncotype DX Score | `oncotype_dx_score` | observation | 35933430; NAACCR `breast@2876@010` — Oncotype DX | Current projection. Existing approved; historical origin unrecorded. source `breast@2876@010`; type 32817; number REVIEW: confirm score versus assay and use the concept domain table. REVIEW: stored table Observation differs from concept domain Meas Value. |
| Test Methodology | `test_methodology` | measurement (candidate) | 42527891; LOINC `85069-3` — Lab test method [Type] | WITHHELD from approval: PatientRecord-first proposal; UI tab Disease; issues #1069–#1079. Lab test method question; preserve NGS/IHC/FISH/PCR answer and link to the corresponding test event. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Test Date | `test_date` | Observation | 3045429; LOINC `33882-2` — Collection date of Specimen | Companion date: link the relevant biomarker/genomics test event before writing measurement_date; a generic PatientRecord date does not identify which result. |
| Test Specimen Type | `test_specimen_type` | measurement | 3015746; LOINC `31208-2` — Specimen source identified | Current projection. Approved · System Generated. source `31208-2`; type 32856; string |
| Report Interpretation | `report_interpretation` | measurement | 40772043; LOINC `69548-6` — Genetic variant assessment | Current projection. Approved · System Generated. source `69548-6`; type 32856; string |
| Ann Arbor Stage | `stage` | measurement | 3022698; LOINC `21908-9` — Stage group.clinical Cancer | Current projection. Existing approved; historical origin unrecorded. source `21908-9`; type 32856; string  REVIEW: stored table Measurement,Observation differs from concept domain Measurement. |
| Tumor Grade | `tumor_grade` | measurement | 4160340; SNOMED `371469007` — Histologic grade of neoplasm | Current projection. Existing approved; historical origin unrecorded. source `371469007`; type 32817; number  REVIEW: stored table Measurement differs from concept domain Observation. |
| GELF Criteria | `gelf_criteria_status` | observation | 2100000004; SNOMED `109964006` — GELF criteria | Current projection. Existing approved; historical origin unrecorded. source `109964006`; type 32817; string  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| FLIPI Score | `flipi_score` | observation | 35917496; NAACCR `lymphoma@2910` — Follicular Lymphoma Prognostic Index (FLIPI) | Current projection. Existing approved; historical origin unrecorded. source `lymphoma@2910`; type 32817; number  REVIEW: stored table Observation differs from concept domain Measurement. |
| FLIPI Risk Category | `flipi_risk_category` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| FLIPI Risk Factors | `flipi_score_options` | Observation | 2100000003; SNOMED `444723004` — FLIPI score | Textual FLIPI categories: map each answer to the appropriate FLIPI Meas Value or Cancer Modifier; do not store option text as a numeric score. |
| Bulky Disease | `bulky_disease` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| B Symptoms | `b_symptoms` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Transformed to DLBCL | `transformed_to_dlbcl` | observation | 432574; SNOMED `109969005` — Diffuse large B-cell lymphoma | Current projection. Existing approved; historical origin unrecorded. source `109969005`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| Transformation Date | `dlbcl_transformation_date` | Observation | 2100000008; SNOMED `91860004` — Richter syndrome | Companion date: condition_occurrence.condition_start_date of the transformation event; needs an explicit link to that event. |
| Post-Transformation Outcome | `post_transformation_outcome` | Observation | 2100000008; SNOMED `91860004` — Richter syndrome | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| Bone Marrow Involvement | `bone_marrow_involvement` | observation | 2100000001; SNOMED `24940005` — Infiltration of bone marrow | Current projection. Existing approved; historical origin unrecorded. source `24940005`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Clonal Bone Marrow B Lymphocytes (%) | `clonal_bone_marrow_b_lymphocytes` | measurement | 3031240; LOINC `42759-1` — B lymphocytes [#/volume] in Bone marrow | Unresolved: field is clonal B-cell percentage in marrow. Existing 42759-1 is total B-cell count per volume, not clonal percentage; do not approve it. |
| Number of Nodal Sites | `number_of_nodal_sites` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| M-Protein Type | `myeloma_type` | observation | 4190641; SNOMED `415109007` — Plasma cell myeloma - category | Current projection. Existing approved; historical origin unrecorded. source `415109007`; type 32817; string |
| R-ISS Stage | `r_iss_stage` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Durie-Salmon Stage | `durie_salmon_stage` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Progression Status | `progression` | observation | 4077864; SNOMED `246450006` — Progression | Current projection. Existing approved; historical origin unrecorded. source `246450006`; type 32817; string |
| Measurable Disease (IMWG) | `measurable_disease_imwg` | Measurement | 2100000005; SNOMED `711259004` — Measurable disease by RECIST | Unresolved: IMWG measurable-disease assessment; RECIST is a different instrument and must not substitute. |
| MRD Status | `mrd_status` | observation | 2100000013; LOINC `98847-0` — Measurable residual disease panel | Current projection. Existing approved; historical origin unrecorded. source `98847-0`; type 32817; string  REVIEW: stored table Observation differs from concept domain Measurement. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Meets CRAB Criteria | `meets_crab` | Contract required | No verified concept | Unresolved: IMWG CRAB assessment with explicit component/result semantics; no suitable loaded concept verified. |
| Meets SLiM Criteria | `meets_slim` | Contract required | No verified concept | Unresolved: IMWG SLiM assessment with explicit component/result semantics; no suitable loaded concept verified. |
| Prior SCT Type | `stem_cell_transplant_history` | observation | 32817; Type Concept `OMOP4976890` — EHR | Current projection. Existing approved; historical origin unrecorded. source `mm-sct-history`; type 32817; string REVIEW: EHR (32817) belongs in the type column, not observation_concept_id. REVIEW: stored table observation differs from concept domain Type Concept. Structured field: scalar mapping alone does not prove lossless list projection/readback. |
| SCT Date | `sct_date` | observation | 32817; Type Concept `OMOP4976890` — EHR | Companion date: procedure_occurrence.procedure_date of the selected transplant. Existing EHR type-as-question mapping requires review.  REVIEW: stored table observation differs from concept domain Type Concept. |
| SCT Eligibility | `sct_eligibility` | observation | 32817; Type Concept `OMOP4976890` — EHR | Current projection. Existing approved; historical origin unrecorded. source `mm-sct-eligibility`; type 32817; string REVIEW: EHR (32817) is a provenance type, not an eligibility question. REVIEW: stored table observation differs from concept domain Type Concept. Structured field: scalar mapping alone does not prove lossless list projection/readback. |
| Serum M-Protein (g/dL) | `monoclonal_protein_serum` | measurement | 3046299; LOINC `33358-3` — Protein.monoclonal [Mass/volume] in Serum or Plasma by Electrophoresis | Current projection. Existing approved; historical origin unrecorded. source `33358-3`; type 32817; number; unit `g/dL` |
| Urine M-Protein (mg/24h) | `monoclonal_protein_urine` | measurement | 3034655; LOINC `42482-0` — Protein.monoclonal [Mass/time] in 24 hour Urine by Electrophoresis | Current projection. Existing approved; historical origin unrecorded. source `42482-0`; type 32817; number; unit `mg/24h` |
| Kappa Free Light Chains | `kappa_flc` | measurement | 1091621; LOINC `104544-2` — Kappa light chains.free [Mass/volume] in Serum or Plasma | Current projection. Existing approved; historical origin unrecorded. source `104544-2`; type 32817; number; unit `mg/L` |
| Lambda Free Light Chains | `lambda_flc` | measurement | 3047169; LOINC `33944-0` — Lambda light chains.free [Mass/volume] in Serum or Plasma | Current projection. Existing approved; historical origin unrecorded. source `33944-0`; type 32817; number; unit `mg/L` |
| Kappa/Lambda Ratio | `kappa_lambda_ratio` | measurement | 1091281; LOINC `104546-7` — Kappa light chains.free/Lambda light chains.free [Mass Ratio] in Serum or Plasma | Current projection. Existing approved; historical origin unrecorded. source `104546-7`; type 32817; number; unit `{ratio}` |
| Involved/Uninvolved Ratio | `involved_uninvolved_ratio` | — | — | Excluded: Computed from kappa_flc, lambda_flc. |
| Bone Lesions | `bone_lesions` | Measurement,Observation | 2100000012; LOINC `24646-7` — Bone lesion XR study | Structured list: per-lesion condition_occurrence/measurement facts with site and event linkage; generic XR procedure is not the lesion result. |
| Hypercalcemia | `hypercalcemia` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Renal Impairment | `renal_impairment` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Anemia | `anemia` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Bone Marrow Plasma Cells (%) | `clonal_plasma_cells` | measurement | 37019751; LOINC `93021-4` — Plasma cells with abnormal marker pattern/Cells counted in Bone marrow by Flow cytometry (FC) | Current projection. Existing approved; historical origin unrecorded. source `93021-4`; type 32817; number; unit `%` |
| Cytogenetic Risk | `cytogenetic_risk` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Cytogenetic Abnormalities | `cytogenetic_abnormalities` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Genetic Mutations | `genetic_mutations` | Observation | 40758361; LOINC `55232-3` — Genetic analysis summary panel | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| Binet Stage | `binet_stage` | observation | 607090; SNOMED `1149099005` — Binet staging classification for chronic lymphocytic leukemia | Current projection. Existing approved; historical origin unrecorded. source `1149099005`; type 32817; string  REVIEW: stored table Observation differs from concept domain Measurement. |
| Tumor Burden | `tumor_burden` | observation | 2100000011; SNOMED `246923001` — Tumor burden | Current projection. Existing approved; historical origin unrecorded. source `246923001`; type 32817; string  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Disease Activity | `disease_activity` | observation | 2100000002; SNOMED `246456005` — Activity of disease | Current projection. Existing approved; historical origin unrecorded. source `246456005`; type 32817; string  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Richter Transformation | `richter_transformation` | observation | 4173956; SNOMED `277550009` — Richter's syndrome | Current projection. Existing approved; historical origin unrecorded. source `277550009`; type 32817; string  REVIEW: stored table Observation differs from concept domain Condition. |
| Protein Expressions | `protein_expressions` | observation | 4155622; SNOMED `371511004` — Tumor immunophenotyping status | Current projection. Existing approved; historical origin unrecorded. source `371511004`; type 32817; string |
| Absolute Lymphocyte Count (×10⁹/L) | `absolute_lymphocyte_count` | measurement | 3019198; LOINC `26474-7` — Lymphocytes [#/volume] in Blood | Current projection. Existing approved; historical origin unrecorded. source `26474-7`; type 32817; number; unit `percent` |
| Lymphocyte Doubling Time (months) | `lymphocyte_doubling_time` | Observation | 3019198; LOINC `26474-7` — Lymphocytes [#/volume] in Blood | Unresolved: doubling interval, including unit and observation window; a lymphocyte count concept is not an interval. |
| Serum Beta-2 Microglobulin (mg/L) | `serum_beta2_microglobulin_level` | measurement (candidate) | 3013201; LOINC `1952-1` — Beta-2-Microglobulin [Mass/volume] in Serum or Plasma | WITHHELD from approval: PatientRecord-first proposal; UI tab Disease; issues #1069–#1079. Beta-2 microglobulin, not beta-2 globulin electrophoresis. Canonical beta2_microglobulin owns 1952-1; keep this source distinct or implement alias routing. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Clonal B-Lymphocyte Count | `clonal_b_lymphocyte_count` | Observation | 2100000032; LOINC `30374-0` — B cells/100 leukocytes in Blood | Unresolved: requires clonal B-cell count per volume, not B cells per 100 leukocytes. |
| QTcF Value (ms) | `qtcf_value` | measurement | 2100000015; LOINC `8632-1` — QTcF interval | Current projection. Existing approved; historical origin unrecorded. source `8632-1`; type 32817; number; unit `ms`  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Largest Lymph Node Size (cm) | `largest_lymph_node_size` | Contract required | 3018102; LOINC `21889-1` — Size Tumor | Expected measurement concept 36769292 (Cancer Modifier: Dimension of Largest Lymph Node) is absent from staging. Load/verify this concept before replacing the generic Size Tumor proposal. |
| Spleen Size (cm) | `spleen_size` | Observation | 200527; SNOMED `16294009` — Splenomegaly | Unresolved: quantitative spleen dimension in cm, not the Condition concept Splenomegaly. |
| TP53 Disruption | `tp53_disruption` | observation | 2100000010; SNOMED `405835008` — TP53 gene mutation | Current projection. Existing approved; historical origin unrecorded. source `405835008`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Measurement. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Measurable Disease (IWCLL) | `measurable_disease_iwcll` | Observation | 2100000005; SNOMED `711259004` — Measurable disease by RECIST | Unresolved: iwCLL measurable-disease assessment; RECIST is a different instrument and must not substitute. |
| Splenomegaly | `splenomegaly` | observation | 200527; SNOMED `16294009` — Splenomegaly | Current projection. Existing approved; historical origin unrecorded. source `16294009`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| Hepatomegaly | `hepatomegaly` | observation | 197676; SNOMED `80515008` — Large liver | Current projection. Existing approved; historical origin unrecorded. source `80515008`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| Lymphadenopathy | `lymphadenopathy` | observation | 315085; SNOMED `30746006` — Lymphadenopathy | Current projection. Existing approved; historical origin unrecorded. source `30746006`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| Autoimmune Cytopenias Refractory to Steroids | `autoimmune_cytopenias_refractory_to_steroids` | observation | 2100000028; SNOMED `439478003` — Autoimmune cytopenia | Current projection. Existing approved; historical origin unrecorded. source `439478003`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| BTK Inhibitor Refractory | `btk_inhibitor_refractory` | observation | 2100007852; HK-Observation `hko:btk-inhibitor-refractory` — BTK inhibitor refractory disease status | Current projection. Existing approved; historical origin unrecorded. source `hko:btk-inhibitor-refractory`; type 32817; boolean  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| BCL-2 Inhibitor Refractory | `bcl2_inhibitor_refractory` | observation | 2100007851; HK-Observation `hko:bcl2-inhibitor-refractory` — BCL-2 inhibitor refractory disease status | Current projection. Existing approved; historical origin unrecorded. source `hko:bcl2-inhibitor-refractory`; type 32817; boolean  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Molecular Markers | `molecular_markers` | observation | 4025367; SNOMED `106221001` — Genetic finding | Current projection. Existing approved; historical origin unrecorded. source `106221001`; type 32817; string  REVIEW: stored table Observation differs from concept domain Condition. |
| Disease | `disease` | ConditionOccurrence | 3027027; LOINC `29308-4` — Diagnosis | Value-dependent condition_occurrence.condition_concept_id: resolve the selected Disease code to its approved standard disease concept. A fixed generic Diagnosis concept is not a disease. Requires occurrence writer and negative/clear semantics.  REVIEW: stored table ConditionOccurrence differs from concept domain Observation. |

## Treatment

Source: `frontend/src/components/PatientInfo/tabs/TreatmentTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Number of Prior Lines | `therapy_lines_count` | — | — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| Relapse Count | `relapse_count` | observation | 0 — no matching standard concept | Current projection. Existing proposed; historical origin unrecorded. source `patient-record:relapse_count`; type 32817; number |
| Refractory Status | `refractory_status` | — | — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| Planned Therapies | `planned_therapies` | Observation | 4254051; SNOMED `410942007` — Drug or medicament | Unresolved: planned regimen assertion with regimen concept as answer. It must not create drug_exposure rows claiming administration; generic Drug or medicament is not an observation question. |

Therapy-line and supportive-course dialogs save the selected regimen, components, start/end dates, intent, outcome and discontinuation reason through their dedicated endpoints. Episode carries the treatment grouping and dates; EpisodeEvent links the relevant DrugExposure/ProcedureOccurrence evidence. The treatment-regimen episode recipe uses concept **32531** and the drug-exposure event-field concept **1147094**; actual drug/regimen concepts depend on the selected catalog entry. `first_line_*`, `second_line_*`, `later_*`, line counts and release/provenance summaries are derived. They are excluded from scalar PatientRecord mappings. Planned therapy remains a plan and must not assert administered medication.


## Blood

Source: `frontend/src/components/PatientInfo/tabs/BloodTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Hemoglobin (g/dL) | `hemoglobin_g_dl` | measurement | 3000963; LOINC `718-7` — Hemoglobin [Mass/volume] in Blood | Current projection. Approved · System Generated. source `718-7`; type 32865; number; unit `g/dL` |
| Hematocrit (%) | `hematocrit_percent` | measurement | 3009542; LOINC `20570-8` — Hematocrit [Volume Fraction] of Blood by calculation | Current projection. Approved · System Generated. source `20570-8`; type 32865; number; unit `%` |
| WBC Count (10³/µL) | `wbc_count_thousand_per_ul` | measurement | 3000905; LOINC `6690-2` — Leukocytes [#/volume] in Blood by Automated count | Current projection. Approved · System Generated. source `6690-2`; type 32865; number; unit `10*3/uL` |
| RBC Count (10⁶/µL) | `rbc_million_per_ul` | measurement | 3020416; LOINC `789-8` — Erythrocytes [#/volume] in Blood by Automated count | Current projection. Approved · System Generated. source `789-8`; type 32865; number; unit `10*6/uL` |
| Platelet Count (10³/µL) | `platelet_count_thousand_per_ul` | measurement | 3024929; LOINC `777-3` — Platelets [#/volume] in Blood by Automated count | Current projection. Approved · System Generated. source `777-3`; type 32865; number; unit `10*3/uL` |
| ANC (10³/µL) | `anc_thousand_per_ul` | measurement | 3013650; LOINC `751-8` — Neutrophils [#/volume] in Blood by Automated count | Current projection. Approved · System Generated. source `751-8`; type 32865; number; unit `10*3/uL` |
| ALC (10³/µL) | `alc_thousand_per_ul` | measurement | 3004327; LOINC `731-0` — Lymphocytes [#/volume] in Blood by Automated count | Current projection. Approved · System Generated. source `731-0`; type 32865; number; unit `10*3/uL` |
| AMC (10³/µL) | `amc_thousand_per_ul` | measurement | 3033575; LOINC `742-7` — Monocytes [#/volume] in Blood by Automated count | Current projection. Approved · System Generated. source `742-7`; type 32865; number; unit `10*3/uL` |

## Labs

Source: `frontend/src/components/PatientInfo/tabs/LabsTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Serum Creatinine (mg/dL) | `serum_creatinine_mg_dl` | measurement | 3016723; LOINC `2160-0` — Creatinine [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2160-0`; type 32865; number; unit `mg/dL` |
| Creatinine Clearance (mL/min) | `creatinine_clearance_ml_min` | measurement (candidate) | 3005770; LOINC `2164-2` — Creatinine renal clearance in 24 hour Urine and Serum or Plasma | WITHHELD from approval: PatientRecord-first proposal; UI tab Labs; issues #1069–#1079. Candidate only for measured 24-hour urine/serum clearance; confirm specimen and method. Estimated Cockcroft-Gault clearance needs a different concept. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Blood Urea Nitrogen (mg/dL) | `bun_mg_dl` | measurement | 3013682; LOINC `3094-0` — Urea nitrogen [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `3094-0`; type 32865; number; unit `mg/dL` |
| eGFR (mL/min/1.73m²) | `egfr_ml_min_173m2` | measurement | 40764999; LOINC `62238-1` — Glomerular filtration rate [Volume Rate/Area] in Serum, Plasma or Blood by Creatinine-based formula (CKD-EPI)/1.73 sq M | Current projection. Approved · System Generated. source `62238-1`; type 32865; number; unit `mL/min/1.73m2` |
| Sodium (mEq/L) | `sodium_meq_l` | measurement | 3019550; LOINC `2951-2` — Sodium [Moles/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2951-2`; type 32865; number; unit `mEq/L` |
| Potassium (mEq/L) | `potassium_meq_l` | measurement | 3023103; LOINC `2823-3` — Potassium [Moles/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2823-3`; type 32865; number; unit `mEq/L` |
| Serum Calcium (mg/dL) | `serum_calcium_mg_dl` | measurement | 3006906; LOINC `17861-6` — Calcium [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `17861-6`; type 32865; number; unit `mg/dL` |
| Magnesium (mg/dL) | `magnesium_mg_dl` | measurement | 3012095; LOINC `2601-3` — Magnesium [Moles/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2601-3`; type 32865; number; unit `mg/dL` |
| Phosphorus (mg/dL) | `phosphorus` | measurement | 3011904; LOINC `2777-1` — Phosphate [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2777-1`; type 32865; number; unit `mg/dL` |
| Albumin (g/dL) | `albumin_g_dl` | measurement | 3024561; LOINC `1751-7` — Albumin [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1751-7`; type 32865; number; unit `g/dL` |
| Total Protein (g/dL) | `total_protein` | measurement | 3020630; LOINC `2885-2` — Protein [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2885-2`; type 32865; number; unit `g/dL` |
| Glucose (mg/dL) | `glucose_mg_dl` | measurement | 3004501; LOINC `2345-7` — Glucose [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2345-7`; type 32865; number; unit `mg/dL` |
| AST (U/L) | `ast_u_l` | measurement | 3013721; LOINC `1920-8` — Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1920-8`; type 32865; number; unit `U/L` |
| ALT (U/L) | `alt_u_l` | measurement | 3006923; LOINC `1742-6` — Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1742-6`; type 32865; number; unit `U/L` |
| Alkaline Phosphatase (U/L) | `alkaline_phosphatase_u_l` | measurement | 3035995; LOINC `6768-6` — Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `6768-6`; type 32865; number; unit `U/L` |
| Total Bilirubin (mg/dL) | `bilirubin_total_mg_dl` | measurement | 3024128; LOINC `1975-2` — Bilirubin.total [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1975-2`; type 32865; number; unit `mg/dL` |
| Direct Bilirubin (mg/dL) | `serum_bilirubin_level_direct` | measurement | 3027597; LOINC `1968-7` — Bilirubin.direct [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1968-7`; type 32865; number; unit `mg/dL` |
| LDH (U/L) | `ldh_u_l` | measurement | 3016436; LOINC `2532-0` — Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2532-0`; type 32865; number; unit `U/L` |
| Beta-2 Microglobulin (mg/L) | `beta2_microglobulin` | measurement | 3013201; LOINC `1952-1` — Beta-2-Microglobulin [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1952-1`; type 32865; number; unit `mg/L` |
| C-Reactive Protein (mg/L) | `c_reactive_protein` | measurement | 3020460; LOINC `1988-5` — C reactive protein [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `1988-5`; type 32865; number; unit `mg/L` |
| ESR (mm/hr) | `esr` | measurement | 3015183; LOINC `30341-2` — Erythrocyte sedimentation rate [Velocity] in Red Blood Cells | Current projection. Approved · System Generated. source `30341-2`; type 32865; number; unit `mm/h` |
| HbA1c (%) | `hba1c_percent` | measurement | 3004410; LOINC `4548-4` — Hemoglobin A1c/Hemoglobin.total in Blood | Current projection. Approved · System Generated. source `4548-4`; type 32865; number; unit `%` |
| INR | `inr` | measurement | 3022217; LOINC `6301-6` — INR in Platelet poor plasma by Coagulation assay | Current projection. Approved · System Generated. source `6301-6`; type 32865; number; unit `{INR}` |
| Prothrombin Time (s) | `pt_seconds` | measurement | 3034426; LOINC `5902-2` — Prothrombin time (PT) | Current projection. Approved · System Generated. source `5902-2`; type 32865; number; unit `s` |
| aPTT (s) | `ptt_seconds` | measurement | 3013466; LOINC `3173-2` — aPTT in Blood by Coagulation assay | Current projection. Approved · System Generated. source `3173-2`; type 32865; number; unit `s` |
| CEA (ng/mL) | `cea_ng_ml` | measurement | 3003785; LOINC `2039-6` — Carcinoembryonic Ag [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2039-6`; type 32865; number; unit `ng/mL` |
| CA 19-9 (U/mL) | `ca19_9_u_ml` | measurement | 3000659; LOINC `25390-6` — Cytokeratin 19 [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `25390-6`; type 32865; number; unit `U/mL` |
| PSA (ng/mL) | `psa_ng_ml` | measurement | 3013603; LOINC `2857-1` — Prostate specific Ag [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `2857-1`; type 32865; number; unit `ng/mL` |
| Troponin (ng/mL) | `troponin_ng_ml` | measurement | 3021337; LOINC `10839-9` — Troponin I.cardiac [Mass/volume] in Serum or Plasma | Current projection. Approved · System Generated. source `10839-9`; type 32865; number; unit `ng/mL` |
| BNP (pg/mL) | `bnp_pg_ml` | measurement | 3031569; LOINC `42637-9` — Natriuretic peptide B [Mass/volume] in Blood | Current projection. Approved · System Generated. source `42637-9`; type 32865; number; unit `pg/mL` |
| Pulmonary Function Test Normal | `pulmonary_function_test_result` | observation (candidate) | 4023991; SNOMED `106053004` — Pulmonary function | WITHHELD from approval: PatientRecord-first proposal; UI tab Labs; issues #1069–#1079. Pulmonary function assessment; UI boolean means Normal. Review normal/abnormal answer encoding before approval; not a numeric lung volume. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| Bone Imaging Normal | `bone_imaging_result` | Observation | 2100000012; LOINC `24646-7` — Bone lesion XR study | Unresolved: boolean Normal result without modality/body site. A bone imaging procedure or XR study concept alone does not express this result. |

## Behavior

Source: `frontend/src/components/PatientInfo/tabs/BehaviorTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Smoking Status | `smoking_status` | measurement | 43054909; LOINC `72166-2` — Tobacco smoking status | Current projection. Existing approved; historical origin unrecorded. source `72166-2`; type 32856; string  REVIEW: stored table measurement differs from concept domain Observation. |
| Pack Years (if applicable) | `pack_years` | measurement | 40766364; LOINC `63640-7` — How many cigarettes per day do, or did, you smoke | Current projection. Existing approved; historical origin unrecorded. source `63640-7`; type 32856; number REVIEW: cigarettes/day does not measure pack-years. REVIEW: stored table measurement differs from concept domain Observation. |
| Alcohol Use | `alcohol_use` | measurement | 44786671; LOINC `74013-4` — Alcoholic drinks per day | Current projection. Existing approved; historical origin unrecorded. source `74013-4`; type 32856; string REVIEW: daily drink count does not encode the categorical alcohol-use answer. REVIEW: stored table measurement differs from concept domain Observation. |
| Drinks per Week (if applicable) | `drinks_per_week` | measurement | 2029606278; LOINC `11286-7` — Drinks per week | Current projection. Existing approved; historical origin unrecorded. source `11286-7`; type 32856; number  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| Exercise Frequency | `exercise_frequency` | measurement | 40771101; LOINC `68516-4` — On those days that you engage in moderate to strenuous exercise, how many minutes, on average, do you exercise | Current projection. Existing approved; historical origin unrecorded. source `68516-4`; type 32856; string REVIEW: current concept asks minutes per day, not frequency. REVIEW: stored table measurement differs from concept domain Observation. |
| Exercise Minutes per Week | `exercise_minutes_per_week` | measurement | 36305482; LOINC `89555-7` — How many days per week did you engage in moderate to strenuous physical activity in the last 30 days | Current projection. Existing approved; historical origin unrecorded. source `89555-7`; type 32856; number REVIEW: current concept asks days per week, not minutes. REVIEW: stored table measurement differs from concept domain Observation. |
| Diet Type | `diet_type` | measurement | 36303387; LOINC `88365-2` — Glucose [Mass/volume] in Blood --pre-meal | Current projection. Existing approved; historical origin unrecorded. source `88365-2`; type 32856; string REVIEW: pre-meal glucose is unrelated to diet type. |
| Average Sleep Hours per Night | `sleep_hours_per_night` | measurement | 1002368; LOINC `93832-4` — Sleep duration | Current projection. Existing approved; historical origin unrecorded. source `93832-4`; type 32856; number; unit `h`  REVIEW: stored table measurement differs from concept domain Observation. |
| Sleep Quality | `sleep_quality` | measurement | 1001932; LOINC `93831-6` — Deep sleep duration | Current projection. Existing approved; historical origin unrecorded. source `93831-6`; type 32856; string REVIEW: deep-sleep duration is not sleep quality. REVIEW: stored table measurement differs from concept domain Observation. |
| Stress Level | `stress_level` | measurement | 44786643; LOINC `73985-4` — Exercise activity | Current projection. Existing approved; historical origin unrecorded. source `73985-4`; type 32856; string REVIEW: exercise activity is not stress level. REVIEW: stored table measurement differs from concept domain Observation. |
| Social Support | `social_support` | measurement | 37020172; LOINC `93033-9` — Are you worried about losing your housing [PRAPARE] | Current projection. Existing approved; historical origin unrecorded. source `93033-9`; type 32856; string REVIEW: housing insecurity is not general social support. REVIEW: stored table measurement differs from concept domain Observation. |
| Employment Status | `employment_status` | observation | 4073163; SNOMED `224362002` — Employment status | Current projection. Existing approved; historical origin unrecorded. source `224362002`; type 32817; string |
| Education Level | `education_level` | measurement | 42528763; LOINC `82589-3` — Highest level of education | Current projection. Existing approved; historical origin unrecorded. source `82589-3`; type 32856; string  REVIEW: stored table measurement differs from concept domain Observation. |
| Marital Status | `marital_status` | measurement | 3046344; LOINC `45404-1` — Marital status | Current projection. Existing approved; historical origin unrecorded. source `45404-1`; type 32856; string  REVIEW: stored table measurement differs from concept domain Observation. |
| Insurance Type | `insurance_type` | observation | 4237193; SNOMED `408729009` — Finding context | Current projection. Existing proposed; historical origin unrecorded. source `408729009`; type 32856; string |
| Number of Dependents | `number_of_dependents` | measurement | 40766239; LOINC `63512-8` — How many people are living or staying at this address [#] | Current projection. Existing approved; historical origin unrecorded. source `63512-8`; type 32856; number REVIEW: household size is not number of dependents. REVIEW: stored table measurement differs from concept domain Observation. |
| Annual Household Income (USD) | `annual_household_income` | measurement | 1872307076; LOINC `77243-3` — Annual household income | Current projection. Existing approved; historical origin unrecorded. source `77243-3`; type 32856; number |
| Pregnancy Test Date | `pregnancy_test_date` | Observation | 3018954; LOINC `2106-3` — Choriogonadotropin [Presence] in Urine | Companion date: date of pregnancy_test_result_value result; select and update that same event, not a second result with a date as its answer. |
| Pregnancy Test Result | `pregnancy_test_result_value` | observation | 3018954; LOINC `2106-3` — Choriogonadotropin [Presence] in Urine | Current projection. Existing approved; historical origin unrecorded. source `2106-3`; type 32817; string  REVIEW: stored table observation differs from concept domain Measurement. |
| Using Contraceptives | `contraceptive_use` | observation | 4027509; SNOMED `13197004` — Uses contraception | Current projection. Existing approved; historical origin unrecorded. source `13197004`; type 32817; boolean |
| Ability to Consent | `consent_capability` | Observation | 4226675; SNOMED `405193005` — Caregiver wellbeing status | Unresolved: capacity to consent, not consent given, caregiver wellbeing, or a jurisdiction-specific legal certificate. |
| Availability of Caregiver | `caregiver_availability_status` | observation | 44786672; LOINC `74014-2` — Last drank alcohol [Date and time] | Current projection. Existing approved; historical origin unrecorded. source `74014-2`; type 32817; boolean REVIEW: last alcohol use is unrelated to caregiver availability. |
| Mental Health Disorders | `no_mental_health_disorder_status` | Contract required | No verified concept | Unresolved polarity: UI label says Mental Health Disorders but stored field starts no_. Confirm the boolean meaning and select a status question/answer mapping. |
| Non-prescription Recreational Drug Use | `no_substance_use_status` | Contract required | No verified concept | Unresolved polarity: UI label says Non-prescription Recreational Drug Use but stored field starts no_. Confirm meaning before approving a coded answer. |
| Substance Use Details | `substance_use_details` | Observation | 4279309; SNOMED `66214007` — Substance abuse | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| Geographic/Occupational/Environmental/Infectious Disease Exposure Risk | `no_geographic_exposure_risk` | observation | 42529548; LOINC `82593-5` — Immunization summary report | Current projection. Existing approved; historical origin unrecorded. source `82593-5`; type 32817; boolean REVIEW: immunization report is unrelated to exposure risk. REVIEW: stored table observation differs from concept domain Note. |
| Exposure Risk Details | `geographic_exposure_risk_details` | Observation | 4168974; SNOMED `420008001` — Travel | Unresolved: geography, occupation, environment and infection exposure narrative; Travel alone does not cover the field. |
| English — speak | `english_speak` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| English — read | `english_read` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| English — write | `english_write` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| English — understand | `english_understand` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| Spanish — speak | `spanish_speak` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| Spanish — read | `spanish_read` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| Spanish — write | `spanish_write` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| Spanish — understand | `spanish_understand` | PersonLanguageSkill (extension) | Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |

Language capability controls write language-skill rows and refresh the eight flattened flags. No independent mappings were added to those flags.


## Wearable

Source: `frontend/src/components/PatientInfo/tabs/WearableTab.tsx`.

| UI field | PatientRecord key | OMOP table / target | Concept | Flow, approval, or exclusion |
|---|---|---|---|---|
| Median Daily Steps | `median_daily_steps_30d` | — | — | Excluded: A 30-day aggregate of steps readings. Upload device data rather than entering a summary value. |
| Active Minutes / Day | `active_minutes_per_day_30d` | — | — | Excluded: A 30-day aggregate of active_minutes readings. Upload device data rather than entering a summary value. |
| Activity Trend | `activity_trend_30d` | — | — | Excluded: A 30-day aggregate of steps readings. Upload device data rather than entering a summary value. |
| Resting Heart Rate (bpm) | `resting_heart_rate_avg_30d` | — | — | Excluded: A 30-day aggregate of resting_hr readings. Upload device data rather than entering a summary value. |
| HRV SDNN (ms) | `hrv_sdnn_avg_30d` | — | — | Excluded: A 30-day aggregate of hrv_sdnn readings. Upload device data rather than entering a summary value. |
| HRV RMSSD (ms) | `hrv_rmssd_avg_30d` | — | — | Excluded: A 30-day aggregate of hrv_rmssd readings. Upload device data rather than entering a summary value. |
| Min SpO&#8322; (%) | `oxygen_saturation_min_30d` | — | — | Excluded: A 30-day aggregate of spo2 readings. Upload device data rather than entering a summary value. |
| Avg SpO&#8322; (%) | `oxygen_saturation_avg_30d` | — | — | Excluded: A 30-day aggregate of spo2 readings. Upload device data rather than entering a summary value. |
| Respiratory Rate (breaths/min) | `respiratory_rate_avg_30d` | — | — | Excluded: A 30-day aggregate of respiratory_rate readings. Upload device data rather than entering a summary value. |
| Avg Sleep Duration (hours) | `sleep_duration_hours_avg_30d` | — | — | Excluded: A 30-day aggregate of sleep_duration readings. Upload device data rather than entering a summary value. |
| VO&#8322; Max (mL/kg/min) | `vo2_max_avg_30d` | — | — | Excluded: A 30-day aggregate of vo2_max readings. Upload device data rather than entering a summary value. |
| Distance (km/day) | `distance_km_per_day_30d` | — | — | Excluded: A 30-day aggregate of distance readings. Upload device data rather than entering a summary value. |
| Flights Climbed / Day | `flights_climbed_per_day_30d` | — | — | Excluded: A 30-day aggregate of flights_climbed readings. Upload device data rather than entering a summary value. |
| Walking Speed (km/hr) | `walking_speed_avg_30d` | — | — | Excluded: A 30-day aggregate of walking_speed readings. Upload device data rather than entering a summary value. |
| Step Length (cm) | `walking_step_length_avg_30d` | — | — | Excluded: A 30-day aggregate of walking_step_length readings. Upload device data rather than entering a summary value. |
| Double Support (%) | `walking_double_support_pct_avg_30d` | — | — | Excluded: A 30-day aggregate of walking_double_support_pct readings. Upload device data rather than entering a summary value. |
| Walking Heart Rate (bpm) | `walking_hr_avg_30d` | — | — | Excluded: A 30-day aggregate of walking_hr_avg readings. Upload device data rather than entering a summary value. |
| Active Energy (kcal/day) | `active_energy_per_day_30d` | — | — | Excluded: A 30-day aggregate of active_energy readings. Upload device data rather than entering a summary value. |
| Basal Energy (kcal/day) | `basal_energy_per_day_30d` | — | — | Excluded: A 30-day aggregate of basal_energy readings. Upload device data rather than entering a summary value. |
| Body Mass (kg) | `body_mass_avg_30d` | — | — | Excluded: A 30-day aggregate of body_mass readings. Upload device data rather than entering a summary value. |
| 30-day coverage | `wearable_coverage_ratio_30d` | — | — | Excluded: Proportion of the 30-day window with any valid wearable reading, counting each day once across all device metrics. |
| Last sync | `wearable_last_sync_at` | — | — | Excluded: Bookkeeping about the device feed rather than a reading; follows from ingesting wearable data. |

The 30-day fields are summaries of device readings, not independently entered measurements. Upload writes underlying dated wearable facts, then derives the displayed aggregates. No scalar mappings were added for aggregates, coverage or sync metadata.


## Other tabs and dynamic fields

Allergies, surveys and the OMOP table browser are separate resources, not scalar PatientRecord editors. Custom editable fields are defined at runtime and store values under `PatientRecord.custom_fields[field_name]`; their approved CustomPatientField/FieldConceptMapping definitions specify the projection. Computed custom fields use formulas and are excluded from this initial mapping set. These are dynamic contracts, not invented fixed PatientRecord columns.

The unmerged Genomics tab and its new marker fields are intentionally outside this dev snapshot. Existing Disease-tab mutation/list controls are documented according to the current dev fields; no genomics code, migrations or mappings were changed by this work.

## Staging application receipt

Applied the provenance-only migration `0224_field_mapping_provenance` to staging; no genomics migrations ran. Updated and verified **41** existing, unreviewed proposals as **Approved / System Generated**. The full projection recipe (concept, table, source key, value kind, unit and type) matches the previously active built-in recipe for each field. Checked approved source-key conflicts and preserved previously reviewed mappings. The normal pending-edit backfill completed for all 41 fields and projected **0** records: no pending edits required backfill.

The provenance UI code is prepared separately and requires deployment before the first-column labels appear on staging. Eight additional concept candidates are documented but withheld from approval. Other unresolved meanings and incompatible existing approvals are listed in the tab tables and repurposed issues; this is not complete clinical mapping coverage.

The exact applied recipe metadata is also available in [patient_field_mapping_initial_approvals.json](docs/patient_field_mapping_initial_approvals.json).

## Initial system-approved mappings

The table records the exact recipes applied by this work. `source_value` distinguishes a fact; type is the OMOP provenance type, separate from the new mapping-origin column. Approval is not a clinical endorsement of every older staging mapping.

| PatientRecord field | OMOP table | Concept ID | Vocabulary:code | Source value | Value kind | Unit | Type concept |
|---|---|---|---|---|---|---|---|

| serum_creatinine_mg_dl | measurement | 3016723 | LOINC:2160-0 | 2160-0 | number | mg/dL | 32865 |
| bun_mg_dl | measurement | 3013682 | LOINC:3094-0 | 3094-0 | number | mg/dL | 32865 |
| egfr_ml_min_173m2 | measurement | 40764999 | LOINC:62238-1 | 62238-1 | number | mL/min/1.73m2 | 32865 |
| sodium_meq_l | measurement | 3019550 | LOINC:2951-2 | 2951-2 | number | mEq/L | 32865 |
| potassium_meq_l | measurement | 3023103 | LOINC:2823-3 | 2823-3 | number | mEq/L | 32865 |
| serum_calcium_mg_dl | measurement | 3006906 | LOINC:17861-6 | 17861-6 | number | mg/dL | 32865 |
| magnesium_mg_dl | measurement | 3012095 | LOINC:2601-3 | 2601-3 | number | mg/dL | 32865 |
| phosphorus | measurement | 3011904 | LOINC:2777-1 | 2777-1 | number | mg/dL | 32865 |
| albumin_g_dl | measurement | 3024561 | LOINC:1751-7 | 1751-7 | number | g/dL | 32865 |
| total_protein | measurement | 3020630 | LOINC:2885-2 | 2885-2 | number | g/dL | 32865 |
| glucose_mg_dl | measurement | 3004501 | LOINC:2345-7 | 2345-7 | number | mg/dL | 32865 |
| ast_u_l | measurement | 3013721 | LOINC:1920-8 | 1920-8 | number | U/L | 32865 |
| alt_u_l | measurement | 3006923 | LOINC:1742-6 | 1742-6 | number | U/L | 32865 |
| alkaline_phosphatase_u_l | measurement | 3035995 | LOINC:6768-6 | 6768-6 | number | U/L | 32865 |
| bilirubin_total_mg_dl | measurement | 3024128 | LOINC:1975-2 | 1975-2 | number | mg/dL | 32865 |
| serum_bilirubin_level_direct | measurement | 3027597 | LOINC:1968-7 | 1968-7 | number | mg/dL | 32865 |
| ldh_u_l | measurement | 3016436 | LOINC:2532-0 | 2532-0 | number | U/L | 32865 |
| beta2_microglobulin | measurement | 3013201 | LOINC:1952-1 | 1952-1 | number | mg/L | 32865 |
| c_reactive_protein | measurement | 3020460 | LOINC:1988-5 | 1988-5 | number | mg/L | 32865 |
| esr | measurement | 3015183 | LOINC:30341-2 | 30341-2 | number | mm/h | 32865 |
| hba1c_percent | measurement | 3004410 | LOINC:4548-4 | 4548-4 | number | % | 32865 |
| inr | measurement | 3022217 | LOINC:6301-6 | 6301-6 | number | {INR} | 32865 |
| pt_seconds | measurement | 3034426 | LOINC:5902-2 | 5902-2 | number | s | 32865 |
| ptt_seconds | measurement | 3013466 | LOINC:3173-2 | 3173-2 | number | s | 32865 |
| cea_ng_ml | measurement | 3003785 | LOINC:2039-6 | 2039-6 | number | ng/mL | 32865 |
| ca19_9_u_ml | measurement | 3000659 | LOINC:25390-6 | 25390-6 | number | U/mL | 32865 |
| psa_ng_ml | measurement | 3013603 | LOINC:2857-1 | 2857-1 | number | ng/mL | 32865 |
| troponin_ng_ml | measurement | 3021337 | LOINC:10839-9 | 10839-9 | number | ng/mL | 32865 |
| bnp_pg_ml | measurement | 3031569 | LOINC:42637-9 | 42637-9 | number | pg/mL | 32865 |
| hemoglobin_g_dl | measurement | 3000963 | LOINC:718-7 | 718-7 | number | g/dL | 32865 |
| hematocrit_percent | measurement | 3009542 | LOINC:20570-8 | 20570-8 | number | % | 32865 |
| wbc_count_thousand_per_ul | measurement | 3000905 | LOINC:6690-2 | 6690-2 | number | 10*3/uL | 32865 |
| rbc_million_per_ul | measurement | 3020416 | LOINC:789-8 | 789-8 | number | 10*6/uL | 32865 |
| platelet_count_thousand_per_ul | measurement | 3024929 | LOINC:777-3 | 777-3 | number | 10*3/uL | 32865 |
| anc_thousand_per_ul | measurement | 3013650 | LOINC:751-8 | 751-8 | number | 10*3/uL | 32865 |
| alc_thousand_per_ul | measurement | 3004327 | LOINC:731-0 | 731-0 | number | 10*3/uL | 32865 |
| amc_thousand_per_ul | measurement | 3033575 | LOINC:742-7 | 742-7 | number | 10*3/uL | 32865 |
| lymph_node_status | measurement | 37020683 | LOINC:92837-4 | 92837-4 | string |  | 32856 |
| metastasis_status | measurement | 3006575 | LOINC:21907-1 | 21907-1 | string |  | 32856 |
| test_specimen_type | measurement | 3015746 | LOINC:31208-2 | 31208-2 | string |  | 32856 |
| report_interpretation | measurement | 40772043 | LOINC:69548-6 | 69548-6 | string |  | 32856 |

## Issue coverage and remaining work

### #1069: Canonical aliases and shared OMOP facts

| Field | Destination / concept | Disposition |
|---|---|---|
| `absolute_neutrophile_count` | —; — | Excluded: Mirrors anc_thousand_per_ul; edit that field instead. Canonical: anc_thousand_per_ul |
| `alkaline_phosphatase` | —; — | Excluded: Mirrors alkaline_phosphatase_u_l; edit that field instead. Canonical: alkaline_phosphatase_u_l |
| `blood_urea_nitrogen` | —; — | Excluded: Mirrors bun_mg_dl; edit that field instead. Canonical: bun_mg_dl |
| `calcium_mg_dl` | —; — | Excluded: Mirrors serum_calcium_mg_dl; edit that field instead. Canonical: serum_calcium_mg_dl |
| `creatinine_clearance_rate` | —; — | Excluded: Mirrors creatinine_clearance_ml_min; edit that field instead. Canonical: creatinine_clearance_ml_min |
| `creatinine_mg_dl` | —; — | Excluded: Mirrors serum_creatinine_mg_dl; edit that field instead. Canonical: serum_creatinine_mg_dl |
| `egfr` | —; — | Excluded: Mirrors egfr_ml_min_173m2; edit that field instead. Canonical: egfr_ml_min_173m2 |
| `estimated_glomerular_filtration_rate` | —; — | Excluded: Mirrors egfr_ml_min_173m2; edit that field instead. Canonical: egfr_ml_min_173m2 |
| `lactate_dehydrogenase_level` | —; — | Excluded: Mirrors ldh_u_l; edit that field instead. Canonical: ldh_u_l |
| `ldh` | —; — | Excluded: Mirrors ldh_u_l; edit that field instead. Canonical: ldh_u_l |
| `ldh_level` | —; — | Excluded: Mirrors ldh_u_l; edit that field instead. Canonical: ldh_u_l |
| `liver_enzyme_levels_alp` | —; — | Excluded: Mirrors alkaline_phosphatase_u_l; edit that field instead. Canonical: alkaline_phosphatase_u_l |
| `liver_enzyme_levels_alt` | —; — | Excluded: Mirrors alt_u_l; edit that field instead. Canonical: alt_u_l |
| `liver_enzyme_levels_ast` | —; — | Excluded: Mirrors ast_u_l; edit that field instead. Canonical: ast_u_l |
| `magnesium` | —; — | Excluded: Mirrors magnesium_mg_dl; edit that field instead. Canonical: magnesium_mg_dl |
| `red_blood_cell_count` | —; — | Excluded: Mirrors rbc_million_per_ul; edit that field instead. Canonical: rbc_million_per_ul |
| `refractory_status` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `serum_bilirubin_level` | —; — | Excluded: Mirrors serum_bilirubin_level_direct; edit that field instead. Canonical: serum_bilirubin_level_direct |
| `serum_potassium` | —; — | Excluded: Mirrors potassium_meq_l; edit that field instead. Canonical: potassium_meq_l |
| `serum_sodium` | —; — | Excluded: Mirrors sodium_meq_l; edit that field instead. Canonical: sodium_meq_l |

### #1070: PatientRecord treatment assertions and OMOP projection

| Field | Destination / concept | Disposition |
|---|---|---|
| `last_treatment` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `line_of_therapy` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `planned_therapies` | Observation; 4254051; SNOMED `410942007` — Drug or medicament | Unresolved: planned regimen assertion with regimen concept as answer. It must not create drug_exposure rows claiming administration; generic Drug or medicament is not an observation question. |
| `prior_therapy` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `reason_for_discontinuation` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `relapse_count` | observation; 0 — no matching standard concept | Current projection. Existing proposed; historical origin unrecorded. source `patient-record:relapse_count`; type 32817; number |
| `supportive_therapies` | Observation; 4254051; SNOMED `410942007` — Drug or medicament | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `supportive_therapy_date` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `supportive_therapy_end_date` | Observation; 4208903; SNOMED `439771001` — Date of event | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `supportive_therapy_intent` | Observation; 4179699; SNOMED `363589002` — Associated procedure | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `supportive_therapy_start_date` | Observation; 4208903; SNOMED `439771001` — Date of event | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `therapy_component_ids` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `therapy_ids_provenance` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `therapy_intent` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `therapy_lines_count` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `therapy_type_ids` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |
| `treatment_refractory_status` | observation; 0 — no matching standard concept | Current projection. Existing proposed; historical origin unrecorded. source `patient-record:treatment_refractory_status`; type 32817; string |
| `washout_period_duration` | —; — | Excluded: Derived from the therapy episodes, not from one fact. Author a line as an Episode grouping its drug exposures and this field follows. |

### #1071: Computed fields excluded from scalar OMOP mappings

| Field | Destination / concept | Disposition |
|---|---|---|
| `active_energy_per_day_30d` | —; — | Excluded: A 30-day aggregate of active_energy readings. Upload device data rather than entering a summary value. |
| `active_minutes_per_day_30d` | —; — | Excluded: A 30-day aggregate of active_minutes readings. Upload device data rather than entering a summary value. |
| `activity_trend_30d` | —; — | Excluded: A 30-day aggregate of steps readings. Upload device data rather than entering a summary value. |
| `age` | —; — | Computed from Person date of birth; excluded from independent mapping. |
| `basal_energy_per_day_30d` | —; — | Excluded: A 30-day aggregate of basal_energy readings. Upload device data rather than entering a summary value. |
| `bmi` | —; — | Excluded: Computed from height, weight. |
| `body_mass_avg_30d` | —; — | Excluded: A 30-day aggregate of body_mass readings. Upload device data rather than entering a summary value. |
| `distance_km_per_day_30d` | —; — | Excluded: A 30-day aggregate of distance readings. Upload device data rather than entering a summary value. |
| `flights_climbed_per_day_30d` | —; — | Excluded: A 30-day aggregate of flights_climbed readings. Upload device data rather than entering a summary value. |
| `hrv_rmssd_avg_30d` | —; — | Excluded: A 30-day aggregate of hrv_rmssd readings. Upload device data rather than entering a summary value. |
| `hrv_sdnn_avg_30d` | —; — | Excluded: A 30-day aggregate of hrv_sdnn readings. Upload device data rather than entering a summary value. |
| `involved_uninvolved_ratio` | —; — | Excluded: Computed from kappa_flc, lambda_flc. |
| `liver_enzyme_levels` | —; — | Excluded: Computed from liver_enzyme_levels_ast, liver_enzyme_levels_alt, liver_enzyme_levels_alp. |
| `median_daily_steps_30d` | —; — | Excluded: A 30-day aggregate of steps readings. Upload device data rather than entering a summary value. |
| `molecular_markers` | observation; 4025367; SNOMED `106221001` — Genetic finding | Current projection. Existing approved; historical origin unrecorded. source `106221001`; type 32817; string  REVIEW: stored table Observation differs from concept domain Condition. |
| `name` | Person.given_name / family_name; No concept required | UI sends patient_name via the PatientRecord PATCH; backend writes Person and renders the name. No PatientRecord.name column. |
| `oxygen_saturation_avg_30d` | —; — | Excluded: A 30-day aggregate of spo2 readings. Upload device data rather than entering a summary value. |
| `oxygen_saturation_min_30d` | —; — | Excluded: A 30-day aggregate of spo2 readings. Upload device data rather than entering a summary value. |
| `person_id` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `respiratory_rate_avg_30d` | —; — | Excluded: A 30-day aggregate of respiratory_rate readings. Upload device data rather than entering a summary value. |
| `resting_heart_rate_avg_30d` | —; — | Excluded: A 30-day aggregate of resting_hr readings. Upload device data rather than entering a summary value. |
| `sleep_duration_hours_avg_30d` | —; — | Excluded: A 30-day aggregate of sleep_duration readings. Upload device data rather than entering a summary value. |
| `tnbc_status` | observation; 2100000009; SNOMED `706886006` — Triple-negative breast cancer | Current projection. Existing approved; historical origin unrecorded. source `706886006`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `tp53_disruption` | observation; 2100000010; SNOMED `405835008` — TP53 gene mutation | Current projection. Existing approved; historical origin unrecorded. source `405835008`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Measurement. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `vo2_max_avg_30d` | —; — | Excluded: A 30-day aggregate of vo2_max readings. Upload device data rather than entering a summary value. |
| `walking_double_support_pct_avg_30d` | —; — | Excluded: A 30-day aggregate of walking_double_support_pct readings. Upload device data rather than entering a summary value. |
| `walking_hr_avg_30d` | —; — | Excluded: A 30-day aggregate of walking_hr_avg readings. Upload device data rather than entering a summary value. |
| `walking_speed_avg_30d` | —; — | Excluded: A 30-day aggregate of walking_speed readings. Upload device data rather than entering a summary value. |
| `walking_step_length_avg_30d` | —; — | Excluded: A 30-day aggregate of walking_step_length readings. Upload device data rather than entering a summary value. |
| `wearable_coverage_ratio_30d` | —; — | Excluded: Proportion of the 30-day window with any valid wearable reading, counting each day once across all device metrics. |

### #1072: Therapy episode fields and OMOP event linkage

| Field | Destination / concept | Disposition |
|---|---|---|
| `first_line_component_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_discontinuation_reason` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_end_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_intent` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_outcome` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_start_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_therapy` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_therapy_display` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `first_line_therapy_id` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `first_line_therapy_type_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_component_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_discontinuation_reason` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_end_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_intent` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_outcome` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_start_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_therapies` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_therapy` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_therapy_display` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `later_therapy_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `later_therapy_type_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `lines_of_therapy` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `second_line_component_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_discontinuation_reason` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_end_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_intent` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_outcome` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_start_date` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_therapy` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_therapy_display` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `second_line_therapy_id` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `second_line_therapy_type_ids` | —; — | Excluded: Code-computed from persisted Episode and EpisodeEvent records. Author a therapy line as an Episode grouping its events and this field follows. |
| `therapy_release_id` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |

### #1073: Largest lymph-node measurement concept availability

| Field | Destination / concept | Disposition |
|---|---|---|
| `largest_lymph_node_size` | Contract required; 3018102; LOINC `21889-1` — Size Tumor | Expected measurement concept 36769292 (Cancer Modifier: Dimension of Largest Lymph Node) is absent from staging. Load/verify this concept before replacing the generic Size Tumor proposal. |

### #1074: Unit companions on the associated OMOP measurement

| Field | Destination / concept | Disposition |
|---|---|---|
| `absolute_neutrophile_count_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `albumin_level_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `height_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `hemoglobin_level_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `platelet_count_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `red_blood_cell_count_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `serum_bilirubin_level_direct_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `serum_bilirubin_level_total_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `serum_calcium_level_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `serum_creatinine_level_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `weight_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |
| `white_blood_cell_count_units` | measurement.unit_concept_id + unit_source_value; UCUM selected unit | Companion of the associated measurement; no independent FieldConceptMapping. |

### #1075: Behavior-field OMOP concepts and answer semantics

| Field | Destination / concept | Disposition |
|---|---|---|
| `alcohol_use` | measurement; 44786671; LOINC `74013-4` — Alcoholic drinks per day | Current projection. Existing approved; historical origin unrecorded. source `74013-4`; type 32856; string REVIEW: daily drink count does not encode the categorical alcohol-use answer. REVIEW: stored table measurement differs from concept domain Observation. |
| `annual_household_income` | measurement; 1872307076; LOINC `77243-3` — Annual household income | Current projection. Existing approved; historical origin unrecorded. source `77243-3`; type 32856; number |
| `caregiver_availability_status` | observation; 44786672; LOINC `74014-2` — Last drank alcohol [Date and time] | Current projection. Existing approved; historical origin unrecorded. source `74014-2`; type 32817; boolean REVIEW: last alcohol use is unrelated to caregiver availability. |
| `consent_capability` | Observation; 4226675; SNOMED `405193005` — Caregiver wellbeing status | Unresolved: capacity to consent, not consent given, caregiver wellbeing, or a jurisdiction-specific legal certificate. |
| `contraceptive_use` | observation; 4027509; SNOMED `13197004` — Uses contraception | Current projection. Existing approved; historical origin unrecorded. source `13197004`; type 32817; boolean |
| `diet_type` | measurement; 36303387; LOINC `88365-2` — Glucose [Mass/volume] in Blood --pre-meal | Current projection. Existing approved; historical origin unrecorded. source `88365-2`; type 32856; string REVIEW: pre-meal glucose is unrelated to diet type. |
| `drinks_per_week` | measurement; 2029606278; LOINC `11286-7` — Drinks per week | Current projection. Existing approved; historical origin unrecorded. source `11286-7`; type 32856; number  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `education_level` | measurement; 42528763; LOINC `82589-3` — Highest level of education | Current projection. Existing approved; historical origin unrecorded. source `82589-3`; type 32856; string  REVIEW: stored table measurement differs from concept domain Observation. |
| `employment_status` | observation; 4073163; SNOMED `224362002` — Employment status | Current projection. Existing approved; historical origin unrecorded. source `224362002`; type 32817; string |
| `exercise_frequency` | measurement; 40771101; LOINC `68516-4` — On those days that you engage in moderate to strenuous exercise, how many minutes, on average, do you exercise | Current projection. Existing approved; historical origin unrecorded. source `68516-4`; type 32856; string REVIEW: current concept asks minutes per day, not frequency. REVIEW: stored table measurement differs from concept domain Observation. |
| `exercise_minutes_per_week` | measurement; 36305482; LOINC `89555-7` — How many days per week did you engage in moderate to strenuous physical activity in the last 30 days | Current projection. Existing approved; historical origin unrecorded. source `89555-7`; type 32856; number REVIEW: current concept asks days per week, not minutes. REVIEW: stored table measurement differs from concept domain Observation. |
| `geographic_exposure_risk_details` | Observation; 4168974; SNOMED `420008001` — Travel | Unresolved: geography, occupation, environment and infection exposure narrative; Travel alone does not cover the field. |
| `marital_status` | measurement; 3046344; LOINC `45404-1` — Marital status | Current projection. Existing approved; historical origin unrecorded. source `45404-1`; type 32856; string  REVIEW: stored table measurement differs from concept domain Observation. |
| `no_geographic_exposure_risk` | observation; 42529548; LOINC `82593-5` — Immunization summary report | Current projection. Existing approved; historical origin unrecorded. source `82593-5`; type 32817; boolean REVIEW: immunization report is unrelated to exposure risk. REVIEW: stored table observation differs from concept domain Note. |
| `no_mental_health_disorder_status` | Contract required; No verified concept | Unresolved polarity: UI label says Mental Health Disorders but stored field starts no_. Confirm the boolean meaning and select a status question/answer mapping. |
| `no_substance_use_status` | Contract required; No verified concept | Unresolved polarity: UI label says Non-prescription Recreational Drug Use but stored field starts no_. Confirm meaning before approving a coded answer. |
| `number_of_dependents` | measurement; 40766239; LOINC `63512-8` — How many people are living or staying at this address [#] | Current projection. Existing approved; historical origin unrecorded. source `63512-8`; type 32856; number REVIEW: household size is not number of dependents. REVIEW: stored table measurement differs from concept domain Observation. |
| `pack_years` | measurement; 40766364; LOINC `63640-7` — How many cigarettes per day do, or did, you smoke | Current projection. Existing approved; historical origin unrecorded. source `63640-7`; type 32856; number REVIEW: cigarettes/day does not measure pack-years. REVIEW: stored table measurement differs from concept domain Observation. |
| `pregnancy_test_date` | Observation; 3018954; LOINC `2106-3` — Choriogonadotropin [Presence] in Urine | Companion date: date of pregnancy_test_result_value result; select and update that same event, not a second result with a date as its answer. |
| `pregnancy_test_result_value` | observation; 3018954; LOINC `2106-3` — Choriogonadotropin [Presence] in Urine | Current projection. Existing approved; historical origin unrecorded. source `2106-3`; type 32817; string  REVIEW: stored table observation differs from concept domain Measurement. |
| `sleep_hours_per_night` | measurement; 1002368; LOINC `93832-4` — Sleep duration | Current projection. Existing approved; historical origin unrecorded. source `93832-4`; type 32856; number; unit `h`  REVIEW: stored table measurement differs from concept domain Observation. |
| `sleep_quality` | measurement; 1001932; LOINC `93831-6` — Deep sleep duration | Current projection. Existing approved; historical origin unrecorded. source `93831-6`; type 32856; string REVIEW: deep-sleep duration is not sleep quality. REVIEW: stored table measurement differs from concept domain Observation. |
| `smoking_status` | measurement; 43054909; LOINC `72166-2` — Tobacco smoking status | Current projection. Existing approved; historical origin unrecorded. source `72166-2`; type 32856; string  REVIEW: stored table measurement differs from concept domain Observation. |
| `social_support` | measurement; 37020172; LOINC `93033-9` — Are you worried about losing your housing [PRAPARE] | Current projection. Existing approved; historical origin unrecorded. source `93033-9`; type 32856; string REVIEW: housing insecurity is not general social support. REVIEW: stored table measurement differs from concept domain Observation. |
| `stress_level` | measurement; 44786643; LOINC `73985-4` — Exercise activity | Current projection. Existing approved; historical origin unrecorded. source `73985-4`; type 32856; string REVIEW: exercise activity is not stress level. REVIEW: stored table measurement differs from concept domain Observation. |
| `substance_use_details` | Observation; 4279309; SNOMED `66214007` — Substance abuse | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |

### #1076: Disease-field OMOP concepts and projection contracts

| Field | Destination / concept | Disposition |
|---|---|---|
| `absolute_lymphocyte_count` | measurement; 3019198; LOINC `26474-7` — Lymphocytes [#/volume] in Blood | Current projection. Existing approved; historical origin unrecorded. source `26474-7`; type 32817; number; unit `percent` |
| `autoimmune_cytopenias_refractory_to_steroids` | observation; 2100000028; SNOMED `439478003` — Autoimmune cytopenia | Current projection. Existing approved; historical origin unrecorded. source `439478003`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `binet_stage` | observation; 607090; SNOMED `1149099005` — Binet staging classification for chronic lymphocytic leukemia | Current projection. Existing approved; historical origin unrecorded. source `1149099005`; type 32817; string  REVIEW: stored table Observation differs from concept domain Measurement. |
| `bone_lesions` | Measurement,Observation; 2100000012; LOINC `24646-7` — Bone lesion XR study | Structured list: per-lesion condition_occurrence/measurement facts with site and event linkage; generic XR procedure is not the lesion result. |
| `bone_marrow_involvement` | observation; 2100000001; SNOMED `24940005` — Infiltration of bone marrow | Current projection. Existing approved; historical origin unrecorded. source `24940005`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `bone_only_metastasis_status` | Measurement; 36309629; LOINC `LA4202-3` — Distant recurrence of an invasive tumor in bone only | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `clonal_b_lymphocyte_count` | Observation; 2100000032; LOINC `30374-0` — B cells/100 leukocytes in Blood | Unresolved: requires clonal B-cell count per volume, not B cells per 100 leukocytes. |
| `clonal_bone_marrow_b_lymphocytes` | measurement; 3031240; LOINC `42759-1` — B lymphocytes [#/volume] in Bone marrow | Unresolved: field is clonal B-cell percentage in marrow. Existing 42759-1 is total B-cell count per volume, not clonal percentage; do not approve it. |
| `clonal_plasma_cells` | measurement; 37019751; LOINC `93021-4` — Plasma cells with abnormal marker pattern/Cells counted in Bone marrow by Flow cytometry (FC) | Current projection. Existing approved; historical origin unrecorded. source `93021-4`; type 32817; number; unit `%` |
| `disease` | ConditionOccurrence; 3027027; LOINC `29308-4` — Diagnosis | Value-dependent condition_occurrence.condition_concept_id: resolve the selected Disease code to its approved standard disease concept. A fixed generic Diagnosis concept is not a disease. Requires occurrence writer and negative/clear semantics.  REVIEW: stored table ConditionOccurrence differs from concept domain Observation. |
| `disease_activity` | observation; 2100000002; SNOMED `246456005` — Activity of disease | Current projection. Existing approved; historical origin unrecorded. source `246456005`; type 32817; string  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `dlbcl_transformation_date` | Observation; 2100000008; SNOMED `91860004` — Richter syndrome | Companion date: condition_occurrence.condition_start_date of the transformation event; needs an explicit link to that event. |
| `flipi_score` | observation; 35917496; NAACCR `lymphoma@2910` — Follicular Lymphoma Prognostic Index (FLIPI) | Current projection. Existing approved; historical origin unrecorded. source `lymphoma@2910`; type 32817; number  REVIEW: stored table Observation differs from concept domain Measurement. |
| `flipi_score_options` | Observation; 2100000003; SNOMED `444723004` — FLIPI score | Textual FLIPI categories: map each answer to the appropriate FLIPI Meas Value or Cancer Modifier; do not store option text as a numeric score. |
| `gelf_criteria_status` | observation; 2100000004; SNOMED `109964006` — GELF criteria | Current projection. Existing approved; historical origin unrecorded. source `109964006`; type 32817; string  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `genetic_mutations` | Observation; 40758361; LOINC `55232-3` — Genetic analysis summary panel | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `hepatomegaly` | observation; 197676; SNOMED `80515008` — Large liver | Current projection. Existing approved; historical origin unrecorded. source `80515008`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| `hr_status` | observation; 44791967; SNOMED `310871000000100` — Tumour hormone receptor status | Current projection. Existing approved; historical origin unrecorded. source `310871000000100`; type 32817; string |
| `hrd_status` | observation; 1469934; LOINC `107286-7` — Homologous recombination deficiency status analysis [Presence] in Tissue by Molecular genetics method | Current projection. Existing approved; historical origin unrecorded. source `107286-7`; type 32817; string  REVIEW: stored table Observation differs from concept domain Measurement. |
| `kappa_flc` | measurement; 1091621; LOINC `104544-2` — Kappa light chains.free [Mass/volume] in Serum or Plasma | Current projection. Existing approved; historical origin unrecorded. source `104544-2`; type 32817; number; unit `mg/L` |
| `kappa_lambda_ratio` | measurement; 1091281; LOINC `104546-7` — Kappa light chains.free/Lambda light chains.free [Mass Ratio] in Serum or Plasma | Current projection. Existing approved; historical origin unrecorded. source `104546-7`; type 32817; number; unit `{ratio}` |
| `lambda_flc` | measurement; 3047169; LOINC `33944-0` — Lambda light chains.free [Mass/volume] in Serum or Plasma | Current projection. Existing approved; historical origin unrecorded. source `33944-0`; type 32817; number; unit `mg/L` |
| `lymphadenopathy` | observation; 315085; SNOMED `30746006` — Lymphadenopathy | Current projection. Existing approved; historical origin unrecorded. source `30746006`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| `lymphocyte_doubling_time` | Observation; 3019198; LOINC `26474-7` — Lymphocytes [#/volume] in Blood | Unresolved: doubling interval, including unit and observation window; a lymphocyte count concept is not an interval. |
| `measurable_disease_by_recist_status` | observation; 2100000005; SNOMED `711259004` — Measurable disease by RECIST | Current projection. Existing approved; historical origin unrecorded. source `711259004`; type 32817; boolean  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `measurable_disease_imwg` | Measurement; 2100000005; SNOMED `711259004` — Measurable disease by RECIST | Unresolved: IMWG measurable-disease assessment; RECIST is a different instrument and must not substitute. |
| `measurable_disease_iwcll` | Observation; 2100000005; SNOMED `711259004` — Measurable disease by RECIST | Unresolved: iwCLL measurable-disease assessment; RECIST is a different instrument and must not substitute. |
| `meets_crab` | Contract required; No verified concept | Unresolved: IMWG CRAB assessment with explicit component/result semantics; no suitable loaded concept verified. |
| `meets_slim` | Contract required; No verified concept | Unresolved: IMWG SLiM assessment with explicit component/result semantics; no suitable loaded concept verified. |
| `menopausal_status` | observation; 4172857; SNOMED `276477006` — Menopause finding | Current projection. Existing approved; historical origin unrecorded. source `276477006`; type 32817; string |
| `monoclonal_protein_serum` | measurement; 3046299; LOINC `33358-3` — Protein.monoclonal [Mass/volume] in Serum or Plasma by Electrophoresis | Current projection. Existing approved; historical origin unrecorded. source `33358-3`; type 32817; number; unit `g/dL` |
| `monoclonal_protein_urine` | measurement; 3034655; LOINC `42482-0` — Protein.monoclonal [Mass/time] in 24 hour Urine by Electrophoresis | Current projection. Existing approved; historical origin unrecorded. source `42482-0`; type 32817; number; unit `mg/24h` |
| `mrd_status` | observation; 2100000013; LOINC `98847-0` — Measurable residual disease panel | Current projection. Existing approved; historical origin unrecorded. source `98847-0`; type 32817; string  REVIEW: stored table Observation differs from concept domain Measurement. REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `myeloma_type` | observation; 4190641; SNOMED `415109007` — Plasma cell myeloma - category | Current projection. Existing approved; historical origin unrecorded. source `415109007`; type 32817; string |
| `oncotype_dx_score` | observation; 35933430; NAACCR `breast@2876@010` — Oncotype DX | Current projection. Existing approved; historical origin unrecorded. source `breast@2876@010`; type 32817; number REVIEW: confirm score versus assay and use the concept domain table. REVIEW: stored table Observation differs from concept domain Meas Value. |
| `pd_l1_tumor_cells` | measurement; 1092047; LOINC `105304-0` — Tumor cells.Programmed cell death ligand 1/Viable tumor cells in Tissue by Immune stain | Current projection. Existing approved; historical origin unrecorded. source `105304-0`; type 32817; number; unit `%` |
| `post_transformation_outcome` | Observation; 2100000008; SNOMED `91860004` — Richter syndrome | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `progression` | observation; 4077864; SNOMED `246450006` — Progression | Current projection. Existing approved; historical origin unrecorded. source `246450006`; type 32817; string |
| `protein_expressions` | observation; 4155622; SNOMED `371511004` — Tumor immunophenotyping status | Current projection. Existing approved; historical origin unrecorded. source `371511004`; type 32817; string |
| `qtcf_value` | measurement; 2100000015; LOINC `8632-1` — QTcF interval | Current projection. Existing approved; historical origin unrecorded. source `8632-1`; type 32817; number; unit `ms`  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `richter_transformation` | observation; 4173956; SNOMED `277550009` — Richter's syndrome | Current projection. Existing approved; historical origin unrecorded. source `277550009`; type 32817; string  REVIEW: stored table Observation differs from concept domain Condition. |
| `sct_date` | observation; 32817; Type Concept `OMOP4976890` — EHR | Companion date: procedure_occurrence.procedure_date of the selected transplant. Existing EHR type-as-question mapping requires review.  REVIEW: stored table observation differs from concept domain Type Concept. |
| `sct_eligibility` | observation; 32817; Type Concept `OMOP4976890` — EHR | Current projection. Existing approved; historical origin unrecorded. source `mm-sct-eligibility`; type 32817; string REVIEW: EHR (32817) is a provenance type, not an eligibility question. REVIEW: stored table observation differs from concept domain Type Concept. Structured field: scalar mapping alone does not prove lossless list projection/readback. |
| `serum_beta2_microglobulin_level` | measurement (candidate); 3013201; LOINC `1952-1` — Beta-2-Microglobulin [Mass/volume] in Serum or Plasma | WITHHELD from approval: PatientRecord-first proposal; UI tab Disease; issues #1069–#1079. Beta-2 microglobulin, not beta-2 globulin electrophoresis. Canonical beta2_microglobulin owns 1952-1; keep this source distinct or implement alias routing. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `spleen_size` | Observation; 200527; SNOMED `16294009` — Splenomegaly | Unresolved: quantitative spleen dimension in cm, not the Condition concept Splenomegaly. |
| `splenomegaly` | observation; 200527; SNOMED `16294009` — Splenomegaly | Current projection. Existing approved; historical origin unrecorded. source `16294009`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| `staging_modalities` | observation; 4219603; SNOMED `399390009` — TNM stage grouping | Current projection. Existing approved; historical origin unrecorded. source `399390009`; type 32817; string REVIEW: TNM stage grouping is a stage, not a modality. |
| `stem_cell_transplant_history` | observation; 32817; Type Concept `OMOP4976890` — EHR | Current projection. Existing approved; historical origin unrecorded. source `mm-sct-history`; type 32817; string REVIEW: EHR (32817) belongs in the type column, not observation_concept_id. REVIEW: stored table observation differs from concept domain Type Concept. Structured field: scalar mapping alone does not prove lossless list projection/readback. |
| `test_date` | Observation; 3045429; LOINC `33882-2` — Collection date of Specimen | Companion date: link the relevant biomarker/genomics test event before writing measurement_date; a generic PatientRecord date does not identify which result. |
| `test_methodology` | measurement (candidate); 42527891; LOINC `85069-3` — Lab test method [Type] | WITHHELD from approval: PatientRecord-first proposal; UI tab Disease; issues #1069–#1079. Lab test method question; preserve NGS/IHC/FISH/PCR answer and link to the corresponding test event. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `transformed_to_dlbcl` | observation; 432574; SNOMED `109969005` — Diffuse large B-cell lymphoma | Current projection. Existing approved; historical origin unrecorded. source `109969005`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| `tumor_burden` | observation; 2100000011; SNOMED `246923001` — Tumor burden | Current projection. Existing approved; historical origin unrecorded. source `246923001`; type 32817; string  REVIEW: locally numbered vocabulary row; verify its provenance rather than assuming Athena standard status. |
| `tumor_grade` | measurement; 4160340; SNOMED `371469007` — Histologic grade of neoplasm | Current projection. Existing approved; historical origin unrecorded. source `371469007`; type 32817; number  REVIEW: stored table Measurement differs from concept domain Observation. |

### #1077: General-field OMOP concepts and profile/language exceptions

| Field | Destination / concept | Disposition |
|---|---|---|
| `ecog_assessment_date` | Observation; 36305384; LOINC `89247-1` — ECOG Performance Status score | Companion date: measurement.measurement_date for ecog_performance_status (LOINC 89247-1; 36305384). Needs linked event-date handling; do not store a score concept with a date answer. |
| `english_read` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `english_speak` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `english_understand` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `english_write` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `hepatitis_b_status` | observation; 4281232; SNOMED `66071002` — Type B viral hepatitis | Current projection. Existing approved; historical origin unrecorded. source `66071002`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| `hepatitis_c_status` | observation; 4150744; SNOMED `278973007` — Hepatitis C status | Current projection. Existing approved; historical origin unrecorded. source `278973007`; type 32817; string |
| `hiv_status` | observation; 439727; SNOMED `86406008` — Human immunodeficiency virus infection | Current projection. Existing approved; historical origin unrecorded. source `86406008`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Condition. |
| `no_active_infection_status` | observation (candidate); 4232893; SNOMED `405009004` — Infection status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. Infection status question. True in this PatientRecord field means absence; coded negative-answer/polarity handling must be reviewed before approval. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `no_hepatitis_b_status` | observation (candidate); 4150742; SNOMED `278969009` — Hepatitis B status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. Hepatitis B status question; True means absence. Coded negative-answer/polarity handling required. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `no_hepatitis_c_status` | observation (candidate); 4150744; SNOMED `278973007` — Hepatitis C status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. Hepatitis C status question; True means absence. Coded negative-answer/polarity handling required. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `no_hiv_status` | observation (candidate); 4149958; SNOMED `278977008` — HIV status | WITHHELD from approval: PatientRecord-first proposal; UI tab General; issues #1069–#1079. HIV status question; True means absence, not HIV infection. Coded negative-answer/polarity handling required. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `no_other_active_malignancies` | Contract required; No verified concept | Unresolved: explicit absence of other active malignancies; needs question and negative-answer concepts, not a generic malignant diagnosis. |
| `peripheral_neuropathy_grade` | observation; 46235473; LOINC `75691-6` — Peripheral sensory neuropathy grade NCICTC | Current projection. Existing approved; historical origin unrecorded. source `75691-6`; type 32817; number |
| `preexisting_conditions` | Observation; 4010833; SNOMED `102478008` — Pre-existing condition | Structured list: resolve each category to a condition_occurrence concept, with dated occurrence evidence. Do not comma-join diagnoses under one generic question. |
| `spanish_read` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `spanish_speak` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `spanish_understand` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |
| `spanish_write` | PersonLanguageSkill (extension); Per-language capability rows | Separate language-skills API; flattened PatientRecord flags are derived. No independent scalar concept mapping. |

### #1078: Labs-field OMOP concepts and diagnostic results

| Field | Destination / concept | Disposition |
|---|---|---|
| `bone_imaging_result` | Observation; 2100000012; LOINC `24646-7` — Bone lesion XR study | Unresolved: boolean Normal result without modality/body site. A bone imaging procedure or XR study concept alone does not express this result. |
| `creatinine_clearance_ml_min` | measurement (candidate); 3005770; LOINC `2164-2` — Creatinine renal clearance in 24 hour Urine and Serum or Plasma | WITHHELD from approval: PatientRecord-first proposal; UI tab Labs; issues #1069–#1079. Candidate only for measured 24-hour urine/serum clearance; confirm specimen and method. Estimated Cockcroft-Gault clearance needs a different concept. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |
| `pulmonary_function_test_result` | observation (candidate); 4023991; SNOMED `106053004` — Pulmonary function | WITHHELD from approval: PatientRecord-first proposal; UI tab Labs; issues #1069–#1079. Pulmonary function assessment; UI boolean means Normal. Review normal/abnormal answer encoding before approval; not a numeric lung volume. Proposed only; requires clinical review and projection/readback validation. No clinical facts changed. |

### #1079: Remaining legacy fields and OMOP projection contracts

| Field | Destination / concept | Disposition |
|---|---|---|
| `active_infection_status` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `active_malignancies` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `albumin_level` | Observation; 3024561; LOINC `1751-7` — Albumin [Mass/volume] in Serum or Plasma | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `biopsy_grade` | measurement; 3047285; LOINC `44648-4` — Histologic grade [Score] in Breast cancer specimen by Nottingham | Current projection. Existing approved; historical origin unrecorded. source `44648-4`; type 32817; number |
| `biopsy_grade_depr` | Observation; 3046328; LOINC `33732-9` — Histology grade [Identifier] in Cancer specimen | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `concomitant_medication_date` | Observation; 4208903; SNOMED `439771001` — Date of event | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `concomitant_medication_details` | observation; 3023330; LOINC `18605-6` — Medication current Set | Current projection. Existing approved; historical origin unrecorded. source `18605-6`; type 32817; string |
| `concomitant_medications` | observation; 3040033; LOINC `52418-1` — Current medication, Name | Current projection. Existing approved; historical origin unrecorded. source `52418-1`; type 32817; string |
| `condition_clinical_status` | observation; 1989567; LOINC `99493-9` — Condition clinical status | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `condition_code_icd_10` | Observation; 3027027; LOINC `29308-4` — Diagnosis | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `condition_code_snomed_ct` | Observation; 3027027; LOINC `29308-4` — Diagnosis | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `cytogenetic_markers` | Read-only legacy summary; see [Genomics architecture](docs/genomics_architecture.md) for current ownership and history access. | New discrete findings belong to Genomics. PatientRecord PATCH rejects changed summaries; retained internal projectors support import/history compatibility. |
| `death_date` | observation; 0 — no matching standard concept | Current projection. Existing approved; historical origin unrecorded. source `patient-record:death_date`; type 32817; date |
| `diagnosis_date` | observation; 40766651; LOINC `63931-0` — Date of diagnosis | Current projection. Existing approved; historical origin unrecorded. source `63931-0`; type 32817; date |
| `disease_slug` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `ejection_fraction` | measurement; 3019817; LOINC `8806-2` — Left ventricular Ejection fraction by 2D echo | Current projection. Existing approved; historical origin unrecorded. source `8806-2`; type 32817; number; unit `%` |
| `heartrate_variability` | measurement; 21491502; LOINC `80404-7` — R-R interval.standard deviation (Heart rate variability) | Current projection. Existing approved; historical origin unrecorded. source `80404-7`; type 32817; number; unit `ms` |
| `hemoglobin_level` | Observation; 3000963; LOINC `718-7` — Hemoglobin [Mass/volume] in Blood | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `languages_skills` | Observation; 4267143; SNOMED `61909002` — Language | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `metastatic_status` | observation; 32944; Episode `OMOP4997716` — Metastatic Disease | Current projection. Existing approved; historical origin unrecorded. source `OMOP4997716`; type 32817; boolean  REVIEW: stored table Observation differs from concept domain Episode. |
| `no_concomitant_medication_status` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `no_pre_existing_conditions` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `no_pregnancy_or_lactation_status` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `no_tobacco_use_status` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `patient_age` | Person; 3022304; LOINC `30525-0` — Age | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `pd_l1_assay` | Observation; 1091989; LOINC `105302-4` — Immune stain PD-L1 scoring method - Tissue | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `plasma_cell_leukemia` | Measurement,Observation; 133154; SNOMED `95210003` — Plasma cell leukemia | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `platelet_count` | Observation; 3024929; LOINC `777-3` — Platelets [#/volume] in Blood by Automated count | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `pregnancy_test_result` | Observation; 3018954; LOINC `2106-3` — Choriogonadotropin [Presence] in Urine | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `prior_procedures` | ProcedureOccurrence; 4322976; SNOMED `71388002` — Procedure | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `remission_duration` | Unresolved; No verified concept | PatientRecord edit retained; no active projection. No verified concept candidate; tracked in the repurposed issue. |
| `remission_duration_min` | Observation; 4172372; SNOMED `277022003` — Remission phase | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `renal_adequacy_status` | Measurement; 36716945; SNOMED `723188008` — Renal insufficiency | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `serum_bilirubin_level_total` | Observation; 3024128; LOINC `1975-2` — Bilirubin.total [Mass/volume] in Serum or Plasma | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `serum_calcium_level` | Observation; 3006906; LOINC `17861-6` — Calcium [Mass/volume] in Serum or Plasma | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `serum_creatinine_level` | Observation; 3016723; LOINC `2160-0` — Creatinine [Mass/volume] in Serum or Plasma | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `temperature` | —; — | No concrete PatientRecord column in current dev. UI/API virtual field or legacy control; no mapping created. |
| `tobacco_use_details` | Observation; 4275495; SNOMED `365981007` — Tobacco smoking behavior - finding | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `toxicity_grade` | Observation; 4077563; SNOMED `246112005` — Severity | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |
| `tumor_size` | observation; 3018102; LOINC `21889-1` — Size Tumor | Current projection. Existing approved; historical origin unrecorded. source `21889-1`; type 32817; number; unit `cm`  REVIEW: stored table Observation differs from concept domain Measurement. |
| `wearable_last_sync_at` | —; — | Excluded: Bookkeeping about the device feed rather than a reading; follows from ingesting wearable data. |
| `white_blood_cell_count` | measurement; 4298431; SNOMED `767002` — White blood cell count | PatientRecord edit retained; no active projection. Existing proposal requires review; not automatically approved. |

## Sources and verification

- Current UI components named above; `patient_portal/api/views.py`; `omop_core/services/write_descriptor.py`; `omop_core/services/omop_projection.py`; `omop_core/signals.py`; `docs/patient-record-first-writes.md`.
- Staging `field_concept_mapping` and `concept` metadata read on 2026-09-11; no patient-level data appears in this document.
- [OMOP CDM v5.4](https://ohdsi.github.io/CommonDataModel/cdm54.html): table/domain definitions, concept references and separate value, unit, source and type fields. Application-specific storage and exceptions above are taken from repository code.
