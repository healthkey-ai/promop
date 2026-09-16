"""Source routing evidence, distinct from clinical equivalence and approval.

Only reviewed public binding names are routed. Source hashes and the exported
destination schema must agree; no route is inferred from a display label.
"""
import hashlib
from collections import Counter
from pathlib import Path


SOURCES = {
    'trials/services/value_options.py': 'a764aa582578a03648f1ba95b3feba5e67a935562867f0f6641c8c9791b95e2a',
    'trials/services/patient_info/patient_info_details.py': 'edd09b562b2d5cc3f6e715b5c767bf31e79a497f06e3fe6ed6f7a2d664bf5917',
    'trials/api/patient_info_serializers.py': '7dadd99a7cbe07d52ac51adb59ebbbdf71a554dd161ce4398c9652f4020726ad',
}
DISEASES = {'Mm': 'MM', 'Fl': 'FL', 'Bc': 'BC', 'Cll': 'CLL', 'Mcl': 'MCL'}


def reviewed_routes():
    routes = {}

    def add(names, paths, reason, *, owner='#1228', representation='scalar', **context):
        if isinstance(paths, str):
            paths = [paths]
        for name in names.split():
            if name in routes:
                raise ValueError(f'Duplicate reviewed route: {name}')
            routes[name] = {'destination_candidates': paths, 'context': context,
                            'representation': representation, 'reason': reason, 'owning_issue': owner}

    scalar = {
        'tumorGrade': 'tumor_grade', 'peripheralNeuropathyGrade': 'peripheral_neuropathy_grade',
        'progression': 'progression', 'disease': 'disease', 'gender': 'gender',
        'ecogPerformanceStatus': 'ecog_performance_status', 'karnofskyPerformanceScore': 'karnofsky_performance_score',
        'treatmentRefractoryStatus': 'treatment_refractory_status', 'menopausalStatus': 'menopausal_status',
        'histologicType': 'histologic_type', 'biopsyGrade': 'biopsy_grade',
        'pdL1Assay': 'pd_l1_assay', 'her2Status': 'her2_status', 'hrdStatus': 'hrd_status',
        'hrStatus': 'hr_status', 'estrogenReceptorStatus': 'estrogen_receptor_status',
        'progesteroneReceptorStatus': 'progesterone_receptor_status', 'toxicityGrade': 'toxicity_grade',
    }
    for name, field in scalar.items():
        owner = '#1227' if name in {
            'menopausalStatus', 'histologicType', 'biopsyGrade', 'pdL1Assay', 'her2Status',
            'hrdStatus', 'hrStatus', 'estrogenReceptorStatus', 'progesteroneReceptorStatus',
        } else '#1228'
        add(name, field, 'Reviewed source field route; codes, polarity, thresholds and answer concepts still require review.', owner=owner)
    add('ethnicity', ['ethnicity', 'race'],
        'CancerBot ancestry/race-like categories and the PRomop legacy Ethnicity catalog conflict with the Hispanic/Latino ethnicity picker. '
        'Preserve Person race/ethnicity ownership and source categories; no cross-field alias is approved.', representation='semantic_conflict')
    add('priorTherapy', ['prior_therapy', 'therapy_lines_count'],
        'Source line-count categories drive planned eligibility. Preserve line history and derived prior-therapy status; '
        'a selected category alone does not establish a dated administration.', owner='#1230', representation='structured')
    add('allCountries', 'country', 'Administrative country selection; preserve Location ownership.',
        owner='#1223', representation='administrative')
    add('statuses register studyType trialPurpose trialType phases', [],
        'Trial search or recruitment metadata; no patient clinical answer destination.',
        owner='#1223', representation='trial_metadata')
    add('positiveNegative', [], 'Shared polarity provider; a question/marker-specific consumer is required.',
        representation='context_required')
    add('stemCellTransplantHistory stemCellTransplantHistoryExcluded', 'stem_cell_transplant_history',
        'Completed transplant history, exclusion criteria and eligibility are distinct assertions; retain source consumer.',
        representation='structured')
    add('boneLesions', 'bone_lesions', 'Source count 1/2/>2 is not equivalent to PRomop presence Yes/No.',
        representation='semantic_conflict', disease='MM')
    add('preExistingConditionCategories upreExistingConditionCategories', 'preexisting_conditions',
        'Eligibility categories require category/concept-set representation, not fabricated diagnosis occurrences.',
        representation='structured')
    add('languagesSkills', 'languages_skills',
        'CancerBot language/fluency pairs differ from PersonLanguageSkill speak/read/write/understand capabilities.',
        representation='semantic_conflict')
    for name, field in [('tumorStages', 'tumor_stage'), ('nodesStages', 'nodes_stage'),
                        ('distantMetastasisStages', 'distant_metastasis_stage')]:
        add(name, field, 'Retain TNM axis, tumor, edition and c/p/yp basis; the list alone does not establish these.',
            owner='#1227', representation='structured', disease='BC', system='TNM', edition=None, basis=None)
    add('stagingModalities', 'staging_modalities', 'Imaging method is a separate event, not TNM c/p/yp basis.',
        owner='#1227', representation='structured', disease='BC')
    genetic = {
        'geneticMutationGenes': 'gene', 'geneticMutationVariants': 'variant_name',
        'geneticMutationAllVariants': 'variant_name', 'geneticMutationOrigins': 'origin',
        'geneticMutationOriginsPerGene': 'origin', 'geneticMutationAllOrigins': 'origin',
        'geneticMutationInterpretations': 'interpretation', 'geneticMutationAllInterpretations': 'interpretation',
    }
    for name, part in genetic.items():
        add(name, 'genetic_mutations.' + part,
            'Use Genomics finding/components; retain nested parent gene, variant and origin keys. No label-only variant equivalence.',
            owner='#1229', representation='structured')
    families = {
        'flipiScore': ('flipi_score_options', 'semantic_conflict', 'Five selected inputs are distinct from numeric score/risk; review age threshold >60 versus legacy >=60.'),
        'gelfCriteriaStatus': ('gelf_criteria_options', 'semantic_conflict', 'CancerBot seven criteria differ from PRomop eight: mass, spleen and cytopenia thresholds need review; aggregate is separate.'),
        'cytogenicMarkers': ('cytogenetic_markers', 'structured', 'Compatibility summary is owned by Genomics findings; preserve marker identity, polarity and independent events.'),
        'molecularMarkers': ('molecular_markers', 'structured', 'Compatibility summary is owned by Genomics findings; preserve marker identity, polarity and independent events.'),
        'binetStages': ('binet_stage', 'scalar', 'Binet A/B/C remains distinct from Rai and generic legacy stage.'),
        'richterTransformations': ('richter_transformation', 'structured', 'Transformation type, polarity and transformation date remain separate.'),
        'tumorBurdens': ('tumor_burden', 'structured', 'Retain disease-specific criteria and measurements supporting tumor burden.'),
        'diseaseActivities': ('disease_activity', 'structured', 'Retain disease and treatment-indication criteria; similar activity labels do not establish equivalence.'),
    }
    for base, (field, representation, reason) in families.items():
        for suffix in ['', *DISEASES]:
            add(base + suffix, field, reason, representation=representation,
                owner='#1229' if 'Markers' in base else '#1228', disease=DISEASES.get(suffix),
                source_applicability='union' if not suffix else 'provider_subset_including_empty')
    for suffix in ['', *DISEASES]:
        add('therapyOutcome' + suffix, ['first_line_outcome', 'second_line_outcome', 'later_outcome'],
            'Outcome belongs to its line/event and disease response system; MRD is not interchangeable with response category.',
            representation='structured', disease=DISEASES.get(suffix), response_system=None)
    for suffix, disease in DISEASES.items():
        add('stages' + suffix, 'stage', 'Legacy stage labels do not establish a staging system, edition or basis.',
            representation='context_required', disease=disease, system=None, edition=None, basis=None)
        add('plannedTherapies' + suffix, 'planned_therapies',
            'Separate CancerBot PlannedTherapy catalog and disease eligibility; this is not TherapyRegimen administration.',
            owner='#1230', representation='structured', disease=disease, treatment_status='planned', line=None)
        for line, field in [('FirstLine', 'first_line_therapy'), ('SecondLine', 'second_line_therapy'), ('LaterLine', 'later_therapies')]:
            add('therapies' + line + suffix, field,
                'Reuse TherapyRegimen, component/class relationships and line authoring. Selection alone supplies no administration date.',
                owner='#1230', representation='structured', disease=disease, line=line)
            add('plannedTherapies' + line + suffix, 'planned_therapies',
                'Source prior-therapy rule: None/blank -> FirstLine, One line -> SecondLine, otherwise LaterLine. Eligibility is not administration.',
                owner='#1230', representation='structured', disease=disease, line=line, treatment_status='planned')
        add('supportiveTherapies' + suffix, 'supportive_therapies',
            'Reuse managed regimen/component/class graph; retain supportive intent and event dates.',
            owner='#1230', representation='structured', disease=disease, treatment_intent='supportive')
    for base, table, suffixes in [('therapies', 'therapy_regimen', ['All', *DISEASES]),
                                   ('therapyComponents', 'therapy_component', ['All', 'Mm', 'Fl', 'Bc']),
                                   ('therapyTypes', 'therapy_class', ['All', 'Mm', 'Fl', 'Bc'])]:
        for suffix in suffixes:
            add(base + suffix, 'reference_tables.' + table,
                'Existing managed reference catalog; preserve code identity and relationships. Does not create a patient fact.',
                owner='#1230', representation='catalog', disease=DISEASES.get(suffix))
    for suffix in ['Mm', 'Fl', 'Bc']:
        add('concomitantMedications' + suffix, 'concomitant_medications',
            'Keep source medication catalog and disease eligibility separate from regimen classification.',
            owner='#1230', representation='structured', disease=DISEASES[suffix])
    add('proteinExpressions proteinExpressionsMcl', 'protein_expressions',
        'Each marker and its polarity is an independent assertion, not a generic positive answer.', representation='structured')
    for name, field in {'morphologicVariants': 'morphologic_variant', 'diseaseBehaviorsMcl': 'disease_behavior',
                        'diseaseSubtypesMcl': 'disease_subtype', 'extranodalSites': 'extranodal_sites',
                        'mipiRisks': 'mipi_risk', 'mipiCRisks': 'mipi_c_risk',
                        'bulkyDiseaseCriteria': 'bulky_disease_criteria', 'highRiskMclCriteria': 'high_risk_mcl_criteria'}.items():
        add(name, field, 'Retain MCL criteria/source inputs; report missing PRomop destination rather than inventing a scalar mapping.',
            representation='structured', disease='MCL', field_parity_issues=['#468', '#542', '#1149'])
    return routes


