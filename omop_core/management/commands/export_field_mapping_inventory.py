"""Export reference tables and source evidence without querying patient rows."""
import json
import hashlib
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from psycopg import sql

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from omop_core import models
from omop_core.services.field_inventory import (
    SCHEMA_VERSION, cancerbot_source, canonical_json, coverage, inventory_row,
    render_report, screen_candidate, source_revision, validate_live_export,
    validate_manifest,
    staging_therapy_coverage,
    apply_live_coverage,
)


# Only explicit reference columns can leave the database. Never export reviewer
# identifiers, free-text review notes, release notes, credentials or patient rows.
REFERENCE_COLUMNS = {
    'id', 'code', 'title', 'value', 'sort_key', 'sort_order', 'source_name', 'source_url',
    'concept_id', 'gene_id', 'field_name', 'display', 'choice_id', 'vocabulary_id',
    'is_primary', 'concept_code', 'unit', 'omop_table', 'source_value', 'value_kind',
    'type_concept_id', 'value_vocabulary', 'multiple', 'status', 'provenance',
    'reviewed_at', 'formula', 'is_active', 'synonym_text', 'source',
    'regimen_id', 'component_id', 'therapy_class_id', 'disease_id', 'round_id', 'therapyoutcome_id',
}
RELEASE_COLUMNS = {
    'id', 'release_id', 'schema_version', 'scope', 'corpus_scope', 'build_timestamp',
    'athena_version', 'vocab_versions', 'vocabulary_versions', 'row_counts', 'checksums',
    'table_checksums', 'status', 'published_at', 'umls_release',
}
CONCEPT_COLUMNS = (
    'concept_id', 'concept_name', 'concept_code', 'vocabulary_id', 'domain_id',
    'concept_class_id', 'standard_concept', 'invalid_reason', 'valid_start_date',
    'valid_end_date', 'source',
)

DECISIONS = [
    {'scope': 'bone_lesions', 'fields': ['bone_lesions'], 'owner': '#1228',
     'decision': 'Keep count and >2 comparator separate from presence; do not equate source counts with Yes/No.'},
    {'scope': 'GELF / FLIPI', 'fields': ['gelf_criteria_status', 'flipi_score_options', 'flipi_risk_category'], 'owner': '#1228',
     'decision': 'Retain seven GELF criteria and aggregate separately; retain five FLIPI inputs, numeric score and risk separately.'},
    {'scope': 'TNM and staging basis', 'fields': ['tumor_stage', 'nodes_stage', 'distant_metastasis_stage', 'staging_modalities'], 'owner': '#1227',
     'decision': 'Retain tumor, system, edition and c/p/yp basis. Imaging modality is a separate event.'},
    {'scope': 'ISS / R-ISS / legacy stage; Rai / Binet', 'fields': ['stage', 'r_iss_stage', 'binet_stage'], 'owner': '#1228',
     'decision': 'Keep disease and staging system in identity; legacy I–IV has unresolved system, never infer R-ISS.'},
    {'scope': 'Markers and genetics', 'fields': ['genetic_mutations', 'cytogenetic_markers', 'protein_expressions'], 'owner': '#1229',
     'decision': 'Use frozen Genomics findings/components; retain gene, variant, origin, interpretation, polarity and independent results.'},
    {'scope': 'Treatment outcomes', 'fields': ['therapy_outcome'], 'owner': '#1228',
     'decision': 'Retain disease/response system and event; MRD and response categories may require separate observations (#253).'},
    {'scope': 'Therapy catalogs and rounds', 'fields': [], 'owner': '#1230',
     'decision': 'Reuse regimen/component/class and disease/round links; preserve planned versus administered status.'},
    {'scope': 'Administrative / computed / legacy', 'fields': [], 'owner': '#1223',
     'decision': 'Use existing descriptor categories and formulas; retain compatibility aliases and no-concept administrative representations.'},
    {'scope': 'Unknown / none / absent / equivocal', 'fields': [], 'owner': '#1224',
     'decision': 'Preserve distinct typed source keys; missing or cleared values never become selected Unknown.'},
]


