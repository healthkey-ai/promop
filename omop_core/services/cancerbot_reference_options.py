"""Reconstruct reference options from read-only rows and a reviewed source version.

This does not import CancerBot or assert which code version is deployed. The
source hash pins the provider semantics implemented below; snapshot rows supply
live membership. No mappings or therapy catalogs are written.
"""
import ast
import hashlib

from omop_core.services.cancerbot_static_options import StaticOptions, Unresolved

SOURCE_SHA256 = 'a764aa582578a03648f1ba95b3feba5e67a935562867f0f6641c8c9791b95e2a'

# Provider name -> reference model, whether the provider adds a blank sentinel.
SIMPLE = {
    'ethnicities': ('Ethnicity', False),
    'stem_cell_transplant_history_excluded': ('StemCellTransplant', False),
    'tumor_stages': ('TumorStage', False), 'nodes_stages': ('NodesStage', False),
    'distant_metastasis_stages': ('DistantMetastasisStage', False),
    'staging_modalities': ('StagingModality', False),
    'mutation_genes': ('MutationGene', True), 'mutation_variants': ('MutationCode', True),
    'mutation_origins': ('MutationOrigin', True), 'mutation_interpretations': ('MutationInterpretation', True),
    'her2_status': ('Her2Status', True), 'hrd_status': ('HrdStatus', True),
    'hr_status': ('HrStatus', True), 'er_statuses': ('EstrogenReceptorStatus', True),
    'pr_statuses': ('ProgesteroneReceptorStatus', True), 'toxicity_grade': ('ToxicityGrade', True),
    'binet_stages': ('BinetStage', True), 'protein_expressions': ('ProteinExpression', True),
    'richter_transformations': ('RichterTransformation', True), 'tumor_burdens': ('TumorBurden', True),
    'morphologic_variants': ('MorphologicVariant', True),
    'bulky_disease_criteria': ('BulkyDiseaseCriteria', False),
    'high_risk_mcl_criteria': ('HighRiskMclCriteria', False), 'extranodal_sites': ('ExtranodalSite', True),
}
REFERENCE_MODELS = sorted({model for model, _ in SIMPLE.values()} | {
    'PreferredCountry', 'HistologicType', 'CytogenicMarker', 'MolecularMarker',
    'Language', 'LanguageSkillLevel', 'PreExistingConditionCategory',
    'ConcomitantMedication', 'ConcomitantMedicationDisease', 'Disease', 'TherapyRound',
    'MutationGeneOriginConnection', 'PlannedTherapy', 'PlannedTherapyDiseaseConnection',
    'Therapy', 'TherapyComponent', 'TherapyComponentCategory',
    'TherapyComponentConnection', 'TherapyComponentCategoryConnection', 'DiseaseRoundTherapyConnection',
})
REFERENCE_COLUMNS = {
    'id', 'code', 'title', 'sort_key', 'gene_id', 'origin_id', 'disease_id',
    'round_id', 'planned_therapy_id', 'concomitant_medication_id',
    'therapy_id', 'component_id', 'category_id',
}
REFERENCE_FKS = {
    'MutationCode': {'gene_id': 'MutationGene'},
    'MutationGeneOriginConnection': {'gene_id': 'MutationGene', 'origin_id': 'MutationOrigin'},
    'ConcomitantMedicationDisease': {'concomitant_medication_id': 'ConcomitantMedication', 'disease_id': 'Disease'},
    'PlannedTherapyDiseaseConnection': {'planned_therapy_id': 'PlannedTherapy', 'disease_id': 'Disease', 'round_id': 'TherapyRound'},
    'TherapyComponentConnection': {'therapy_id': 'Therapy', 'component_id': 'TherapyComponent'},
    'TherapyComponentCategoryConnection': {'component_id': 'TherapyComponent', 'category_id': 'TherapyComponentCategory'},
    'DiseaseRoundTherapyConnection': {'therapy_id': 'Therapy', 'disease_id': 'Disease', 'round_id': 'TherapyRound'},
}


