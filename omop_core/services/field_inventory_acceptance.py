"""Issue-scope traceability and evidence checks for the inventory prerequisite.

These checks establish reviewable accounting, not clinical approval or runtime
completion. Missing implementations stay assigned to their delivery issues.
"""
from collections import Counter
from fnmatch import fnmatchcase

from omop_core.services.field_inventory import validate_manifest
from omop_core.services.field_inventory_repairs import repair_contracts, question_evidence_requests


# Explicit issue-scope selectors. Wildcards select source families, never labels
# or clinical equivalence. Each required pattern must match a captured binding.
SCOPES = [
    ('MM stage/progression', 'stage r_iss_stage progression', 'stagesMm progression', '#1228'),
    ('MM bone lesions', 'bone_lesions', 'boneLesions', '#1228'),
    ('Transplant history/eligibility', 'stem_cell_transplant_history sct_eligibility sct_date', 'stemCellTransplantHistory*', '#1228'),
    ('Treatment response', 'first_line_outcome second_line_outcome later_outcome', 'therapyOutcome*', '#1228'),
    ('FL stage/grade', 'stage tumor_grade', 'stagesFl tumorGrade', '#1228'),
    ('FLIPI inputs/score/risk', 'flipi_score_options flipi_score flipi_risk_category', 'flipiScore*', '#1228'),
    ('GELF criteria/aggregate', 'gelf_criteria_options gelf_criteria_status', 'gelfCriteriaStatus*', '#1228'),
    ('Cytogenetic/molecular markers', 'cytogenetic_markers molecular_markers', 'cytogenicMarkers* molecularMarkers*', '#1229'),
    ('BC pathology/TNM/basis', 'histologic_type biopsy_grade tumor_stage nodes_stage distant_metastasis_stage staging_modalities',
     'histologicType biopsyGrade tumorStages nodesStages distantMetastasisStages stagingModalities', '#1227'),
    ('BC receptors/HR/HRD', 'estrogen_receptor_status progesterone_receptor_status her2_status androgen_receptor_status hr_status hrd_status',
     'estrogenReceptorStatus progesteroneReceptorStatus her2Status hrStatus hrdStatus', '#1227'),
    ('BC PD-L1 assay/scores', 'pd_l1_assay pd_l1_tumor_cells pd_l1_combined_positive_score pd_l1_ic_percentage', 'pdL1Assay', '#1227'),
    ('Menopause', 'menopausal_status', 'menopausalStatus', '#1227'),
    ('Nested genetic catalogs', 'genetic_mutations', 'geneticMutation*', '#1229'),
    ('CLL Rai/Binet', 'stage binet_stage', 'stagesCll binetStages*', '#1228'),
    ('Protein expression/polarity', 'protein_expressions', 'proteinExpressions*', '#1229'),
    ('Richter transformation', 'richter_transformation', 'richterTransformations*', '#1228'),
    ('Burden/morphology/activity', 'tumor_burden disease_activity', 'tumorBurdens* morphologicVariants diseaseActivities*', '#1228'),
    ('Managed therapy catalogs/lines', 'first_line_therapy second_line_therapy later_therapies supportive_therapies planned_therapies concomitant_medications',
     'therapies* therapyComponents* therapyTypes* supportiveTherapies* plannedTherapies* concomitantMedications* priorTherapy', '#1230'),
    ('Pre-existing categories', 'preexisting_conditions', '*preExistingConditionCategories', '#1228'),
    ('Neuropathy/toxicity/polarity', 'peripheral_neuropathy_grade toxicity_grade', 'peripheralNeuropathyGrade toxicityGrade positiveNegative', '#1228'),
    ('Demographic/language/admin lookups', 'ethnicity race languages_skills country', 'ethnicity languagesSkills allCountries', '#1228'),
    ('MCL parity', '', 'stagesMcl *Mcl morphologicVariants bulkyDiseaseCriteria highRiskMclCriteria extranodalSites mipiRisks mipiCRisks', '#1228'),
]
DISPOSITIONS = {'needs_review', 'ambiguous', 'no_equivalent', 'not_applicable',
                'requires_structured_representation', 'verified_mapping'}