def read_table(table, allowed):
    # Callers supply only model-derived reference tables or named release tables.
    with connection.cursor() as cursor:
        columns = [c.name for c in connection.introspection.get_table_description(cursor, table)
                   if c.name in allowed]
        if not columns:
            raise CommandError(f'No permitted reference columns in {table}')
        query = sql.SQL('SELECT {} FROM {} ORDER BY 1').format(
            sql.SQL(', ').join(map(sql.Identifier, columns)), sql.Identifier(table))
        cursor.execute(query)
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def collect_reference_tables():
    lookup_models = [m for m in apps.get_app_config('omop_core').get_models()
                     if issubclass(m, models.VocabularyLookup)]
    reference_models = [*lookup_models, models.ToxicityGrade, models.TherapyOutcome,
                        models.FieldConceptMapping, models.FieldChoice, models.FieldChoiceCode,
                        models.FieldSynonym, models.FieldFormula,
                        models.TherapyRegimenComponent, models.TherapyComponentClassLink,
                        models.DiseaseTherapyRegimen, models.TherapyOutcome.diseases.through]
    tables = {m._meta.db_table: read_table(m._meta.db_table, REFERENCE_COLUMNS) for m in reference_models}
    return tables, lookup_models


def collect_rows(tables, lookups, frontend):
    from omop_core.services.field_descriptor import _classify_field
    from omop_core.services.provenance_registry import get_registry

    registry = get_registry()
    mappings = {r['field_name']: r for r in tables['field_concept_mapping']}
    formulas = {r['field_name']: r for r in tables['field_formula']}
    synonyms = defaultdict(list)
    for row in tables['field_synonym']:
        synonyms[row['field_name']].append(row['synonym_text'])
    rows, fields = [], {f.name: f for f in models.PatientRecord._meta.concrete_fields}
    for name in sorted(set(fields) | set(mappings) | set(registry) | set(formulas)):
        category = _classify_field(name)
        disposition = 'not_applicable' if category in {'internal', 'profile', 'location', 'unit', 'computed', 'alias'} else 'needs_review'
        row = inventory_row('promop_field', 'PatientRecord', name, name, field=name, kind='field',
            aliases=synonyms[name], disposition=disposition,
            reason=f'Existing descriptor category: {category}. Clinical answer mapping is a separate review.')
        row['descriptor_category'] = category
        row['model_type'] = fields[name].get_internal_type() if name in fields else None
        row['recipe'] = asdict(registry[name]) if name in registry else None
        row['formula'] = formulas.get(name)
        row['existing_mappings'] = [mappings[name]] if name in mappings else []
        rows.append(row)
        if name in fields:
            choices = list(fields[name].flatchoices or [])
            if fields[name].get_internal_type() == 'BooleanField':
                choices += [(True, 'True'), (False, 'False')]
            for value, label in choices:
                rows.append(inventory_row('promop_model_choice', name, value, str(label), field=name, value=value))
    codes = defaultdict(list)
    for code in tables['field_choice_code']:
        codes[code['choice_id']].append(code)
    for choice in tables['field_choice']:
        row = inventory_row('promop_choice', choice['field_name'], choice['id'], choice['display'],
                            field=choice['field_name'], value=choice['display'])
        row['identity_limitation'] = 'Legacy choice ID is stable only within this source database; display is mutable.'
        row['existing_mappings'] = codes[choice['id']]
        row['field_mapping_key'] = choice['field_name'] if choice['field_name'] in mappings else None
        rows.append(row)
    catalog_models = [*lookups, models.ToxicityGrade, models.TherapyOutcome]
    destinations = defaultdict(list)
    for name, mapping in mappings.items():
        if mapping.get('value_vocabulary'):
            destinations[mapping['value_vocabulary']].append(name)
    for model in catalog_models:
        for item in tables[model._meta.db_table]:
            row = inventory_row('promop_catalog', model._meta.db_table, item['code'], item['title'],
                value=item['code'], role='reference', owner='#1230' if model.__name__.startswith('Therapy') else '#1228',
                scope={'gene_source_id': item['gene_id']} if 'gene_id' in item else None)
            row['destination_candidates'] = sorted(destinations[model.__name__])
            row['source_record'] = item
            if item.get('concept_id'):
                row['existing_mappings'] = [{'concept_id': item['concept_id']}]
            rows.append(row)
    constants = {r['name']: r for r in frontend['constants']}
    for const in frontend['constants']:
        for index, value in enumerate(const['values']):
            rows.append(inventory_row('frontend_constant', f"{const['file']}:{const['name']}", value, str(value), value=value,
                evidence=[{'file': const['file'], 'line': const['line'], 'index': index}]))
    for control in frontend['controls']:
        if not control['field']:
            continue
        options = control['options']
        if options is None and control['constant'] in constants:
            options = constants[control['constant']]['values']
        if options is None and control['type'] == 'boolean':
            options = [True, False]
        for index, value in enumerate(options or []):
            label = value.get('label', value.get('value')) if isinstance(value, dict) else value
            value = value.get('value') if isinstance(value, dict) else value
            disease = {'BreastCancerSection': 'BC', 'MyelomaSection': 'MM',
                       'LymphomaSection': 'FL', 'CLLSection': 'CLL', 'MCLSection': 'MCL'}.get(control['component'])
            rows.append(inventory_row('frontend_control', f"{control['file']}:{control['component']}:{control['field']}", value,
                str(label), field=control['field'], value=value, scope={'disease': disease},
                evidence=[control]))
    for row in rows:
        for decision in DECISIONS:
            if row['destination_path'] in decision['fields']:
                row['reason'] = decision['decision']
                row['owning_issue'] = decision['owner']
                row['disposition'] = 'requires_structured_representation'
    return rows