def validate_reference_tables(tables):
    if set(tables) != {'trials_' + name.lower() for name in REFERENCE_MODELS}:
        raise ValueError('Reference snapshot must contain exactly the allowlisted tables.')
    for model in REFERENCE_MODELS:
        rows = tables['trials_' + model.lower()]
        required = {'id'} | set(REFERENCE_FKS.get(model, {}))
        if model not in REFERENCE_FKS or model == 'MutationCode':
            required |= {'code', 'title'}
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not required <= set(r) or set(r) - REFERENCE_COLUMNS for r in rows):
            raise ValueError(f'Invalid reference columns for {model}.')
        ids = [r['id'] for r in rows]
        if any(type(pk) is not int for pk in ids) or len(set(ids)) != len(ids):
            raise ValueError(f'Duplicate or invalid reference IDs for {model}.')
        if 'code' in required:
            codes = [(type(r['code']).__name__, r['code']) for r in rows]
            if any(type(r['code']) not in (str, int) or not isinstance(r['title'], str) for r in rows):
                raise ValueError(f'Invalid reference code or title for {model}.')
            if len(set(codes)) != len(codes):
                raise ValueError(f'Duplicate reference codes for {model}.')
        for field, target in REFERENCE_FKS.get(model, {}).items():
            targets = {r['id'] for r in tables['trials_' + target.lower()]}
            if any(r[field] is not None and r[field] not in targets for r in rows):
                raise ValueError(f'Orphan reference relationship: {model}.{field}.')


