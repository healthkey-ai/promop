"""
provenance_registry.py — Declarative mapping of PatientRecord fields to OMOP sources.

Each entry describes how a PatientRecord field was derived so the provenance
service can re-execute the same query and return the underlying OMOP rows.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldProvenance:
    """Describes how to trace a PatientRecord field back to OMOP."""

    omop_table: str
    """Primary OMOP table, e.g. 'Measurement', 'Observation'."""

    lookup_strategy: str
    """One of: 'loinc', 'source_value', 'concept_name', 'snomed',
    'condition', 'drug_exposure', 'procedure', 'person', 'composite'."""

    concept_codes: list[str] | None = None
    """LOINC or SNOMED codes used in the section extractor."""

    source_values: list[str] | None = None
    """Fallback source_value strings for name-based lookups."""

    extractor: str = ""
    """Name of the section extractor function, e.g. '_get_laboratory_data'."""

    selection_rule: str = "latest"
    """How the value was selected: 'latest', 'earliest', 'aggregate',
    'composite', 'all', 'boolean'."""

    description: str = ""
    """Human-readable explanation of the derivation."""

    constituent_fields: list[str] = field(default_factory=list)
    """For composite fields — the sub-fields whose provenance should be merged."""


def _auto_register_loinc_labs() -> dict[str, FieldProvenance]:
    """Build registry entries from _LOINC_LAB_FIELDS and _SOURCE_VALUE_LAB_FIELDS."""
    from omop_core.services.patient_record_service import (
        _LOINC_LAB_FIELDS,
        _SOURCE_VALUE_LAB_FIELDS,
    )

    # Invert: field_name → set of LOINC codes
    field_to_codes: dict[str, list[str]] = {}
    for code, (field_name, _cast) in _LOINC_LAB_FIELDS.items():
        field_to_codes.setdefault(field_name, []).append(code)

    # Invert: field_name → set of source values
    field_to_svs: dict[str, list[str]] = {}
    for sv, field_name in _SOURCE_VALUE_LAB_FIELDS.items():
        field_to_svs.setdefault(field_name, []).append(sv)

    entries: dict[str, FieldProvenance] = {}
    for field_name, codes in field_to_codes.items():
        entries[field_name] = FieldProvenance(
            omop_table="Measurement",
            lookup_strategy="loinc",
            concept_codes=sorted(codes),
            source_values=sorted(field_to_svs.get(field_name, [])),
            extractor="_get_laboratory_data",
            selection_rule="latest",
            description=f"Latest Measurement by LOINC {', '.join(sorted(codes))}",
        )
    return entries


# ---------------------------------------------------------------------------
# Manual entries for fields NOT covered by _LOINC_LAB_FIELDS
# ---------------------------------------------------------------------------

_MANUAL_ENTRIES: dict[str, FieldProvenance] = {
    # --- Disease / condition ---
    "disease": FieldProvenance(
        omop_table="ConditionOccurrence",
        lookup_strategy="condition",
        extractor="_get_disease_data",
        selection_rule="latest",
        description="Primary condition from most recent ConditionOccurrence",
    ),
    "diagnosis_date": FieldProvenance(
        omop_table="ConditionOccurrence",
        lookup_strategy="condition",
        extractor="_get_disease_data",
        selection_rule="earliest",
        description="Earliest condition_start_date from ConditionOccurrence",
    ),
    "death_date": FieldProvenance(
        omop_table="Death",
        lookup_strategy="person",
        extractor="_get_demographics",
        selection_rule="latest",
        description="Death date from Death table",
    ),
    "condition_clinical_status": FieldProvenance(
        omop_table="ConditionOccurrence",
        lookup_strategy="condition",
        extractor="_get_disease_data",
        selection_rule="latest",
        description="Clinical status of the primary condition",
    ),
    "disease_slug": FieldProvenance(
        omop_table="ConditionOccurrence",
        lookup_strategy="condition",
        extractor="_get_disease_data",
        selection_rule="latest",
        description="URL slug derived from disease name",
    ),

    # --- Vitals ---
    "systolic_blood_pressure": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="loinc",
        concept_codes=["8480-6"],
        extractor="_get_vitals_data",
        selection_rule="latest",
        description="Latest systolic BP by LOINC 8480-6",
    ),
    "diastolic_blood_pressure": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="loinc",
        concept_codes=["8462-4"],
        extractor="_get_vitals_data",
        selection_rule="latest",
        description="Latest diastolic BP by LOINC 8462-4",
    ),
    "heartrate": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="loinc",
        concept_codes=["8867-4"],
        extractor="_get_vitals_data",
        selection_rule="latest",
        description="Latest heart rate by LOINC 8867-4",
    ),
    "temperature": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="loinc",
        concept_codes=["8310-5"],
        extractor="_get_vitals_data",
        selection_rule="latest",
        description="Latest body temperature by LOINC 8310-5",
    ),
    "weight": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="loinc",
        concept_codes=["29463-7"],
        extractor="_get_vitals_data",
        selection_rule="latest",
        description="Latest body weight by LOINC 29463-7",
    ),
    "height": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="loinc",
        concept_codes=["8302-2"],
        extractor="_get_vitals_data",
        selection_rule="latest",
        description="Latest body height by LOINC 8302-2",
    ),

    # --- Performance ---
    "ecog_performance_status": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_performance_data",
        selection_rule="latest",
        description="Latest ECOG performance status from Observation",
    ),
    "karnofsky_performance_score": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_performance_data",
        selection_rule="latest",
        description="Latest Karnofsky score from Observation",
    ),
    "ecog_assessment_date": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_performance_data",
        selection_rule="latest",
        description="Date of most recent ECOG assessment",
    ),

    # --- Staging ---
    "stage": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_staging_data",
        selection_rule="latest",
        description="Overall cancer stage from Observation",
    ),
    "tumor_stage": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_staging_data",
        selection_rule="latest",
        description="T component of TNM staging",
    ),
    "nodes_stage": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_staging_data",
        selection_rule="latest",
        description="N component of TNM staging",
    ),
    "distant_metastasis_stage": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_staging_data",
        selection_rule="latest",
        description="M component of TNM staging",
    ),

    # --- Biomarkers ---
    "estrogen_receptor_status": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_biomarker_data",
        selection_rule="latest",
        description="ER status from Observation",
    ),
    "progesterone_receptor_status": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_biomarker_data",
        selection_rule="latest",
        description="PR status from Observation",
    ),
    "her2_status": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_biomarker_data",
        selection_rule="latest",
        description="HER2 status from Observation",
    ),
    "ki67_proliferation_index": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_biomarker_data",
        selection_rule="latest",
        description="Ki-67 proliferation index from Observation",
    ),
    "pd_l1_tumor_cells": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_biomarker_data",
        selection_rule="latest",
        description="PD-L1 tumor proportion score from Observation",
    ),

    # --- Treatment lines ---
    "first_line_therapy": FieldProvenance(
        omop_table="DrugExposure",
        lookup_strategy="drug_exposure",
        extractor="_get_treatment_data",
        selection_rule="earliest",
        description="First-line therapy name from DrugExposure/Episode",
    ),
    "second_line_therapy": FieldProvenance(
        omop_table="DrugExposure",
        lookup_strategy="drug_exposure",
        extractor="_get_treatment_data",
        selection_rule="latest",
        description="Second-line therapy name from DrugExposure/Episode",
    ),
    "later_therapy": FieldProvenance(
        omop_table="DrugExposure",
        lookup_strategy="drug_exposure",
        extractor="_get_treatment_data",
        selection_rule="latest",
        description="Later-line therapy name from DrugExposure/Episode",
    ),

    # --- Genetic mutations ---
    "genetic_mutations": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_genetic_mutations",
        selection_rule="all",
        description="All genetic mutation Observations (structured JSON)",
    ),

    # --- Procedures ---
    "prior_procedures": FieldProvenance(
        omop_table="ProcedureOccurrence",
        lookup_strategy="procedure",
        extractor="_get_prior_procedures",
        selection_rule="all",
        description="All ProcedureOccurrence rows for the patient",
    ),

    # --- SCT ---
    "stem_cell_transplant_history": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_sct_cytogenetic_data",
        selection_rule="all",
        description="SCT history from FHIR extensions / Observation",
    ),
    "sct_date": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_sct_cytogenetic_data",
        selection_rule="latest",
        description="SCT date from FHIR extensions / Observation",
    ),
    "sct_eligibility": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_sct_cytogenetic_data",
        selection_rule="all",
        description="SCT eligibility from FHIR extensions / Observation",
    ),
    "cytogenic_markers": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="snomed",
        extractor="_get_sct_cytogenetic_data",
        selection_rule="latest",
        description="Cytogenetic markers from Observation",
    ),

    # --- Computed / composite fields ---
    "bmi": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="Computed from weight and height",
        constituent_fields=["weight", "height"],
    ),
    "hr_status": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="HR+ if ER+ or PR+; HR- if both negative",
        constituent_fields=["estrogen_receptor_status", "progesterone_receptor_status"],
    ),
    "metastatic_status": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="True when distant_metastasis_stage contains M1",
        constituent_fields=["distant_metastasis_stage"],
    ),
    "renal_adequacy_status": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="eGFR >= 30 or creatinine <= 1.5",
        constituent_fields=["egfr_ml_min_173m2", "serum_creatinine_mg_dl"],
    ),
    "tp53_disruption": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="True if genetic_mutations contains pathogenic TP53",
        constituent_fields=["genetic_mutations"],
    ),
    "measurable_disease_imwg": FieldProvenance(
        omop_table="Measurement",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="IMWG measurable disease based on M-spike, FLC ratio",
        constituent_fields=[
            "monoclonal_protein_serum", "monoclonal_protein_urine",
            "kappa_flc", "lambda_flc",
        ],
    ),
    "measurable_disease_iwcll": FieldProvenance(
        omop_table="Observation",
        lookup_strategy="composite",
        extractor="_compute_derived_fields",
        selection_rule="composite",
        description="iwCLL measurable disease based on ALC, node size, spleen, liver",
        constituent_fields=[
            "absolute_lymphocyte_count", "largest_lymph_node_size",
            "splenomegaly", "hepatomegaly",
        ],
    ),
    "meets_crab": FieldProvenance(
        omop_table="Measurement,Observation,ConditionOccurrence",
        lookup_strategy="composite",
        extractor="_get_mm_specific_data",
        selection_rule="composite",
        description="Composite CRAB criteria: calcium > 11, creatinine > 2, hemoglobin < 10, bone lesions",
        constituent_fields=[
            "serum_calcium_mg_dl", "serum_creatinine_mg_dl",
            "hemoglobin_g_dl", "bone_lesions",
        ],
    ),
    "meets_slim": FieldProvenance(
        omop_table="Measurement,Observation",
        lookup_strategy="composite",
        extractor="_get_mm_specific_data",
        selection_rule="composite",
        description="Composite SLiM criteria: sixty% plasma cells, light chain ratio, MRI lesions",
        constituent_fields=["clonal_plasma_cells", "kappa_flc", "lambda_flc"],
    ),
}


def get_registry() -> dict[str, FieldProvenance]:
    """Return the complete field→provenance registry, lazily built."""
    registry = _auto_register_loinc_labs()
    registry.update(_MANUAL_ENTRIES)
    return registry