def attach_candidates(rows, vocabularies, as_of):
    ids, pairs = set(), set()
    for row in rows:
        row['validation_flags'] = []
        for mapping in row['existing_mappings']:
            if mapping.get('concept_id'):
                ids.add(mapping['concept_id'])
            if mapping.get('vocabulary_id') and (mapping.get('concept_code') or mapping.get('code')):
                pairs.add((mapping['vocabulary_id'], mapping.get('concept_code') or mapping['code']))
    query = Q(concept_id__in=sorted(ids))
    for vocabulary, code in sorted(pairs):
        query |= Q(vocabulary_id=vocabulary, concept_code=code)
    candidates = {c['concept_id']: screen_candidate(c, vocabularies.get(c['vocabulary_id']), as_of)
                  for c in models.Concept.objects.filter(query).values(*CONCEPT_COLUMNS)}
    by_pair = {(c['vocabulary_id'], c['concept_code']): c['concept_id'] for c in candidates.values()}
    for row in rows:
        for mapping in row['existing_mappings']:
            pair = (mapping.get('vocabulary_id'), mapping.get('concept_code') or mapping.get('code'))
            resolved = by_pair.get(pair)
            fk = mapping.get('concept_id')
            row['candidate_ids'] += [i for i in (fk, resolved) if i in candidates]
            row['search_evidence'].append({'method': 'existing_exact_code_or_fk', 'vocabulary_code': pair,
                'resolved_id': resolved, 'stored_id': fk, 'identity_conflict': bool(fk and pair[0] and resolved != fk)})
            if fk and pair[0] and resolved != fk:
                row['validation_flags'].append('stored_fk_disagrees_with_vocabulary_code')
            concept = candidates.get(fk or resolved)
            table = mapping.get('omop_table', '').lower()
            expected = {'measurement': 'Measurement', 'observation': 'Observation',
                        'condition_occurrence': 'Condition', 'procedure_occurrence': 'Procedure',
                        'drug_exposure': 'Drug', 'device_exposure': 'Device'}.get(table)
            if concept and expected and concept['domain_id'] != expected:
                row['validation_flags'].append('existing_mapping_domain_disagrees_with_table')
            if concept and not concept['passes_mechanical_screen']:
                row['validation_flags'].append('existing_candidate_fails_mechanical_screen')
            recipe = row.get('recipe') or {}
            recipe_codes = recipe.get('concept_codes') or []
            if recipe_codes and mapping.get('concept_code') and mapping['concept_code'] not in recipe_codes:
                row['validation_flags'].append('existing_mapping_code_differs_from_read_recipe')
        row['candidate_ids'] = sorted(set(row['candidate_ids']))
    return candidates


def search_candidates(rows, candidates, vocabularies, as_of):
    """Two bounded, batched exact-name searches; never infer semantic equivalence."""
    terms = sorted({r['source_label'] for r in rows if r['kind'] == 'value' and r['source_label']})
    found = defaultdict(set)
    for table, column in [('concept', 'concept_name'), ('concept_synonym', 'concept_synonym_name')]:
        with connection.cursor() as cursor:
            # Identifiers use composable SQL; terms stay query parameters.
            query = sql.SQL('SELECT {column}, concept_id FROM {table} WHERE {column} = ANY(%s) ORDER BY 1, 2').format(
                column=sql.Identifier(column), table=sql.Identifier(table))
            cursor.execute(query, [terms])
            for term, concept_id in cursor.fetchall():
                found[term].add(concept_id)
    selected = {term: sorted(ids)[:25] for term, ids in found.items()}
    new_ids = {i for ids in selected.values() for i in ids} - candidates.keys()
    for c in models.Concept.objects.filter(pk__in=new_ids).values(*CONCEPT_COLUMNS):
        candidates[c['concept_id']] = screen_candidate(c, vocabularies.get(c['vocabulary_id']), as_of)
    for row in rows:
        if row['kind'] != 'value':
            continue
        label = row['source_label']
        row['candidate_ids'] = sorted(set(row['candidate_ids']) | set(selected.get(label, [])))
        row['search_evidence'].append({'method': 'exact_name_or_synonym_case_sensitive', 'term': label,
            'matches': len(found.get(label, [])), 'recorded_limit': 25,
            'truncated': len(found.get(label, [])) > 25})


