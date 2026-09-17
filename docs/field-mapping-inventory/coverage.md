# Field and value reference inventory

Snapshot: 2026-09-14T15:07:52.840817+00:00. Schema: 1.

**Inventory remains incomplete. No candidates are clinically approved by this export.**

7767 source rows; 5906 have no direct scalar-field destination (see source/catalog routing below); 5042 have no attached candidate.

| Source | Rows |
|---|---:|
| cancerbot_history | 394 |
| cancerbot_live | 4105 |
| cancerbot_source | 175 |
| cancerbot_static | 82 |
| descriptor_option | 211 |
| frontend_constant | 505 |
| frontend_control | 413 |
| frontend_provider | 41 |
| genomics_catalog | 42 |
| promop_catalog | 977 |
| promop_choice | 262 |
| promop_field | 420 |
| promop_model_choice | 140 |

| Disposition | Rows |
|---|---:|
| ambiguous | 80 |
| needs_review | 4097 |
| not_applicable | 404 |
| requires_structured_representation | 3186 |

## Coverage gaps

- CancerBot: 0 public lists lack reference coverage; 0 planned lists lack source eligibility coverage. Reference membership is distinct from destination and clinical mapping approval.
- CancerBot source routes are recorded separately from immutable source context; missing destinations and clinical equivalence remain review items. Seed/migration retirement history still requires reconciliation.
- Frontend: 0 controls and 0 constants lack provider accounting. Descriptor source: base_descriptors_captured. Source routing does not certify destination semantics.
- Reference catalogs preserve codes, links and destination candidates; unresolved destination/context is never inferred from labels.
- Exact labels and synonyms are lexical evidence only; case-sensitive search is not exhaustive and no-equivalent requires separate review.
- Existing approved statuses are preserved; candidate semantic meaning, destination domains and vocabulary lineage still require review.

## CancerBot public-list reconciliation

| Coverage | Lists |
|---|---:|
| covered_by_live_export | 118 |
| covered_by_static_source | 51 |
| excluded_trial_search | 3 |

Static results describe the checked-in source definitions, including empty and blank-only lists. They do not certify a deployed CancerBot version. Trial-search exclusions retain their source evidence. Unresolved providers below require reference data; model names come from source imports, not label matching.

| Unresolved list | Source models |
|---|---|

## Implementation destination accounting

Source/consumer routing assigns implementation work. It does not approve concepts, merge aliases, infer retirement or certify runtime field availability.

| Destination evidence | Source rows |
|---|---:|
| existing_reference_catalog | 977 |
| field_destination_recorded | 1542 |
| frontend_consumers_recorded | 290 |
| historical_source_evidence | 394 |
| retained_source_evidence | 202 |
| source_consumers_recorded | 4360 |
| trial_search_metadata | 2 |

Rows awaiting a source/consumer routing disposition: 0. Missing runtime fields and ambiguous clinical contexts remain explicit even when source routing is known.

## CancerBot destination routing

Source review status: `source_routes_recorded`.

Routes retain source disease, line, nested keys and representation warnings separately from immutable source identity. A recorded route is not clinical equivalence or an approved answer mapping.

| Route status | Bindings |
|---|---:|
| destination_missing | 8 |
| source_route_recorded | 164 |

| Source list | Missing PRomop destination |
|---|---|
| diseaseBehaviorsMcl | disease_behavior |
| diseaseSubtypesMcl | disease_subtype |
| morphologicVariants | morphologic_variant |
| bulkyDiseaseCriteria | bulky_disease_criteria |
| highRiskMclCriteria | high_risk_mcl_criteria |
| extranodalSites | extranodal_sites |
| mipiRisks | mipi_risk |
| mipiCRisks | mipi_c_risk |

## CancerBot-derived staging therapy catalogs

Staging is the authoritative source for these catalogs, as confirmed by the user. Catalog and disease/round link rows are already included in the reference export.

| Catalog | Rows |
|---|---:|
| regimens | 244 |
| components | 187 |
| classes | 91 |
| disease_round_links | 681 |

