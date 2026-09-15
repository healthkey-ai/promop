"""Reference inventory primitives. Candidates are evidence, never approvals."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path


SCHEMA_VERSION = 1
DISPOSITIONS = {
    'needs_review', 'ambiguous', 'no_equivalent', 'not_applicable',
    'requires_structured_representation', 'verified_mapping',
}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def typed(value):
    # bool must precede int: True, 1, "1", blank and null are different identities.
    kind = ('null' if value is None else 'boolean' if isinstance(value, bool)
            else 'integer' if isinstance(value, int) else 'number' if isinstance(value, float)
            else 'string' if isinstance(value, str) else 'json')
    return {'type': kind, 'value': value}


def source_revision(root, paths):
    root = Path(root)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    return {'revision': git('rev-parse', 'HEAD'),
            'dirty': bool(git('status', '--porcelain', '--', *paths)),
            'files': {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                      for p in sorted(paths)}}


def inventory_row(source, option_list, key, label, *, field=None, value=None,
                  kind='value', scope=None, role=None, evidence=None, aliases=None,
                  disposition='needs_review', reason=None, owner='#1223'):
    scope = {'disease': None, 'system': None, 'edition': None, 'basis': None,
             'method': None, **(scope or {})}
    identity = [source, option_list, typed(key), scope, kind]
    return {
        'id': hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24],
        'kind': kind, 'source': source, 'option_list': option_list,
        'source_key': typed(key), 'source_label': label, 'destination_path': field,
        'canonical_value': typed(value), 'aliases': aliases or [], 'scope': scope,
        'mapping_role': role or ('question' if kind == 'field' else 'answer'),
        'existing_mappings': [], 'candidate_ids': [], 'search_evidence': evidence or [],
        'disposition': disposition, 'reason': reason or 'Semantic review required.',
        'reviewer': None, 'owning_issue': owner, 'retired': None,
        'replacement_aliases': [],
    }


def literal_binding_method(expression, literal_methods):
    """Recognize only direct lists and the known value/label wrapper.

    Finding a literal list somewhere inside a call/conditional does not prove
    that the expression returns that list unchanged.
    """
    node = ast.parse(expression, mode='eval').body
    if not (isinstance(node, ast.Dict) and len(node.keys) == 1
            and isinstance(node.keys[0], ast.Constant) and node.keys[0].value == 'options'):
        return None
    node = node.values[0]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'
            and node.func.attr == 'to_value_and_label' and len(node.args) == 1 and not node.keywords):
        node = node.args[0]
    if isinstance(node, ast.Call):
        if node.args or node.keywords:
            return None
        node = node.func
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == 'self' and node.attr in literal_methods):
        return node.attr
    return None


def cancerbot_source(path):
    """Enumerate public list bindings and literal fragments, without executing code.

    Even a literal return is not a live catalog. Conditions, helper methods and
    database expansion remain visible as coverage gaps, including nested lists.
    """
    tree = ast.parse(Path(path).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ValueOptions')
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    literal_methods = set()
    for name, method in methods.items():
        body = [n for n in method.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        if len(body) == 1 and isinstance(body[0], ast.Return):
            try:
                value = ast.literal_eval(body[0].value)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and all(isinstance(k, (str, int, float, bool)) and isinstance(v, str) for k, v in value.items()):
                literal_methods.add(name)
    bindings, rows, fragments = [], [], []
    for node in ast.walk(methods['get_all_options']):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            bindings = [{'option_list': ast.literal_eval(k), 'expression': ast.unparse(v),
                         'line': v.lineno, 'coverage': 'requires_live_export'}
                        for k, v in zip(node.value.keys, node.value.values) if k is not None]
            break
    for binding in bindings:
        method = literal_binding_method(binding['expression'], literal_methods)
        if method:
            binding.update(coverage='covered_by_static_source', source_method=method)
    for name, method in methods.items():
        if name in {'get_all_options', 'all_options'} or name.startswith('_'):
            continue
        for node_index, node in enumerate(n for n in ast.walk(method) if isinstance(n, ast.Dict)):
            for index, (key, label) in enumerate(zip(node.keys, node.values)):
                if key is None:
                    continue
                try:
                    key, label = ast.literal_eval(key), ast.literal_eval(label)
                except (ValueError, TypeError):
                    continue
                if not isinstance(key, (str, int, float, bool)) or not isinstance(label, str):
                    continue
                list_key = f'{name}:literal:{node_index}'
                rows.append(inventory_row('cancerbot_source', list_key, key, label, value=key,
                    evidence=[{'method': 'static_literal', 'line': node.lineno, 'entry': index}],
                    reason=('Complete literal source list; destination/semantics require reconciliation.'
                            if name in literal_methods else 'Source fragment; live membership and destination are unverified.')))
                fragments.append(name)
    for binding in bindings:
        if binding['coverage'] == 'covered_by_static_source':
            binding['source_row_ids'] = [r['id'] for r in rows if r['option_list'] == f"{binding['source_method']}:literal:0"]
    from omop_core.services.cancerbot_static_options import reconcile_static_bindings
    rows += reconcile_static_bindings(Path(path).read_text(), bindings, inventory_row)
    excluded_methods = {'register': 'registers', 'trialPurpose': 'trial_purposes', 'trialType': 'trial_types'}
    excluded = {excluded_methods[b['option_list']] for b in bindings if b['coverage'] == 'excluded_trial_search'}
    for row in rows:
        if row['option_list'].split(':')[0] in excluded:
            row.update(disposition='not_applicable',
                       reason='Trial search metadata; outside patient clinical field/value mapping scope.')
    return {'bindings': bindings, 'rows': rows, 'literal_methods': sorted(set(fragments))}


def validate_live_export(payload, expected_lists):
    """Accept a deliberately narrow, reference-only envelope, fail closed on extras."""
    if set(payload) != {'schema_version', 'source_revision', 'exported_at', 'options'}:
        raise ValueError('CancerBot export requires schema_version, source_revision, exported_at, options only.')
    if payload['schema_version'] != 1 or not payload['source_revision'] or not payload['exported_at']:
        raise ValueError('CancerBot export needs version 1 and source/export metadata.')
    if not isinstance(payload['options'], dict):
        raise ValueError('CancerBot options must be an object keyed by public option list.')
    rows = []
    def visit(value, option_list, parents=()):
        if isinstance(value, dict) and set(value) == {'options'}:
            visit(value['options'], option_list, parents)
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict) or set(item) != {'value', 'label'}:
                    raise ValueError('Each exported option must contain only value and label.')
                if not isinstance(item['label'], str) or not isinstance(item['value'], (str, int, float, bool, type(None))):
                    raise ValueError('Exported option labels must be strings and values scalar.')
                rows.append(inventory_row('cancerbot_live', option_list, item['value'], item['label'],
                    value=item['value'], scope={'parent_keys': list(parents)},
                    evidence=[{'method': 'live_reference_export', 'revision': payload['source_revision']}]))
        elif isinstance(value, dict):
            for key, child in sorted(value.items()):
                visit(child, option_list, (*parents, key))
        else:
            raise ValueError('Nested catalogs must contain option lists.')
    unexpected = set(payload['options']) - set(expected_lists)
    if unexpected:
        raise ValueError(f'Unrecognized public option lists: {sorted(unexpected)}')
    for name, options in sorted(payload['options'].items()):
        visit(options, name)
    return rows, sorted(set(expected_lists) - set(payload['options']))


def apply_live_coverage(bindings, payload, rows):
    """Update per-list evidence and provider state, including empty live lists."""
    by_list = defaultdict(list)
    for row in rows:
        by_list[row['option_list']].append(row['id'])
    for binding in bindings:
        name = binding['option_list']
        if name in payload['options']:
            if binding['coverage'] != 'covered_by_live_export':
                binding['coverage_before_live_export'] = binding['coverage']
            binding['coverage'] = 'covered_by_live_export'
            binding['live_source_row_ids'] = sorted(by_list[name])
            binding['live_source_revision'] = payload['source_revision']
            binding['live_exported_at'] = payload['exported_at']
    return sorted(b['option_list'] for b in bindings if b['coverage'] == 'requires_live_export')


def screen_candidate(concept, vocabulary, as_of):
    """Mechanical checks only; uncertain vocabulary lineage never passes."""
    as_of = date.fromisoformat(str(as_of)[:10])
    start, end = concept.get('valid_start_date'), concept.get('valid_end_date')
    version = (vocabulary or {}).get('vocabulary_version', '') or ''
    source = concept.get('source')
    checks = {
        'standard': concept.get('standard_concept') == 'S',
        'not_invalid': not concept.get('invalid_reason'),
        'valid_dates': bool(start and end and date.fromisoformat(str(start)[:10]) <= as_of <= date.fromisoformat(str(end)[:10])),
        'external_number_range': 0 < concept['concept_id'] < 2_000_000_000,
        # Concept.source's documented convention is NULL for external loads.
        # This is a mechanical convention, not proof of row-level lineage.
        'external_source_convention': source is None,
        'external_vocabulary': not str(concept.get('vocabulary_id', '')).lower().startswith(('hk', 'healthkey')),
        'vocabulary_metadata_present': bool(vocabulary),
        'vocabulary_not_synthetic': bool(version) and not any(s in version.lower() for s in ('synthetic', 'benchmark', 'test seed')),
        'vocabulary_not_deprecated': bool(vocabulary) and not vocabulary.get('is_deprecated'),
    }
    return {**concept, 'checks': checks, 'passes_mechanical_screen': all(checks.values()),
            'semantic_approval': False, 'provenance_verified': False,
            'vocabulary_version': version}


def coverage(rows, sources):
    by_id, by_identity = defaultdict(list), defaultdict(list)
    for row in rows:
        if row['disposition'] not in DISPOSITIONS or not row['owning_issue']:
            raise ValueError('Every row requires a disposition and owner.')
        by_id[row['id']].append(row)
        if row['destination_path']:
            key = canonical_json([row['destination_path'], row['canonical_value'], row['scope']])
            by_identity[key].append(row['id'])
    return {
        'total_rows': len(rows), 'by_kind': dict(sorted(Counter(r['kind'] for r in rows).items())),
        'by_source': dict(sorted(Counter(r['source'] for r in rows).items())),
        'by_disposition': dict(sorted(Counter(r['disposition'] for r in rows).items())),
        'by_owner': dict(sorted(Counter(r['owning_issue'] for r in rows).items())),
        'without_destination': sum(r['destination_path'] is None for r in rows),
        'without_candidate': sum(not r['candidate_ids'] for r in rows),
        'duplicate_source_ids': [key for key, entries in sorted(by_id.items()) if len(entries) > 1],
        'shared_destination_identities': [sorted(ids) for _, ids in sorted(by_identity.items()) if len(ids) > 1],
        'source_coverage': sources,
    }


def validate_manifest(manifest):
    """Check conservation and references before publishing a snapshot."""
    rows, totals = manifest['rows'], manifest['totals']
    if totals != coverage(rows, totals['source_coverage']):
        raise ValueError('Manifest totals do not reconcile with source rows.')
    if totals['duplicate_source_ids']:
        raise ValueError('Manifest contains duplicate source identities.')
    if 'cancerbot_bindings' in manifest:
        bindings = manifest['cancerbot_bindings']
        ids = {r['id'] for r in rows}
        for binding in bindings:
            if any(pk not in ids for key in ('source_row_ids', 'live_source_row_ids') for pk in binding.get(key, [])):
                raise ValueError('CancerBot binding references a missing source row.')
        providers = totals['source_coverage']['cancerbot_public_lists']
        if providers['by_provider'] != dict(Counter(b['coverage'] for b in bindings)):
            raise ValueError('CancerBot provider totals do not reconcile with bindings.')
        missing = sorted(b['option_list'] for b in bindings if b['coverage'] == 'requires_live_export')
        if sorted(providers['missing_live_lists']) != missing:
            raise ValueError('CancerBot outstanding lists do not reconcile with bindings.')
    for row in rows:
        if any(str(key) not in manifest['candidates'] for key in row['candidate_ids']):
            raise ValueError('Candidate reference missing from manifest.')
    for relationship in manifest['maps_to']:
        if any(str(relationship[key]) not in manifest['candidates'] for key in ('concept_1_id', 'concept_2_id')):
            raise ValueError('Relationship endpoint missing from manifest.')
    if any(c['semantic_approval'] for c in manifest['candidates'].values()):
        raise ValueError('Inventory cannot grant semantic approval.')


def staging_therapy_coverage(tables, bindings):
    """Use the CancerBot-derived staging catalogs as the authoritative source.

    Public picker membership is expressed through existing reference IDs, never
    another copy of the catalog. Planned eligibility is not inferred from past
    treatment round links.
    """
    diseases = {r['id']: r for r in tables['vocabulary_disease']}
    rounds = {r['id']: r for r in tables['therapy_round']}
    regimens = {r['id']: r for r in tables['therapy_regimen']}
    components = {r['id']: r for r in tables['therapy_component']}
    classes = {r['id']: r for r in tables['therapy_class']}
    code_map = {'Mm': 'C3242', 'Fl': 'C3209', 'Bc': 'C9335', 'Cll': 'C2987', 'Mcl': 'MCL'}
    round_map = {'FirstLine': 'first_line_therapy', 'SecondLine': 'second_line_therapy',
                 'LaterLine': 'later_line_therapy', 'Supportive': 'supportive_therapy'}
    links = tables['disease_therapy_regimen']
    orphan_links = [r['id'] for r in links if r['disease_id'] not in diseases
                    or r['round_id'] not in rounds or r['regimen_id'] not in regimens]
    memberships = []
    for binding in bindings:
        name = binding['option_list']
        if name.startswith('plannedTherapies'):
            binding.update(coverage='staging_catalog_available_context_pending',
                source_tables=['therapy_regimen', 'disease_therapy_regimen'],
                reason='Authoritative catalog is exported; planned eligibility/status is not encoded in disease/round links.')
            continue
        match = re.fullmatch(r'(therapies|therapyComponents|therapyTypes)(All|Mm|Fl|Bc|Cll|Mcl)', name)
        line_match = re.fullmatch(r'therapies(FirstLine|SecondLine|LaterLine)(Mm|Fl|Bc|Cll|Mcl)', name)
        supportive = re.fullmatch(r'supportiveTherapies(Mm|Fl|Bc|Cll|Mcl)', name)
        if not (match or line_match or supportive):
            continue
        role = match[1] if match else 'therapies'
        suffix = match[2] if match else line_match[2] if line_match else supportive[1]
        disease_code = code_map.get(suffix)
        round_code = round_map[line_match[1]] if line_match else 'supportive_therapy' if supportive else None
        selected_links = [r for r in links if r['id'] not in orphan_links
            and (not disease_code or diseases[r['disease_id']]['code'] == disease_code)
            and (not round_code or rounds[r['round_id']]['code'] == round_code)]
        regimen_ids = set(regimens) if suffix == 'All' else {r['regimen_id'] for r in selected_links}
        component_ids = set(components) if suffix == 'All' else {
            r['component_id'] for r in tables['therapy_regimen_component'] if r['regimen_id'] in regimen_ids}
        class_ids = set(classes) if suffix == 'All' else {
            r['therapy_class_id'] for r in tables['therapy_component_class'] if r['component_id'] in component_ids}
        table, ids = {'therapies': ('therapy_regimen', regimen_ids),
                      'therapyComponents': ('therapy_component', component_ids),
                      'therapyTypes': ('therapy_class', class_ids)}[role]
        membership = {'option_list': name, 'source_table': table, 'source_ids': sorted(ids),
            'disease_code': disease_code, 'round_code': round_code,
            'catalog_options': len(ids), 'disease_round_link_ids': sorted(r['id'] for r in selected_links),
            'unknown_other_sentinel': bool(line_match or supportive),
            'coverage': 'covered_by_staging_reference'}
        memberships.append(membership)
        binding.update(coverage=membership['coverage'], source_tables=[table, 'disease_therapy_regimen'],
                       reason='User confirmed staging therapy catalogs and disease links were generated from CancerBot.')
    return {'source': 'CancerBot-derived PRomop staging reference tables',
            'authority': 'User confirmed 2026-09-14: therapies/components/classes and regimen/disease mappings already reflected on staging.',
            'catalog_counts': {'regimens': len(regimens), 'components': len(components), 'classes': len(classes),
                               'disease_round_links': len(links)},
            'memberships': memberships, 'orphan_disease_round_links': orphan_links,
            'context_pending_lists': [b['option_list'] for b in bindings if b['coverage'] == 'staging_catalog_available_context_pending']}


def render_report(manifest):
    totals = manifest['totals']
    lines = ['# Field and value reference inventory', '',
             f"Snapshot: {manifest['generated_at']}. Schema: {manifest['schema_version']}.", '',
             '**Inventory remains incomplete. No candidates are clinically approved by this export.**', '',
             f"{totals['total_rows']} source rows; {totals['without_destination']} have no direct scalar-field destination "
             '(see source/catalog routing below); '
             f"{totals['without_candidate']} have no attached candidate.", '',
             '| Source | Rows |', '|---|---:|']
    lines += [f'| {key} | {value} |' for key, value in totals['by_source'].items()]
    lines += ['', '| Disposition | Rows |', '|---|---:|']
    lines += [f'| {key} | {value} |' for key, value in totals['by_disposition'].items()]
    acceptance = manifest.get('inventory_acceptance')
    if acceptance:
        lines += ['', '## Inventory acceptance evidence', '', acceptance['limitation'], '',
                  f"Accounting checks pass: {acceptance['accounting_checks_pass']}. Review status: `{acceptance['review_status']}`.", '',
                  '| #21 field | Representation | Disposition | Validation flags |', '|---|---|---|---|']
        lines += [f"| `{r['field']}` | {r['representation']} | {r['disposition']} | {', '.join(r['validation_flags']) or 'None recorded'} |"
                  for r in acceptance['issue_21']]
        lines += ['', '| #26 scope | Source bindings | Source occurrences | Missing runtime fields | Owner |', '|---|---:|---:|---|---|']
        lines += [f"| {r['scope']} | {len(r['source_bindings'])} | {len(r['source_row_ids'])} | {', '.join(r['missing_runtime_destinations']) or 'None recorded'} | {r['owner']} |"
                  for r in acceptance['issue_26']]
        if acceptance['problems']:
            lines += ['', 'Accounting gaps:', '', *['- ' + problem for problem in acceptance['problems']]]
    lines += ['', '## Coverage gaps', '']
    lines += [f'- {gap}' for gap in manifest['limitations']]
    lines += ['', '## CancerBot public-list reconciliation', '',
              '| Coverage | Lists |', '|---|---:|']
    providers = totals['source_coverage'].get('cancerbot_public_lists', {}).get('by_provider', {})
    lines += [f'| {key} | {count} |' for key, count in sorted(providers.items())]
    lines += ['', 'Static results describe the checked-in source definitions, including empty and blank-only lists. '
              'They do not certify a deployed CancerBot version. Trial-search exclusions retain their source evidence. '
              'Unresolved providers below require reference data; model names come from source imports, not label matching.', '',
              '| Unresolved list | Source models |', '|---|---|']
    for binding in manifest.get('cancerbot_bindings', []):
        if binding['coverage'] == 'requires_live_export':
            providers = binding.get('static_resolution', {}).get('provider_models', [])
            lines.append(f"| {binding['option_list']} | {', '.join(providers) or 'unresolved source expression'} |")
    therapy = manifest.get('therapy_source_coverage')
    crosswalk = manifest.get('destination_crosswalk')
    contracts = manifest.get('implementation_contracts')
    if contracts:
        lines += ['', '## Implementation destination accounting', '', contracts['limitation'], '',
                  '| Destination evidence | Source rows |', '|---|---:|']
        lines += [f'| {key} | {count} |' for key, count in contracts['counts'].items()]
        lines += ['', f"Rows awaiting a source/consumer routing disposition: {len(contracts['unresolved_row_ids'])}. "
                  'Missing runtime fields and ambiguous clinical contexts remain explicit even when source routing is known.']
    if crosswalk:
        lines += ['', '## CancerBot destination routing', '',
                  f"Source review status: `{crosswalk.get('status', 'unrecorded')}`.", '',
                  'Routes retain source disease, line, nested keys and representation warnings separately from immutable source identity. '
                  'A recorded route is not clinical equivalence or an approved answer mapping.', '',
                  '| Route status | Bindings |', '|---|---:|']
        lines += [f'| {key} | {count} |' for key, count in crosswalk.get('counts', {}).items()]
        lines += ['', '| Source list | Missing PRomop destination |', '|---|---|']
        lines += [f"| {r['option_list']} | {', '.join(r['missing_destinations'])} |"
                  for r in crosswalk.get('bindings', []) if r['status'] == 'destination_missing']
    if therapy:
        lines += ['', '## CancerBot-derived staging therapy catalogs', '',
                  'Staging is the authoritative source for these catalogs, as confirmed by the user. '
                  'Catalog and disease/round link rows are already included in the reference export.', '',
                  '| Catalog | Rows |', '|---|---:|']
        lines += [f'| {key} | {value} |' for key, value in therapy['catalog_counts'].items()]
        lines += ['', '| Public list | Catalog options | Disease code | Round |', '|---|---:|---|---|']
        lines += [f"| {r['option_list']} | {r['catalog_options']} | {r['disease_code'] or 'all'} | {r['round_code'] or 'all'} |"
                  for r in therapy['memberships']]
        lines += ['', 'Staging membership above is preserved as independent evidence. Where live CancerBot rows are supplied, '
                  'the public binding references those rows instead. PlannedTherapy is a separate CancerBot source catalog; '
                  'its disease/round eligibility never proves administration or a mapping to a PRomop regimen. '
                  f"Planned lists still awaiting source eligibility: {len(therapy['context_pending_lists'])}. "
                  'Unknown/Other sentinels are recorded separately from catalog counts.', '']
    source_history = manifest.get('source_history')
    if source_history:
        lines += ['', '## CancerBot source history', '', source_history.get('limitation', 'Source history unavailable.'), '',
                  f"Migration definition accounting complete: {source_history.get('definition_coverage_complete', False)}.", '',
                  '| History evidence | Count |', '|---|---:|']
        lines += [f'| {key} | {count} |' for key, count in source_history.get('counts', {}).items()]
        lines += ['', '| Catalog scope | Old code | Replacement | Rule |', '|---|---|---|---|']
        lines += [f"| {', '.join(r['models'])} | `{r['old_code']}` | `{r['replacement_code'] or '(none)'}` | {r['condition']} |"
                  for r in source_history.get('catalog_events', [])]
    priority = manifest.get('priority_field_coverage')
    if priority:
        lines += ['', '## Priority gene and marker field mappings (#1311)', '',
                  f"{priority['total_fields']} distinct fields across {priority['disease_memberships']} disease memberships. "
                  + priority['limitation'], '',
                  '| Disease | Fields | Storage incomplete | Source-only parent | Standard parent candidate requiring review |',
                  '|---|---:|---:|---:|---:|']
        for disease, data in priority['by_disease'].items():
            counts = data['counts']
            lines.append(f"| {disease} | {len(data['field_names'])} | {counts.get('storage_recipe_incomplete', 0)} | "
                         f"{counts.get('source_only_parent', 0)} | {counts.get('standard_parent_candidate_requires_review', 0)} |")
    lines += ['', '## Reconciliation', '',
              f"Duplicate source identities: {len(totals['duplicate_source_ids'])}. "
              f"Shared destination/value/context groups: {len(totals['shared_destination_identities'])}. "
              'These groups are evidence for review, not automatic aliases.', '',
              '| Validation flag | Source rows |', '|---|---:|']
    flags = Counter(flag for row in manifest['rows'] for flag in set(row.get('validation_flags', [])))
    lines += [f'| {flag} | {count} |' for flag, count in sorted(flags.items())]
    lines += ['',
              '## Reference table counts', '', '| Table | Rows |', '|---|---:|']
    lines += [f'| {key} | {len(value)} |' for key, value in sorted(manifest['reference_tables'].items())]
    lines += ['', '## Vocabulary provenance', '',
              'Both release mechanisms and vocabulary history are recorded separately in the manifest. '
              'An Athena release label does not certify individual concept lineage.', '']
    for vocab, data in manifest['vocabulary_metadata'].items():
        if any(s in (data.get('vocabulary_version') or '').lower() for s in ('synthetic', 'benchmark')):
            lines.append(f"- {vocab}: `{data['vocabulary_version']}`. All attached candidates fail the vocabulary provenance screen; coordinate #461/#623.")
    lines += ['', '## Representation decisions', '', '| Scope | Required representation | Owner |', '|---|---|---|']
    lines += [f"| {r['scope']} | {r['decision']} | {r['owner']} |" for r in manifest['representation_decisions']]
    return '\n'.join(lines) + '\n'