def attach_relationships(candidates, vocabularies, as_of):
    relationships = list(models.ConceptRelationship.objects.filter(
        concept_1_id__in=candidates, relationship_id='Maps to',
    ).values('concept_1_id', 'concept_2_id', 'relationship_id', 'valid_start_date', 'valid_end_date', 'invalid_reason'))
    target_ids = {r['concept_2_id'] for r in relationships} - candidates.keys()
    for c in models.Concept.objects.filter(pk__in=target_ids).values(*CONCEPT_COLUMNS):
        candidates[c['concept_id']] = screen_candidate(c, vocabularies.get(c['vocabulary_id']), as_of)
    for relation in relationships:
        relation['currently_valid'] = (not relation['invalid_reason'] and relation['valid_start_date'] <= as_of <= relation['valid_end_date'])
    return sorted(relationships, key=canonical_json)


def build_inventory(root, cancerbot_root, frontend, live_export=None, search=False):
    now = timezone.now()
    cancerbot_root = Path(cancerbot_root) if cancerbot_root else None
    tables, lookups = collect_reference_tables()
    vocabularies = {r['vocabulary_id']: r for r in models.Vocabulary.objects.values(
        'vocabulary_id', 'vocabulary_name', 'vocabulary_reference', 'vocabulary_version', 'is_deprecated')}
    rows = collect_rows(tables, lookups, frontend)
    source = cancerbot_source(cancerbot_root / 'trials/services/value_options.py') if cancerbot_root else {'rows': [], 'bindings': []}
    rows += source['rows']
    therapy_coverage = staging_therapy_coverage(tables, source['bindings'])
    expected_lists = [b['option_list'] for b in source['bindings']]
    missing_lists = [b['option_list'] for b in source['bindings'] if b['coverage'] == 'requires_live_export']
    live_metadata = None
    if live_export:
        payload = json.loads(Path(live_export).read_text())
        live_rows, _ = validate_live_export(payload, expected_lists)
        missing_lists = apply_live_coverage(source['bindings'], payload, live_rows)
        therapy_coverage['context_pending_lists'] = [b['option_list'] for b in source['bindings']
            if b['coverage'] == 'staging_catalog_available_context_pending']
        rows += live_rows
        live_metadata = {k: v for k, v in payload.items() if k != 'options'}
    from omop_core.services.genomics_catalog import catalog as effective_catalog
    catalog = effective_catalog()
    for marker in catalog['markers']:
        rows.append(inventory_row('genomics_catalog', 'markers', marker['key'], marker['label'],
            field=marker['field_name'], value=marker['gene'], aliases=marker['aliases'], role='reference',
            scope={'diseases': marker['diseases']}, disposition='requires_structured_representation',
            reason='Versioned marker catalog; use structured finding/components.', owner='#1229',
            evidence=[{'catalog_version': catalog['version'], 'source_commit': catalog['source_commit'],
                       'naming_decision': catalog['naming_decision']}]))
    candidates = attach_candidates(rows, vocabularies, now.date())
    if search:
        search_candidates(rows, candidates, vocabularies, now.date())
    relationships = attach_relationships(candidates, vocabularies, now.date())
    sources = {
        'promop_reference': {'coverage': 'exported', 'tables': {k: len(v) for k, v in tables.items()}},
        'cancerbot_public_lists': {'coverage': 'partial',
                                 'by_provider': dict(sorted(Counter(b['coverage'] for b in source['bindings']).items())),
                                 'missing_live_lists': missing_lists, 'live_metadata': live_metadata},
        'frontend': {'coverage': 'partial', 'controls': len(frontend['controls']),
                     'unresolved_constants': frontend['unresolved'],
                     'dynamic_controls': [c for c in frontend['controls'] if c['expression'] and c['options'] is None and c['constant'] not in {r['name'] for r in frontend['constants']}]},
    }
    release_tables = connection.introspection.table_names()
    releases = {t: read_table(t, RELEASE_COLUMNS) if t in release_tables else None
                for t in ('vocabulary_release', 'vocab_release')}
    history = read_table('vocabulary_version_history', {'id', 'vocabulary_id', 'version', 'action', 'cdm_release_date', 'created_at'})
    paths = ['omop_core/models.py', 'omop_core/services/mappings.py', 'omop_core/services/write_descriptor.py',
             'omop_core/services/field_descriptor.py', 'omop_core/services/provenance_registry.py',
             'omop_core/services/field_inventory.py',
             'omop_core/services/cancerbot_static_options.py',
             'omop_core/management/commands/export_field_mapping_inventory.py',
             'scripts/inventory-frontend-options.cjs',
             'omop_core/data/genomics_catalog_v1.json', 'omop_core/services/genomics_catalog.py', *frontend['files']]
    limitations = [
        f'CancerBot: {len(missing_lists)} public lists need further source/provider reconciliation; database-driven lists need live reference coverage. Therapy catalogs/disease links use authoritative staging exports; literal lists and planned picker context are tracked separately.',
        'CancerBot seed/migration retirement history and source-to-destination crosswalk still require reconciliation.',
        'Dynamic frontend expressions and dependent genetics lists require explicit provider reconciliation; see source_coverage.',
        'Reference catalogs preserve codes, links and destination candidates; unresolved destination/context is never inferred from labels.',
        'Exact labels and synonyms are lexical evidence only; case-sensitive search is not exhaustive and no-equivalent requires separate review.',
        'Existing approved statuses are preserved; candidate semantic meaning, destination domains and vocabulary lineage still require review.',
    ]
    if not search:
        limitations.append('Candidate search not requested; only existing code/FK references were resolved.')
    return {
        'schema_version': SCHEMA_VERSION, 'generated_at': now.isoformat(), 'complete': False,
        'scope': 'Reference data and source schema only; no patient values or identity tables queried.',
        'source_revisions': {'promop': source_revision(root, paths),
            'cancerbot': source_revision(cancerbot_root, ['trials/services/value_options.py']) if cancerbot_root else None},
        'rows': sorted(rows, key=lambda r: (r['source'], r['option_list'], r['id'])),
        'totals': coverage(rows, sources), 'limitations': limitations,
        'reference_tables': tables, 'cancerbot_bindings': source['bindings'], 'frontend': frontend,
        'therapy_source_coverage': therapy_coverage,
        'candidates': {str(k): v for k, v in sorted(candidates.items())}, 'maps_to': relationships,
        'vocabulary_metadata': vocabularies, 'vocabulary_releases': releases,
        'vocabulary_history': history, 'representation_decisions': DECISIONS,
    }


