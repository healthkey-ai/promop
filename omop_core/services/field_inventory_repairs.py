"""Reviewable #21 repair contracts, not executable or approved mapping recipes.

Portable candidate codes come from the plan's verified candidate table. The
export resolves them against its reference snapshot and retains missing targets.
No local concept ID, display-label alias or inferred clinical approval is seeded.
"""
from copy import deepcopy


def repair_contracts():
    contracts = {}

    def add(fields, representation, meaning, *, questions=(), context=(), forbidden=()):
        for field in fields.split():
            contracts[field] = {
                'parent_issue': '#21', 'implementation_owner': '#1227',
                'representation': representation, 'meaning': meaning,
                'required_context': list(context),
                'question_candidates': [dict(vocabulary_id=vocabulary, concept_code=code,
                                             context=scope) for vocabulary, code, scope in questions],
                'excluded_question_codes': [dict(vocabulary_id=vocabulary, concept_code=code)
                                            for vocabulary, code in forbidden],
                'clinical_approval': False,
            }

    for field, clinical, pathological in (
        ('tumor_stage', '21905-5', '21899-0'),
        ('nodes_stage', '21906-3', '21900-6'),
        ('distant_metastasis_stage', '21907-1', '21901-4'),
    ):
        add(field, 'scoped_question_and_answer',
            'Retain clinical and pathological question/answer pairs across supported source tables. '
            'Select the latest applicable clinical event, not the preferred table. '
            'Post-neoadjuvant pathological basis remains distinct and requires its own validated context.',
            questions=[('LOINC', clinical, {'basis': 'c'}), ('LOINC', pathological, {'basis': 'p'})],
            context=['tumor', 'disease', 'system', 'edition', 'basis', 'clinical_date'])
    add('staging_modalities', 'assessment_context',
        'The stored c/p/yp choice is staging basis. CT/MRI/PET belong to linked imaging procedures; '
        'neither stage text nor an imaging name is an alias for this choice.',
        context=['tumor', 'system', 'edition', 'basis', 'assessment_event'])
    add('metastatic_status metastasis_status', 'canonical_assessment_and_alias',
        'Use one scoped extent assessment and compatible legacy aliases. Reconcile explicit distant '
        'metastasis and M-stage evidence; unknown, MX and missing evidence must not become false. '
        'Define conflict handling before projecting a derived status.',
        context=['tumor', 'assessment_event', 'basis', 'evidence_polarity', 'derived_provenance'])
    add('lymph_node_status', 'scoped_nodal_assessment',
        'Use N-stage or explicit nodal involvement with unknown/conflict preservation. '
        'Perineural invasion is not nodal involvement.',
        forbidden=[('LOINC', '92837-4')],
        context=['tumor', 'assessment_event', 'evidence_polarity'])
    add('bone_only_metastasis_status', 'scoped_extent_assessment',
        'Require an explicit bone-only result or a complete scoped extent assessment. '
        'Bone metastasis alone cannot establish the absence of other metastatic sites. '
        'LA4202-3 is an answer describing distant recurrence in bone only, not a generic question.',
        forbidden=[('LOINC', 'LA4202-3')], context=['tumor', 'assessment_event', 'extent_completeness'])
    add('tumor_size', 'numeric_measurement',
        'Retain source units and normalize only compatible units for the same lesion and event.',
        questions=[('LOINC', '21889-1', {})], context=['lesion', 'site', 'clinical_date', 'unit'])
    add('histologic_type', 'question_and_answer_or_occurrence',
        'Retain histology, site and behavior. An occurrence representation requires an explicit '
        'recipe; an answer concept cannot silently become a diagnosis occurrence.',
        questions=[('LOINC', '59847-4', {})], context=['tumor', 'site', 'behavior', 'pathology_event'])
    add('biopsy_grade', 'question_and_answer',
        'Retain Nottingham grade versus numeric score and disease-specific grading system. '
        'Legacy biopsy fields remain compatibility aliases, not duplicate assessments.',
        questions=[('LOINC', '44648-4', {})], context=['tumor', 'grading_system', 'pathology_event'])
    add('menopausal_status', 'question_and_answer',
        'Use explicitly reviewed answers; broad menopause-related concept-name matches are insufficient.',
        questions=[('SNOMED', '276477006', {})], context=['assessment_event', 'clinical_date'])
    add('ki67_proliferation_index', 'numeric_measurement',
        'Use the Ki-67 percentage question and compatible units. A HER2 result must never populate Ki-67.',
        questions=[('LOINC', '29593-1', {})], forbidden=[('LOINC', '85319-2')],
        context=['tumor', 'test_event', 'specimen', 'unit'])
    add('hr_status', 'explicit_or_derived_assessment',
        'Keep explicit HR results distinct from the existing ER/PR-derived result. '
        'Preserve unknown/equivocal states, conflicting tests and derivation provenance. '
        'Positive/negative alone cannot establish low/high expression.',
        questions=[('SNOMED', '310871000000100', {})],
        context=['tumor', 'test_event', 'clinical_date', 'derived_provenance'])
    add('hrd_status', 'question_and_answer',
        'HRD is a Measurement assay result. Do not compute it from HR status or breast phenotype.',
        questions=[('LOINC', '107286-7', {})], context=['tumor', 'test_event', 'assay'])
    add('ecog_assessment_date test_date', 'linked_event_date',
        'Read the date of the corresponding selected ECOG/test event. Do not create an independent '
        'date assertion or pair unrelated latest results and dates.', context=['selected_event'])
    add('test_methodology', 'linked_test_metadata',
        'Retain method as metadata for the selected test/report. The ER assay code is not a method question.',
        questions=[('LOINC', '85069-3', {})], forbidden=[('LOINC', '85337-4')],
        context=['test_event', 'report', 'specimen'])
    add('test_specimen_type', 'linked_test_metadata',
        'Link the specimen answer to the same selected test/report; retain specimen identity.',
        questions=[('LOINC', '31208-2', {})], context=['test_event', 'report', 'specimen'])
    add('oncotype_dx_score', 'scoped_numeric_measurement',
        'Select the numeric score question by invasive/DCIS test indication. '
        'An assay answer or numeric ER result is not an Oncotype score.',
        questions=[('NAACCR', '3904', {'indication': 'invasive'}),
                   ('NAACCR', '3903', {'indication': 'DCIS'})],
        forbidden=[('LOINC', '85337-4'), ('NAACCR', 'breast@2876@010')],
        context=['tumor', 'test_event', 'indication'])
    add('androgen_receptor_status', 'question_and_answer',
        'Preserve positive, negative, equivocal, explicit unknown and cleared values independently.',
        questions=[('LOINC', '49457-5', {})], context=['tumor', 'test_event', 'assay'])
    return contracts


