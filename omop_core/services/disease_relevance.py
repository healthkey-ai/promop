"""Which standard concepts matter for a disease, for extraction services (#1942).

Meaning (source term to concept) is served by ``code-mappings/lookup``. This
is the second layer: given a disease, which concepts a document extractor
should emphasise. It is built from the fields the patient detail view shows for
that disease, joined to their **approved** ``FieldConceptMapping`` rows, so it
grows as field curation does (#1611, #1076) and never invents a concept.

Three tiers:

* ``disease``: the disease tab's own fields and the genomics priority markers
  the catalog lists for it (#1180).
* ``common``: clinical fields every supported cancer shares (general clinical,
  blood counts, chemistry, treatment), not specific to any one disease.
* ``curated``: concepts a curator accepted from an extractor's
  absent-from-relevance signal (``RelevanceSignal``).

The version changes whenever anything a response could contain changes, so a
service that pins it on a run can tell later that the run used a stale set.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from omop_core.services.genomics_catalog import catalog as genomics_catalog, disease_code, markers

# Bump when the field lists below change meaning, not when curation changes:
# curation is already in the content hash.
CATALOG_VERSION = 1

DISEASES = ('BC', 'FL', 'MM', 'CLL', 'MCL')

# The fields DiseaseTab.tsx renders for each disease, section by section.
# tests/test_disease_relevance.py fails when the two drift apart.
DISEASE_FIELDS: dict[str, frozenset[str]] = {
    'BC': frozenset({
        'histologic_type', 'tumor_grade', 'menopausal_status', 'tumor_stage', 'nodes_stage',
        'staging_modalities', 'distant_metastasis_stage', 'bone_only_metastasis_status',
        'measurable_disease_by_recist_status', 'estrogen_receptor_status',
        'progesterone_receptor_status', 'her2_status', 'hr_status', 'hrd_status',
        'androgen_receptor_status', 'ki67_proliferation_index', 'pd_l1_tumor_cells',
        'oncotype_dx_score', 'test_methodology', 'test_date', 'test_specimen_type',
        'report_interpretation', 'lymph_node_status', 'metastasis_status',
        'pd_l1_combined_positive_score', 'pd_l1_ic_percentage', 'pd_l1_assay',
    }),
    'FL': frozenset({
        'stage', 'tumor_grade', 'bulky_disease',
        'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
        'transformed_to_dlbcl',
        'dlbcl_transformation_date', 'post_transformation_outcome', 'bone_marrow_involvement',
        'clonal_bone_marrow_b_lymphocytes', 'ldh_upper_limit_normal', 'number_of_nodal_sites',
    }),
    'MM': frozenset({
        'mgus', 'smoldering_multiple_myeloma', 'waldenstrom_macroglobulinemia',
        'igm_multiple_myeloma', 'al_amyloidosis', 'poems_syndrome',
        'myeloma_type', 'stage', 'progression', 'measurable_disease_imwg', 'mrd_status',
        'meets_crab', 'meets_slim', 'stem_cell_transplant_history', 'sct_date', 'sct_eligibility',
        'monoclonal_protein_serum', 'monoclonal_protein_urine', 'kappa_flc', 'lambda_flc',
        'kappa_lambda_ratio', 'involved_uninvolved_ratio', 'bone_lesions', 'clonal_plasma_cells',
        'cytogenetic_markers',
    }),
    'CLL': frozenset({
        'binet_stage', 'tumor_burden', 'disease_activity', 'richter_transformation',
        'protein_expressions', 'absolute_lymphocyte_count', 'lymphocyte_doubling_time',
        'serum_beta2_microglobulin_level', 'clonal_b_lymphocyte_count',
        'clonal_bone_marrow_b_lymphocytes', 'qtcf_value', 'largest_lymph_node_size', 'spleen_size',
        'bone_marrow_involvement', 'measurable_disease_iwcll', 'splenomegaly', 'hepatomegaly',
        'lymphadenopathy', 'autoimmune_cytopenias_refractory_to_steroids',
        'btk_inhibitor_refractory', 'bcl2_inhibitor_refractory',
    }),
    'MCL': frozenset({
        'stage', 'protein_expressions', 'ki67_proliferation_index', 'lymphadenopathy',
        'splenomegaly', 'spleen_size', 'hepatomegaly', 'largest_lymph_node_size',
        'mipi_risk', 'mipi_c_risk', 'high_risk_mcl_criteria',
    }),
}

_COMMON_TABS = frozenset({'general', 'disease', 'blood', 'labs', 'treatment'})
_NOT_CLINICAL = frozenset({'internal', 'profile', 'location', 'alias'})


def supported(code: str | None) -> bool:
    return code in DISEASES


def _common_fields(field_names) -> set[str]:
    from omop_core.services.field_descriptor import _classify_field, _classify_tab

    claimed = set().union(*DISEASE_FIELDS.values())
    return {f for f in field_names
            if f not in claimed
            and _classify_tab(f) in _COMMON_TABS
            and _classify_field(f) not in _NOT_CLINICAL}


def _disease_fields(code: str) -> set[str]:
    genomic = {m['field_name'] for m in markers() if code in m['diseases']}
    return set(DISEASE_FIELDS[code]) | genomic


def _approved_mappings() -> dict[str, dict]:
    from omop_core.models import FieldConceptMapping

    rows = (FieldConceptMapping.objects
            .filter(status='approved', concept__isnull=False)
            .values('field_name', 'omop_table', 'concept_id', 'concept__concept_name',
                    'concept__vocabulary_id', 'concept__concept_code', 'concept__domain_id'))
    return {r['field_name']: r for r in rows}


def _synonyms(concept_ids) -> dict[int, list[str]]:
    from omop_core.models import ConceptSynonym

    names = defaultdict(set)
    for concept_id, name in (ConceptSynonym.objects.filter(concept_id__in=concept_ids)
                             .values_list('concept_id', 'concept_synonym_name')):
        names[concept_id].add(name)
    return {k: sorted(v) for k, v in names.items()}


def _accepted_signals() -> dict[str, set[int]]:
    from omop_core.models import RelevanceSignal

    accepted = defaultdict(set)
    for disease, concept_id in (RelevanceSignal.objects.filter(status='accepted')
                                .values_list('disease', 'concept_id').distinct()):
        accepted[disease].add(concept_id)
    return accepted


def _build() -> dict:
    """Every disease's set, plus the version that names the whole state."""
    from omop_core.models import Concept

    mappings = _approved_mappings()
    common = _common_fields(mappings)
    accepted = _accepted_signals()
    curated_ids = set().union(*accepted.values()) if accepted else set()
    curated = {c['concept_id']: c for c in Concept.objects.filter(concept_id__in=curated_ids).values(
        'concept_id', 'concept_name', 'vocabulary_id', 'concept_code', 'domain_id')}
    sets = {}
    for code in DISEASES:
        entries, seen = [], set()
        specific = _disease_fields(code)
        for tier, fields in (('disease', specific), ('common', common - specific)):
            for field in sorted(fields):
                row = mappings.get(field)
                if row is None:
                    continue
                seen.add(row['concept_id'])
                entries.append({
                    'concept_id': row['concept_id'], 'name': row['concept__concept_name'],
                    'vocabulary_id': row['concept__vocabulary_id'],
                    'concept_code': row['concept__concept_code'],
                    'domain_id': row['concept__domain_id'],
                    'omop_table': row['omop_table'], 'field_name': field, 'tier': tier,
                })
        for concept_id in sorted(accepted.get(code, ())):
            concept = curated.get(concept_id)
            if concept is None or concept_id in seen:
                continue
            entries.append({
                'concept_id': concept_id, 'name': concept['concept_name'],
                'vocabulary_id': concept['vocabulary_id'], 'concept_code': concept['concept_code'],
                'domain_id': concept['domain_id'], 'omop_table': None, 'field_name': None,
                'tier': 'curated',
            })
        sets[code] = {
            'concepts': entries,
            # Fields that belong to the set but have no approved concept yet:
            # how far curation still has to go, not an error.
            'unmapped_fields': sorted(f for f in specific if f not in mappings),
        }
    digest = hashlib.sha256(json.dumps(sets, sort_keys=True, default=str).encode()).hexdigest()[:12]
    version = f"{CATALOG_VERSION}.{genomics_catalog()['version']}.{digest}"
    return {'version': version, 'sets': sets}