| Public list | Catalog options | Disease code | Round |
|---|---:|---|---|
| therapiesAll | 244 | all | all |
| therapyComponentsAll | 187 | all | all |
| therapyTypesAll | 91 | all | all |
| therapiesMm | 111 | C3242 | all |
| therapiesFl | 86 | C3209 | all |
| therapiesBc | 124 | C9335 | all |
| therapiesCll | 89 | C2987 | all |
| therapiesMcl | 98 | MCL | all |
| therapyComponentsMm | 89 | C3242 | all |
| therapyComponentsFl | 74 | C3209 | all |
| therapyComponentsBc | 98 | C9335 | all |
| therapyTypesMm | 51 | C3242 | all |
| therapyTypesFl | 50 | C3209 | all |
| therapyTypesBc | 53 | C9335 | all |
| therapiesFirstLineMm | 20 | C3242 | first_line_therapy |
| therapiesFirstLineFl | 8 | C3209 | first_line_therapy |
| therapiesFirstLineBc | 23 | C9335 | first_line_therapy |
| therapiesFirstLineCll | 23 | C2987 | first_line_therapy |
| therapiesFirstLineMcl | 14 | MCL | first_line_therapy |
| therapiesSecondLineMm | 21 | C3242 | second_line_therapy |
| therapiesSecondLineFl | 17 | C3209 | second_line_therapy |
| therapiesSecondLineBc | 24 | C9335 | second_line_therapy |
| therapiesSecondLineCll | 14 | C2987 | second_line_therapy |
| therapiesSecondLineMcl | 20 | MCL | second_line_therapy |
| therapiesLaterLineMm | 31 | C3242 | later_line_therapy |
| therapiesLaterLineFl | 22 | C3209 | later_line_therapy |
| therapiesLaterLineBc | 40 | C9335 | later_line_therapy |
| therapiesLaterLineCll | 14 | C2987 | later_line_therapy |
| therapiesLaterLineMcl | 22 | MCL | later_line_therapy |
| supportiveTherapiesMm | 59 | C3242 | supportive_therapy |
| supportiveTherapiesFl | 56 | C3209 | supportive_therapy |
| supportiveTherapiesBc | 76 | C9335 | supportive_therapy |
| supportiveTherapiesCll | 59 | C2987 | supportive_therapy |
| supportiveTherapiesMcl | 69 | MCL | supportive_therapy |

Staging membership above is preserved as independent evidence. Where live CancerBot rows are supplied, the public binding references those rows instead. PlannedTherapy is a separate CancerBot source catalog; its disease/round eligibility never proves administration or a mapping to a PRomop regimen. Planned lists still awaiting source eligibility: 0. Unknown/Other sentinels are recorded separately from catalog counts.


## CancerBot source history

Definitions only; no migration, patient or trial table is read or executed. Recorded scopes distinguish clinical source repairs from trial/platform metadata. Definition coverage never certifies execution, clinical aliases or current membership. Absence never establishes retirement.

Migration definition accounting complete: True.

| History evidence | Count |
|---|---:|
| schema_events | 91 |
| catalog_events | 10 |
| historical_seed_options | 309 |
| source_scope_recorded | 51 |
| literal_seed_definitions_recorded | 20 |
| source_noop | 21 |
| reviewed_catalog_rule | 6 |

| Catalog scope | Old code | Replacement | Rule |
|---|---|---|---|
| PatientInfo | `40` | `(none)` | Clear the obsolete numeric grade-4 value to null; this is not selection of Unknown or a valid breast biopsy grade. |
| Disease | `mm` | `MM` | Repoint disease relationships and deduplicate, or rename when uppercase counterpart is absent. Do not normalize unrelated codes. |
| Disease | `fl` | `FL` | Repoint disease relationships and deduplicate, or rename when uppercase counterpart is absent. Do not normalize unrelated codes. |
| Disease | `bc` | `BC` | Repoint disease relationships and deduplicate, or rename when uppercase counterpart is absent. Do not normalize unrelated codes. |
| TrialType | `targeted_therapy_all` | `(none)` | Trial-type search category removed; this is not a Therapy, component or class catalog retirement. |
| Therapy, TherapyComponent, TherapyComponentCategory | `i` | `ixazomib` | Replacement must exist in the same catalog level; unresolved references abort removal. |
| Therapy, TherapyComponent, TherapyComponentCategory | `s` | `selinexor` | Replacement must exist in the same catalog level; unresolved references abort removal. |
| Therapy, TherapyComponent, TherapyComponentCategory | `t` | `tazemetostat` | Replacement must exist in the same catalog level; unresolved references abort removal. |
| TherapyComponent | `st._john_s_wort` | `st._john's_wort` | Repoint duplicate to canonical component, or rename if canonical absent; no global punctuation normalization. |
| Therapy | `nsaids` | `(none)` | Misclassified regimen removed; concomitant medication remains a separate source identity. |

## Priority gene and marker field mappings (#1311)

42 distinct fields across 51 disease memberships. Source-only parents remain supported by the Genomics Measurement/event contract. An Observation-domain concept cannot certify a standard Measurement parent. Field question coverage does not approve gene/variant answer concepts.

| Disease | Fields | Storage incomplete | Source-only parent | Standard parent candidate requiring review |
|---|---:|---:|---:|---:|
| BC | 6 | 0 | 6 | 0 |
| CLL | 8 | 0 | 8 | 0 |
| FL | 5 | 0 | 5 | 0 |
| MCL | 16 | 0 | 16 | 0 |
| MM | 16 | 0 | 16 | 0 |

## Reconciliation

Duplicate source identities: 0. Shared destination/value/context groups: 258. These groups are evidence for review, not automatic aliases.

| Validation flag | Source rows |
|---|---:|
| existing_candidate_fails_mechanical_screen | 362 |
| existing_mapping_code_differs_from_read_recipe | 10 |
| existing_mapping_domain_disagrees_with_table | 126 |

