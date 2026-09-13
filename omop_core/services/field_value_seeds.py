"""Versioned candidates verified by exact code against staging Athena.

These are review proposals, never automatic clinical approvals. Numeric IDs
are evidence, not portable foreign keys. Runtime resolves vocabulary + code.
"""
RELEASE = 'Athena v5.0 29-AUG-26; staging checked 2026-09-13'
QUESTIONS = [
    ('tumor_stage', 'LOINC', '21905-5', 3008841, 'string', ''),
    ('nodes_stage', 'LOINC', '21906-3', 3007727, 'string', ''),
    ('distant_metastasis_stage', 'LOINC', '21907-1', 3006575, 'string', ''),
    ('histologic_type', 'LOINC', '59847-4', 40762908, 'string', ''),
    ('biopsy_grade', 'LOINC', '44648-4', 3047285, 'number', ''),
    ('ki67_proliferation_index', 'LOINC', '29593-1', 3018217, 'number', '%'),
    ('tumor_size', 'LOINC', '21889-1', 3018102, 'number', 'cm'),
    ('hrd_status', 'LOINC', '107286-7', 1469934, 'string', ''),
    ('androgen_receptor_status', 'LOINC', '49457-5', 3032783, 'string', ''),
    ('test_methodology', 'LOINC', '85069-3', 42527891, 'string', ''),
    ('test_specimen_type', 'LOINC', '31208-2', 3015746, 'string', ''),
    ('oncotype_dx_score', 'NAACCR', '3904', 35918544, 'number', ''),
    ('pd_l1_tumor_cells', 'LOINC', '105304-0', 1092047, 'number', '%'),
    ('pd_l1_ic_percentage', 'LOINC', '105305-7', 1092026, 'number', '%'),
    ('pd_l1_combined_positive_score', 'LOINC', '105303-2', 1091860, 'number', ''),
    ('menopausal_status', 'SNOMED', '276477006', 4172857, 'string', ''),
    ('hr_status', 'SNOMED', '310871000000100', 44791967, 'string', ''),
    ('r_iss_stage', 'SNOMED', '1149163003', 607126, 'string', ''),
    ('binet_stage', 'SNOMED', '1149099005', 607090, 'string', ''),
    ('flipi_score', 'NAACCR', 'lymphoma@2910', 35917496, 'number', ''),
]
TNM = {
    'tumor_stage': [('TX', 'LA3601-7'), ('T0', 'LA3638-9'), ('Tis', 'LA3608-2'),
        ('T1', 'LA3637-1'), ('T1mi', 'LA21854-7'), ('T1a', 'LA3636-3'), ('T1b', 'LA3633-0'),
        ('T1c', 'LA3630-6'), ('T2', 'LA3628-0'), ('T3', 'LA3624-9'), ('T4', 'LA3620-7'),
        ('T4a', 'LA3619-9'), ('T4b', 'LA3618-1'), ('T4c', 'LA3617-3'), ('T4d', 'LA3616-5')],
    'nodes_stage': [('NX', 'LA4745-1'), ('N0', 'LA4368-2'), ('N1', 'LA4537-2'),
        ('N1mi', 'LA21822-4'), ('N1a', 'LA4264-3'), ('N1b', 'LA4535-6'), ('N1c', 'LA21866-1'),
        ('N2', 'LA4534-9'), ('N2a', 'LA4533-1'), ('N2b', 'LA4517-4'), ('N3', 'LA4545-5'),
        ('N3a', 'LA4529-9'), ('N3b', 'LA4528-1'), ('N3c', 'LA4527-3')],
    'distant_metastasis_stage': [('M0', 'LA4629-7'), ('M1', 'LA4628-9'), ('M0(i+)', 'LA21876-0')],
}
LOOKUPS = {
    'HistologicType': 'histologic_type', 'EstrogenReceptorStatus': 'estrogen_receptor_status',
    'ProgesteroneReceptorStatus': 'progesterone_receptor_status', 'Her2Status': 'her2_status',
    'HrStatus': 'hr_status', 'HrdStatus': 'hrd_status', 'TumorStage': 'tumor_stage',
    'NodesStage': 'nodes_stage', 'DistantMetastasisStage': 'distant_metastasis_stage',
    'StagingModality': 'staging_modalities', 'StemCellTransplant': 'stem_cell_transplant',
    'SctEligibility': 'sct_eligibility', 'BinetStage': 'binet_stage',
    'ProteinExpression': 'protein_expressions', 'RichterTransformation': 'richter_transformation',
    'TumorBurden': 'tumor_burden', 'DiseaseActivity': 'disease_activity',
    'FollicularLymphomaGrade': 'tumor_grade', 'GelfCriteria': 'gelf_criteria_status',
    'DiseaseProgression': 'progression', 'PeripheralNeuropathyGrade': 'peripheral_neuropathy_grade',
    'MutationGene': 'genetic_mutations.gene', 'MutationOrigin': 'genetic_mutations.origin',
    'MutationInterpretation': 'genetic_mutations.interpretation',
}


def answer_candidates():
    for field in ('estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status', 'androgen_receptor_status', 'hrd_status'):
        for value, vocabulary, code in [('Positive', 'SNOMED', '10828004'), ('Negative', 'SNOMED', '260385009'),
                ('Equivocal', 'SNOMED', '42425007'), ('Unknown', 'LOINC', 'LA4489-6')]:
            yield field, '', value, vocabulary, code
    for field, choices in TNM.items():
        for value, code in choices:
            yield field, '', value, 'LOINC', code
    for value, code in [(1, 'LA27823-6'), (2, 'LA27824-4'), (3, 'LA27825-1')]:
        yield 'biopsy_grade', '', value, 'LOINC', code
    for field, choices in {
        'genetic_mutations.origin': [('Germline', 'LA6683-2'), ('Somatic', 'LA6684-0')],
        'genetic_mutations.interpretation': [('Pathogenic', 'LA6668-3'), ('Likely pathogenic', 'LA26332-9')],
    }.items():
        for value, code in choices:
            yield field, '', value, 'LOINC', code