def inventory_acceptance(manifest):
    """Recompute from evidence, never trust a saved acceptance summary."""
    validate_manifest(manifest)
    rows = manifest['rows']
    by_id = {r['id']: r for r in rows}
    fields = {r['destination_path']: r for r in rows if r['source'] == 'promop_field'}
    bindings = {b['option_list']: b for b in manifest.get('cancerbot_bindings', [])}
    routes = {r['option_list']: r for r in manifest.get('destination_crosswalk', {}).get('bindings', [])}
    problems = []
    repair_records = []
    for field, contract in repair_contracts().items():
        row = fields.get(field)
        if row is None:
            problems.append(f'#21 field missing: {field}')
            continue
        if row.get('repair_contract') != contract:
            problems.append(f'#21 repair contract missing or stale: {field}')
        for request in question_evidence_requests(row):
            evidence = [e for e in row['search_evidence']
                        if e.get('method') == request['method']
                        and e.get('vocabulary_code') == [request['vocabulary_id'], request['concept_code']]
                        and e.get('context') == request['context']]
            if len(evidence) != 1:
                problems.append(f'#21 exact question evidence missing or duplicated: {field}')
                continue
            evidence = evidence[0]
            ids = evidence.get('candidate_ids', [])
            if evidence.get('resolution') != ('missing' if not ids else 'resolved' if len(ids) == 1 else 'ambiguous'):
                problems.append(f'#21 question resolution disagrees with candidates: {field}')
            for key in ids:
                candidate = manifest['candidates'].get(str(key), {})
                if (key not in row['candidate_ids'] or candidate.get('vocabulary_id') != request['vocabulary_id']
                        or candidate.get('concept_code') != request['concept_code']):
                    problems.append(f'#21 exact question candidate mismatch: {field}')
        repair_records.append({'field': field, 'row_id': row['id'],
                               'representation': contract['representation'], 'owner': '#1227',
                               'disposition': row['disposition'], 'validation_flags': row['validation_flags'],
                               'candidate_ids': row['candidate_ids']})
    scope_records = []
    for name, field_names, patterns, owner in SCOPES:
        names = sorted({b for pattern in patterns.split() for b in bindings if fnmatchcase(b, pattern)})
        for pattern in patterns.split():
            if not any(fnmatchcase(b, pattern) for b in bindings):
                problems.append(f'#26 source family missing: {name}: {pattern}')
        expected_fields = field_names.split()
        for field in expected_fields:
            if field not in fields:
                problems.append(f'#26 field evidence missing: {name}: {field}')
        source_ids = {r['id'] for r in rows if r['destination_path'] in expected_fields}
        for binding in names:
            source_ids.update(bindings[binding].get('source_row_ids', []))
            source_ids.update(bindings[binding].get('live_source_row_ids', []))
            if binding not in routes:
                problems.append(f'#26 destination route missing: {binding}')
            source_ids.update(routes.get(binding, {}).get('source_row_ids', []))
        missing = sorted({f for b in names for f in routes.get(b, {}).get('missing_destinations', [])})
        if source_ids - by_id.keys():
            problems.append(f'#26 scope references missing rows: {name}')
        scope_records.append({'scope': name, 'owner': owner, 'fields': expected_fields,
                              'source_bindings': names, 'source_row_ids': sorted(source_ids),
                              'missing_runtime_destinations': missing,
                              'dispositions': dict(sorted(Counter(by_id[i]['disposition'] for i in source_ids if i in by_id).items()))})
    for row in rows:
        if row['disposition'] not in DISPOSITIONS or not row.get('reason') or not row.get('owning_issue'):
            problems.append(f'Missing explicit disposition/owner: {row["id"]}')
        contract = row.get('implementation_contract', {})
        if not contract.get('implementation_owners') or contract.get('status') in {None, 'destination_unresolved'}:
            problems.append(f'Missing implementation routing: {row["id"]}')
    if set(bindings) != set(routes):
        problems.append('Public bindings and reviewed destination routes differ.')
    if any(r.get('status') == 'unreviewed_binding' or not r.get('reason') or not r.get('owning_issue') for r in routes.values()):
        problems.append('A public binding has no reviewed routing disposition.')
    if manifest.get('destination_crosswalk', {}).get('status') != 'source_routes_recorded':
        problems.append('Source revision routing requires review.')
    sources = manifest['totals']['source_coverage']
    for key, expected in [('cancerbot_public_lists', 'source_membership_accounted_for'),
                          ('frontend', 'source_providers_accounted_for')]:
        if sources.get(key, {}).get('coverage') != expected:
            problems.append(f'Source coverage incomplete: {key}')
    if not manifest.get('source_history', {}).get('definition_coverage_complete'):
        problems.append('Migration definition accounting incomplete.')
    return {'schema_version': 1, 'accounting_checks_pass': not problems, 'problems': sorted(set(problems)),
            'review_status': 'pending_semantic_acceptance', 'clinical_approval': False,
            'issue_21': repair_records, 'issue_26': scope_records,
            'limitation': 'Scope counts overlap; source rows retain their identities and dispositions. '
                          'Accounting checks do not approve concepts, establish runtime completion or close issues.'}