## Reference table counts

| Table | Rows |
|---|---:|
| custom_patient_field | 0 |
| disease_therapy_regimen | 681 |
| field_choice | 262 |
| field_choice_code | 66 |
| field_concept_mapping | 314 |
| field_formula | 12 |
| field_synonym | 1 |
| omop_core_therapyoutcome | 7 |
| omop_core_therapyoutcome_diseases | 39 |
| therapy_class | 91 |
| therapy_component | 187 |
| therapy_component_class | 490 |
| therapy_regimen | 244 |
| therapy_regimen_component | 391 |
| therapy_round | 5 |
| vocabulary_binet_stage | 3 |
| vocabulary_breast_cancer_first_line_therapy | 16 |
| vocabulary_breast_cancer_later_line_therapy | 33 |
| vocabulary_breast_cancer_second_line_therapy | 11 |
| vocabulary_cancer_stage | 5 |
| vocabulary_disease | 9 |
| vocabulary_disease_activity | 5 |
| vocabulary_disease_progression | 4 |
| vocabulary_distant_metastasis_stage | 3 |
| vocabulary_ecog_status | 6 |
| vocabulary_estrogen_receptor_status | 3 |
| vocabulary_ethnicity | 5 |
| vocabulary_flipi_score | 6 |
| vocabulary_follicular_lymphoma_grade | 4 |
| vocabulary_gelf_criteria | 3 |
| vocabulary_her2_status | 3 |
| vocabulary_histologic_type | 12 |
| vocabulary_hr_status | 4 |
| vocabulary_hrd_status | 2 |
| vocabulary_infection_status | 3 |
| vocabulary_karnofsky_score | 11 |
| vocabulary_language | 3 |
| vocabulary_language_skill_level | 2 |
| vocabulary_measurable_disease | 3 |
| vocabulary_morphologic_variant | 3 |
| vocabulary_mutation_code | 161 |
| vocabulary_mutation_gene | 5 |
| vocabulary_mutation_interpretation | 6 |
| vocabulary_mutation_origin | 2 |
| vocabulary_myeloma_type | 12 |
| vocabulary_nodes_stage | 14 |
| vocabulary_peripheral_neuropathy_grade | 5 |
| vocabulary_post_transformation_outcome | 6 |
| vocabulary_pre_existing_condition_category | 11 |
| vocabulary_progesterone_receptor_status | 3 |
| vocabulary_protein_expression | 18 |
| vocabulary_richter_transformation | 5 |
| vocabulary_sct_eligibility | 4 |
| vocabulary_staging_modality | 3 |
| vocabulary_stem_cell_transplant | 3 |
| vocabulary_toxicity_grade | 5 |
| vocabulary_tumor_burden | 3 |
| vocabulary_tumor_stage | 15 |

## Vocabulary provenance

Both release mechanisms and vocabulary history are recorded separately in the manifest. An Athena release label does not certify individual concept lineage.

- SNOMED: `SNOMED CT (synthetic, benchmark seed)`. All attached candidates fail the vocabulary provenance screen; coordinate #461/#623.
- LOCAL: `synthetic enrichment`. All attached candidates fail the vocabulary provenance screen; coordinate #461/#623.
- sct: `synthetic`. All attached candidates fail the vocabulary provenance screen; coordinate #461/#623.

## Representation decisions

| Scope | Required representation | Owner |
|---|---|---|
| Ethnicity / race | Keep CancerBot ancestry/race categories distinct from the PRomop Hispanic/Latino ethnicity picker; reconcile the legacy Ethnicity lookup without merging Person race and ethnicity. | #1228 |
| bone_lesions | Keep count and >2 comparator separate from presence; do not equate source counts with Yes/No. | #1228 |
| GELF / FLIPI | Retain CancerBot seven and PRomop eight GELF criteria with threshold conflicts explicit; aggregate is separate. Retain five FLIPI inputs, numeric score and risk separately. | #1228 |
| TNM and staging basis | Retain tumor, system, edition and c/p/yp basis. Imaging modality is a separate event. | #1227 |
| ISS / R-ISS / legacy stage; Rai / Binet | Keep disease and staging system in identity; legacy I–IV has unresolved system, never infer R-ISS. | #1228 |
| Markers and genetics | Use frozen Genomics findings/components; retain gene, variant, origin, interpretation, polarity and independent results. | #1229 |
| Treatment outcomes | Retain disease/response system and event; MRD and response categories may require separate observations (#253). | #1228 |
| Therapy catalogs and rounds | Reuse regimen/component/class and disease/round links; preserve planned versus administered status. | #1230 |
| Administrative / computed / legacy | Use existing descriptor categories and formulas; retain compatibility aliases and no-concept administrative representations. | #1223 |
| Unknown / none / absent / equivocal | Preserve distinct typed source keys; missing or cleared values never become selected Unknown. | #1224 |