def attach_repair_contracts(rows):
    """Attach field requirements without replacing saved mapping decisions."""
    contracts = repair_contracts()
    for row in rows:
        if row['source'] == 'promop_field' and row['destination_path'] in contracts:
            row['repair_contract'] = deepcopy(contracts[row['destination_path']])


def question_evidence_requests(row):
    """Yield exact-code evidence requests with explicit origin and context."""
    recipe = row.get('recipe') or {}
    vocabulary = {'loinc': 'LOINC', 'snomed': 'SNOMED'}.get(recipe.get('lookup_strategy'))
    if vocabulary:
        for code in recipe.get('concept_codes') or []:
            yield {'method': 'read_recipe_exact_code', 'vocabulary_id': vocabulary,
                   'concept_code': code, 'context': {}}
    for question in row.get('repair_contract', {}).get('question_candidates', []):
        yield {'method': 'repair_plan_exact_code', **question}


def record_question_evidence(row, candidates, by_pair):
    """Record missing targets and known wrong question usage without approving replacements."""
    excluded = {(p['vocabulary_id'], p['concept_code'])
                for p in row.get('repair_contract', {}).get('excluded_question_codes', [])}
    existing_pairs = {(m.get('vocabulary_id'), m.get('concept_code') or m.get('code'))
                      for m in row['existing_mappings']}
    for request in question_evidence_requests(row):
        pair = (request['vocabulary_id'], request['concept_code'])
        ids = sorted(by_pair.get(pair, []))
        row['candidate_ids'].extend(ids)
        row['search_evidence'].append({
            'method': request['method'], 'vocabulary_code': list(pair),
            'context': request['context'], 'role': 'question', 'candidate_ids': ids,
            'resolution': 'missing' if not ids else 'resolved' if len(ids) == 1 else 'ambiguous',
            'clinical_approval': False,
        })
        if request['method'] == 'read_recipe_exact_code':
            if pair in excluded:
                row['validation_flags'].append('read_recipe_uses_excluded_question_code')
            if not ids:
                row['validation_flags'].append('read_recipe_question_missing')
            if any(not candidates[i]['passes_mechanical_screen'] for i in ids):
                row['validation_flags'].append('read_recipe_candidate_fails_mechanical_screen')
    if excluded & existing_pairs:
        row['validation_flags'].append('existing_mapping_uses_excluded_question_code')
    row['candidate_ids'] = sorted(set(row['candidate_ids']))
    row['validation_flags'] = sorted(set(row['validation_flags']))