class ReferenceOptions(StaticOptions):
    def __init__(self, source, tables):
        if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
            raise ValueError('CancerBot source changed; review reference provider semantics before exporting.')
        super().__init__(source)
        self.tables = tables

    def rows(self, model):
        table = 'trials_' + model.lower()
        if model not in REFERENCE_MODELS or table not in self.tables:
            raise ValueError(f'Missing allowlisted reference table: {table}')
        return sorted(self.tables[table], key=lambda r: r['id'])

    @staticmethod
    def options(rows, blank=False):
        return {**({'': 'Unknown'} if blank else {}), **{r['code']: r['title'] for r in rows}}

    def public_options(self, expression):
        self.steps, self.dependencies, self.diseases = 0, set(), set()
        result = self.expr(ast.parse(expression, mode='eval').body, {})
        if not isinstance(result, dict) or set(result) != {'options'}:
            raise Unresolved('Expected an options envelope.')
        return result

    def expr(self, node, env):
        # One known keyword-taking reference provider; no general keyword calls.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'
                and node.func.attr == 'pre_existing_conditions' and node.keywords):
            if node.args or len(node.keywords) != 1 or node.keywords[0].arg != 'with_nothing':
                raise Unresolved('Unsupported pre-existing condition arguments.')
            return self.method('pre_existing_conditions', [self.expr(node.keywords[0].value, env)])
        return super().expr(node, env)

    def method(self, name, args):
        if name in self.methods:
            self.dependencies.add(name)
        if name in SIMPLE:
            model, blank = SIMPLE[name]
            return self.options(self.rows(model), blank)
        if name in {'therapies_all', 'therapy_components_all', 'therapy_types_all',
                    'therapies_by_disease_code', 'therapies_by_disease_code_and_line_code',
                    'therapy_components_by_disease_code', 'therapy_types_by_disease_code'}:
            return self.therapies(name, args)
        if name in {'all_countries', 'histologic_type'}:
            model = 'PreferredCountry' if name == 'all_countries' else 'HistologicType'
            rows = sorted(self.rows(model), key=lambda r: (r.get('sort_key') is None, r.get('sort_key') or 0, r['title']))
            return self.options(rows, True)
        if name in {'cytogenic_markers', 'molecular_markers'}:
            model = 'CytogenicMarker' if name == 'cytogenic_markers' else 'MolecularMarker'
            return {'none': 'None', **self.options(self.rows(model))}
        if name == 'pre_existing_conditions':
            rows = sorted(self.rows('PreExistingConditionCategory'), key=lambda r: r['title'])
            return {**({'none': 'None'} if args and args[0] else {}), **self.options(rows)}
        if name == 'languages_skills':
            return {'': 'Unknown', **{f"{skill['code']}__{lang['code']}": f"{skill['title']} {lang['title']}"
                for lang in self.rows('Language') for skill in self.rows('LanguageSkillLevel')}}
        if name == 'concomitant_medications_by_disease_code':
            diseases = {r['id'] for r in self.rows('Disease') if r['code'] == args[0]}
            ids = {r['concomitant_medication_id'] for r in self.rows('ConcomitantMedicationDisease') if r['disease_id'] in diseases}
            return dict(sorted(self.options([r for r in self.rows('ConcomitantMedication') if r['id'] in ids]).items()))
        if name == 'planned_therapies':
            diseases = {r['id'] for r in self.rows('Disease') if r['code'] == args[0].upper()}
            round_code = args[1] if len(args) > 1 else None
            rounds = {r['id'] for r in self.rows('TherapyRound') if r['code'] == round_code}
            ids = {r['planned_therapy_id'] for r in self.rows('PlannedTherapyDiseaseConnection')
                if r['disease_id'] in diseases and (not round_code or r['round_id'] is None or r['round_id'] in rounds)}
            return {'none': 'No planned therapy', **self.options([r for r in self.rows('PlannedTherapy') if r['id'] in ids])}
        if name == 'protein_expressions_mcl':
            assignment = next(n for n in self.methods[name].body if isinstance(n, ast.Assign)
                              and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'mcl_codes')
            codes = ast.literal_eval(assignment.value)
            return self.options([r for r in self.rows('ProteinExpression') if r['code'] in codes], True)
        if name in {'all_mutation_variants', 'mutation_origins_per_gene', 'mutation_all_origins', 'mutation_all_interpretations'}:
            return self.genetics(name)
        return super().method(name, args)

    def therapies(self, name, args):
        model = ('TherapyComponentCategory' if name.startswith('therapy_types') else
                 'TherapyComponent' if name.startswith('therapy_components') else 'Therapy')
        rows = self.rows(model)
        if name.endswith('_all'):
            return dict(sorted(self.options(rows).items()))
        disease_code = args[0].upper()
        self.diseases.add(disease_code)
        disease = next((r['id'] for r in self.rows('Disease') if r['code'] == disease_code), None)
        links = [r for r in self.rows('DiseaseRoundTherapyConnection') if r['disease_id'] == disease]
        scoped_line = name == 'therapies_by_disease_code_and_line_code'
        if scoped_line:
            line = next((r['id'] for r in self.rows('TherapyRound') if r['code'] == args[1]), None)
            links = [r for r in links if r['round_id'] == line]
        ids = {r['therapy_id'] for r in links}
        if None in ids:
            raise ValueError('CancerBot therapy provider dereferences a null therapy; source review required.')
        component_links = self.rows('TherapyComponentConnection')
        if model != 'Therapy':
            ids = {r['component_id'] for r in component_links if r['therapy_id'] in ids}
        if model == 'TherapyComponentCategory':
            ids = {r['category_id'] for r in self.rows('TherapyComponentCategoryConnection') if r['component_id'] in ids}
        rows = [r for r in rows if r['id'] in ids]
        if scoped_line:
            # Therapy.full_title(): names of actual related components, ordered by id.
            # Django's M2M join excludes null links; never create a blank component.
            values = {'': 'Unknown/Other'}
            for row in rows:
                component_ids = {r['component_id'] for r in component_links if r['therapy_id'] == row['id']}
                titles = ', '.join(r['title'] for r in self.rows('TherapyComponent') if r['id'] in component_ids)
                values[row['code']] = f"{row['title']} ({titles})" if titles else row['title']
            return values
        return dict(sorted(self.options(rows).items()))

    def genetics(self, name):
        result = {}
        for gene in self.rows('MutationGene'):
            variants = [r for r in self.rows('MutationCode') if r['gene_id'] == gene['id']]
            origin_ids = {r['origin_id'] for r in self.rows('MutationGeneOriginConnection') if r['gene_id'] == gene['id']}
            origins = [r for r in self.rows('MutationOrigin') if r['id'] in origin_ids]
            if name == 'all_mutation_variants':
                result[gene['code']] = [{'value': k, 'label': v} for k, v in self.options(variants, True).items()]
            elif name == 'mutation_origins_per_gene':
                result[gene['code']] = [{'value': k, 'label': v} for k, v in self.options(origins, bool(origins)).items()]
            elif name == 'mutation_all_origins':
                for origin in origins:
                    for target in [gene, *variants]:
                        result[f"{origin['code']}__{target['code']}"] = f"{origin['title']} {target['title']}"
            else:
                for interpretation in self.rows('MutationInterpretation'):
                    result[f"{gene['code']}__{interpretation['code']}"] = f"{gene['title']} {interpretation['title']}"
        return {'': 'Unknown', **result} if name in {'mutation_all_origins', 'mutation_all_interpretations'} else result