class Command(BaseCommand):
    help = 'Create a versioned reference-only field/answer inventory and coverage report (no writes to DB).'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)
        parser.add_argument('--report', required=True)
        parser.add_argument('--cancerbot-root', type=Path)
        parser.add_argument('--cancerbot-export', type=Path)
        parser.add_argument('--frontend-export', type=Path, help='Output of scripts/inventory-frontend-options.cjs')
        parser.add_argument('--search', action='store_true', help='Include exact label/synonym evidence (bounded by DB timeout).')

    def handle(self, **options):
        root = Path(settings.BASE_DIR)
        if options['cancerbot_export'] and not options['cancerbot_root']:
            raise CommandError('--cancerbot-export requires --cancerbot-root for list validation.')
        frontend = (json.loads(options['frontend_export'].read_text()) if options['frontend_export'] else
                    json.loads(subprocess.check_output(['node', str(root / 'scripts/inventory-frontend-options.cjs'), str(root)], text=True)))
        for name, digest in frontend['files'].items():
            path = root / name
            if not path.resolve().is_relative_to((root / 'frontend/src/components/PatientInfo').resolve()):
                raise CommandError('Frontend export contains a path outside the PatientInfo source tree.')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise CommandError(f'Frontend export source hash mismatch: {name}')
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                cursor.execute("SET LOCAL statement_timeout = '45s'")
            result = build_inventory(root, options['cancerbot_root'], frontend, options['cancerbot_export'], options['search'])
        validate_manifest(result)
        # Complete reads before replacing either artifact; exceptions leave prior output intact.
        Path(options['output']).write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + '\n')
        Path(options['report']).write_text(render_report(result))
        self.stdout.write(canonical_json({k: v for k, v in result['totals'].items() if k.startswith('by_') or k == 'total_rows'}))