def current_version() -> str:
    return _build()['version']


def relevance(codes) -> dict:
    """The union of the given diseases' sets, each concept listing its diseases.

    A concept in several sets appears once, with the strongest tier it holds
    (disease, then curated, then common).
    """
    built = _build()
    rank = {'disease': 0, 'curated': 1, 'common': 2}
    merged: dict[tuple, dict] = {}
    for code in codes:
        for entry in built['sets'][code]['concepts']:
            key = (entry['concept_id'], entry['field_name'])
            existing = merged.get(key)
            if existing is None:
                merged[key] = {**entry, 'diseases': [code]}
                continue
            existing['diseases'].append(code)
            if rank[entry['tier']] < rank[existing['tier']]:
                existing['tier'] = entry['tier']
    concepts = sorted(merged.values(), key=lambda e: (rank[e['tier']], e['name'] or '', e['field_name'] or ''))
    synonyms = _synonyms({e['concept_id'] for e in concepts})
    for entry in concepts:
        entry['synonyms'] = synonyms.get(entry['concept_id'], [])
    return {
        'version': built['version'],
        'diseases': list(codes),
        'concepts': concepts,
        'unmapped_fields': {code: built['sets'][code]['unmapped_fields'] for code in codes},
    }


def patient_diseases(person) -> list[str]:
    """Every supported cancer the record names, primary first.

    ``PatientRecord.disease`` is the primary cancer; any other diagnosis on the
    record that is a supported cancer follows it, sorted. A patient with more
    than one cancer is served the union of the sets (see ``relevance``).
    """
    from omop_core.models import PatientRecord
    from omop_core.services.disease_episodes import disease_keys_on_record

    record = PatientRecord.objects.filter(person=person).only('disease').first()
    primary = disease_code(record.disease) if record else None
    others = {disease_code(key) for key in disease_keys_on_record(person)}
    others = sorted(c for c in others if supported(c) and c != primary)
    return ([primary] if supported(primary) else []) + others