def reconcile_cancerbot_destinations(root, bindings, rows, tables):
    """Annotate fresh inventory rows without changing immutable source identity."""
    for row in rows:
        row.pop('destination_route_keys', None)
    root = Path(root) if root else None
    changed = [p for p, digest in SOURCES.items() if root is None or not (root / p).is_file()
               or hashlib.sha256((root / p).read_bytes()).hexdigest() != digest]
    if changed:
        return {'status': 'source_unavailable_or_changed', 'changed_files': changed, 'bindings': []}
    fields = {r['destination_path'] for r in rows if r['source'] == 'promop_field'}
    fields |= {'reference_tables.' + name for name in tables}
    by_id = {r['id']: r for r in rows}
    routes, records = reviewed_routes(), []
    for binding in bindings:
        name = binding['option_list']
        rule = routes.get(name)
        record = {'option_list': name, 'status': 'unreviewed_binding'}
        if rule:
            record.update(rule)
            missing = [field for field in rule['destination_candidates'] if field not in fields]
            record.update(status='destination_missing' if missing else 'source_route_recorded', missing_destinations=missing)
            row_ids = set(binding.get('source_row_ids', []))
            row_ids.update(r['id'] for r in rows if r['source'] in {'cancerbot_live', 'cancerbot_static'} and r['option_list'] == name)
            record['source_row_ids'] = sorted(row_ids)
            for row_id in row_ids:
                row = by_id.get(row_id)
                if row is not None:
                    row.setdefault('destination_route_keys', []).append(name)
        records.append(record)
    return {'status': 'source_routes_recorded', 'source_files': SOURCES, 'bindings': records,
            'counts': dict(sorted(Counter(r['status'] for r in records).items())),
            'limitation': 'Source routes are review evidence. They do not alter canonical values, scope, dispositions or mapping approvals.'}
