"""Read migration definitions as evidence without importing or running them."""
import ast
import hashlib
from collections import Counter
from pathlib import Path

from omop_core.services.cancerbot_reference_options import REFERENCE_MODELS
from omop_core.services.field_inventory_history_seeds import SEED_DECLARATIONS


REVIEWED = {
    '0337_patch_remove_tumor_grade_4.py': 'e0ca59d462a1d3cc1210bdf400664f706611e327366b8cf75ed6a3aa502c4eea',
    '0350_merge_lowercase_disease_codes.py': '8620567f2595b723eedd1903cd179f03f35559ca6facc413e16265668a6174f3',
    '0371_delete_targeted_therapy_all.py': 'e1f3cc196ccd487d5d1738c28342d1f61e6405e3adbeea4821c79e796b7bae7d',
    '0403_remap_junk_therapy_codes.py': 'a60f69a47c5ccb7e6b17a7de4a096bab1da247938c129917bd8d410bdc4f0a42',
    '0404_dedup_st_john_s_wort.py': 'a193db9e1df9fe5d982fd0dfd02ce5ef0fcb6eb3cfc0f1d4974bda918987078a',
    '0406_remove_nsaids_therapy.py': '1e67b83d8d58d5fbb243acde80cced046faf9a0a1f8d5b876af145f769202cc9',
}


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return {'unresolved_expression': ast.unparse(node)}


def _reviewed_events(name):
    if name == '0337_patch_remove_tumor_grade_4.py':
        return [{'kind': 'invalid_value_clear', 'models': ['PatientInfo'], 'field': 'tumor_grade',
                 'old_code': 40, 'replacement_code': None,
                 'condition': 'Clear the obsolete numeric grade-4 value to null; this is not selection of Unknown or a valid breast biopsy grade.'}]
    if name == '0350_merge_lowercase_disease_codes.py':
        return [{'kind': 'catalog_alias', 'models': ['Disease'], 'old_code': old, 'replacement_code': new,
                 'condition': 'Repoint disease relationships and deduplicate, or rename when uppercase counterpart is absent. Do not normalize unrelated codes.'}
                for old, new in {'mm': 'MM', 'fl': 'FL', 'bc': 'BC'}.items()]
    if name == '0371_delete_targeted_therapy_all.py':
        return [{'kind': 'catalog_removal', 'models': ['TrialType'], 'old_code': 'targeted_therapy_all',
                 'replacement_code': None,
                 'condition': 'Trial-type search category removed; this is not a Therapy, component or class catalog retirement.'}]
    if name == '0403_remap_junk_therapy_codes.py':
        return [{'kind': 'conditional_replacement', 'models': ['Therapy', 'TherapyComponent', 'TherapyComponentCategory'],
                 'old_code': old, 'replacement_code': new,
                 'condition': 'Replacement must exist in the same catalog level; unresolved references abort removal.',
                 'historical_membership': 'Not established for every model; this is the migration rule.'}
                for old, new in {'i': 'ixazomib', 's': 'selinexor', 't': 'tazemetostat'}.items()]
    if name == '0404_dedup_st_john_s_wort.py':
        return [{'kind': 'catalog_alias', 'models': ['TherapyComponent'], 'old_code': 'st._john_s_wort',
                 'replacement_code': "st._john's_wort",
                 'condition': 'Repoint duplicate to canonical component, or rename if canonical absent; no global punctuation normalization.'}]
    if name == '0406_remove_nsaids_therapy.py':
        return [{'kind': 'catalog_removal', 'models': ['Therapy'], 'old_code': 'nsaids', 'replacement_code': None,
                 'condition': 'Misclassified regimen removed; concomitant medication remains a separate source identity.'}]
    return []


def _is_noop(function):
    if not isinstance(function, ast.FunctionDef):
        return False
    # Comments are absent from AST. A commented-out data repair is not evidence
    # that aliases were ever migrated.
    return all(isinstance(n, ast.Pass)
               or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
               or (isinstance(n, ast.Return) and (n.value is None or isinstance(n.value, ast.Constant) and n.value.value is None))
               for n in function.body)


def _loader_sources(root, tree):
    """Fingerprint current loader dependencies, not their historical execution."""
    pending = [tree]
    found = {}
    while pending:
        current = pending.pop()
        for node in ast.walk(current):
            if not (isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('trials.services.') and not node.level):
                continue
            name = node.module.replace('.', '/') + '.py'
            path = root / name
            if name in found or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                continue
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            pending.append(ast.parse(path.read_text()))
    return [{'path': name, 'sha256': digest} for name, digest in sorted(found.items())]


def _seed_options(tree, declarations):
    """Read only pinned literal declarations; never evaluate loader expressions."""
    options = []
    for function, variable, model, destination, scope in declarations:
        body = tree.body
        if function:
            body = next(n.body for n in body if isinstance(n, ast.FunctionDef) and n.name == function)
        node = next(n for n in body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == variable for t in n.targets))
        values = ast.literal_eval(node.value)
        if isinstance(values, dict):
            pairs = list(values.items())
        elif isinstance(values, set):
            # Python set declarations remove duplicates; retain the source's
            # actual collection semantics and sort for reproducible artifacts.
            pairs = [(v, v) for v in sorted(values)]
        else:
            pairs = [(v['code'], v['title']) if isinstance(v, dict) else
                     tuple(v) if isinstance(v, (list, tuple)) else (v, v) for v in values]
        for code, label in pairs:
            if scope.get('source_code_transform') == 'palb1_variant':
                code = code.replace('>', '_').replace(' ', '_').lower()
            options.append({'function': function, 'variable': variable, 'line': node.lineno,
                            'model': model, 'destination': destination, 'scope': scope,
                            'code': code, 'label': label})
    return options


def historical_option_rows(history, field_names):
    """Keep historical option occurrences separate from live provider membership."""
    from omop_core.services.field_inventory import inventory_row

    rows = []

    def add(event, key, label, destination, scope, namespace):
        owner = '#1229' if (destination or '').startswith('genetic_mutations.') else '#1228'
        row = inventory_row('cancerbot_history', namespace, key, str(label), value=key,
                            field=destination if destination in field_names else None, scope=scope,
                            owner=owner, evidence=[event],
                            reason='Historical source definition only. Preserve as a legacy option requiring review; '
                                   'absence from current catalogs does not prove retirement or equivalence.')
        row['source_presence'] = 'historical_definition'
        row['destination_candidates'] = [destination] if destination else []
        rows.append(row)

    for event in history.get('seed_options', []):
        add(event, event['code'], event['label'], event['destination'],
            {'source_model': event['model'], **event['scope']},
            f"{event['file']}:{event['function'] or 'module'}:{event['variable']}")
    for event in history.get('schema_events', []):
        choices = event.get('choices')
        if not isinstance(choices, (list, tuple)):
            continue
        field = event['name'] if event['model_name'].lower() == 'patientinfo' else None
        for pair in choices:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], str):
                add(event, pair[0], pair[1], field, {'source_model': event['model_name']},
                    f"{event['file']}:{event['operation_index']}:{event['name']}")
    for event in history.get('catalog_events', []):
        for model in event['models']:
            add(event, event['old_code'], event['old_code'], event.get('field'), {'source_model': model},
                f"{event['file']}:{model}:old_code")
            # Record a source migration rule, not a global clinical alias or a
            # claim that this deployment applied it.
            rows[-1]['source_replacement_rule'] = event
            rows[-1]['owning_issue'] = '#1230' if model.startswith('Therapy') else '#1228'
    return rows


def collect_cancerbot_history(root):
    base = Path(root) / 'trials/migrations' if root else None
    if base is None or not base.is_dir():
        return {'status': 'source_unavailable', 'complete': False, 'schema_events': [], 'data_migrations': [], 'catalog_events': []}
    allowed = {name.lower() for name in REFERENCE_MODELS} | {'patientinfo'}
    schema_events, data_migrations, catalog_events, files, seed_options = [], [], [], {}, []
    for path in sorted(base.glob('[0-9]*.py')):
        content = path.read_text()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[path.name] = digest
        tree = ast.parse(content)
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        imports = {a.asname or a.name: n.module + '.' + a.name
                   for n in tree.body if isinstance(n, ast.ImportFrom) and n.module for a in n.names}
        loader_sources = _loader_sources(Path(root), tree)
        seed_rule = SEED_DECLARATIONS.get(path.name)
        if seed_rule and seed_rule['sha256'] == digest:
            seed_options += [{**r, 'file': path.name, 'sha256': digest,
                              'status': 'historical_seed_definition_not_execution_receipt'}
                             for r in _seed_options(tree, seed_rule['declarations'])]
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Migration'), None)
        if cls is None:
            continue
        operations = next((n.value for n in cls.body if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == 'operations' for t in n.targets)), None)
        if operations is None:
            continue
        if not isinstance(operations, (ast.List, ast.Tuple)):
            data_migrations.append({'file': path.name, 'sha256': digest, 'status': 'dynamic_operations_require_review'})
            continue
        for index, op in enumerate(operations.elts):
            evidence = {'file': path.name, 'sha256': digest, 'operation_index': index, 'line': op.lineno}
            if (isinstance(op, ast.Call) and isinstance(op.func, ast.Name)
                    and imports.get(op.func.id) == 'django.contrib.postgres.operations.CreateExtension'):
                schema_events.append({**evidence, 'kind': 'CreateExtension', 'status': 'schema_operation_not_value_change'})
                continue
            if not (isinstance(op, ast.Call) and isinstance(op.func, ast.Attribute)
                    and isinstance(op.func.value, ast.Name) and op.func.value.id == 'migrations'):
                data_migrations.append({**evidence, 'status': 'unrecognized_operation_requires_review'})
                continue
            kind = op.func.attr
            args = {kw.arg: _literal(kw.value) for kw in op.keywords if kw.arg in {'model_name', 'name', 'old_name', 'new_name'}}
            model = args.get('model_name', args.get('old_name') if kind == 'RenameModel'
                             else args.get('name') if kind in {'DeleteModel', 'CreateModel'} else None)
            if kind in {'RenameField', 'RemoveField', 'RenameModel', 'DeleteModel'} and isinstance(model, str) and model.lower() in allowed:
                schema_events.append({**evidence, 'kind': kind, **args,
                                      'status': 'schema_history_only_not_value_equivalence'})
            if kind in {'AddField', 'AlterField'} and isinstance(model, str) and model.lower() in allowed:
                field = next((kw.value for kw in op.keywords if kw.arg == 'field'), None)
                if isinstance(field, ast.Call):
                    choices = next((kw.value for kw in field.keywords if kw.arg == 'choices'), None)
                    if choices is not None:
                        schema_events.append({**evidence, 'kind': 'DeclaredChoices', **args, 'choices': _literal(choices),
                                              'status': 'historical_declaration_not_current_membership'})
            if kind == 'CreateModel' and isinstance(model, str) and model.lower() in allowed:
                fields = next((kw.value for kw in op.keywords if kw.arg == 'fields'), None)
                if isinstance(fields, (ast.List, ast.Tuple)):
                    for pair in fields.elts:
                        if not (isinstance(pair, (ast.List, ast.Tuple)) and len(pair.elts) == 2
                                and isinstance(pair.elts[1], ast.Call)):
                            continue
                        choices = next((kw.value for kw in pair.elts[1].keywords if kw.arg == 'choices'), None)
                        if choices is not None:
                            schema_events.append({**evidence, 'kind': 'DeclaredChoices', 'model_name': model,
                                                  'name': _literal(pair.elts[0]), 'choices': _literal(choices),
                                                  'status': 'historical_declaration_not_current_membership'})
            if kind in {'RunPython', 'RunSQL', 'SeparateDatabaseAndState'}:
                reviewed = REVIEWED.get(path.name) == digest and kind == 'RunPython'
                function = functions.get(op.args[0].id) if kind == 'RunPython' and op.args and isinstance(op.args[0], ast.Name) else None
                status = ('reviewed_catalog_rule' if reviewed else 'source_noop' if _is_noop(function)
                          else 'delegated_source_requires_review' if loader_sources else 'data_operation_requires_review')
                data_migrations.append({**evidence, 'kind': kind,
                                        'callable': ast.unparse(op.args[0]) if op.args else None,
                                        'loader_sources': loader_sources,
                                        'status': status})
        if REVIEWED.get(path.name) == digest:
            catalog_events += [{**event, 'file': path.name, 'sha256': digest,
                                'status': 'reviewed_source_rule_not_execution_receipt'} for event in _reviewed_events(path.name)]
    return {'status': 'source_history_indexed', 'complete': False,
            'files': [{'path': name, 'sha256': digest} for name, digest in files.items()],
            'schema_events': schema_events, 'data_migrations': data_migrations, 'catalog_events': catalog_events,
            'seed_options': seed_options,
            'counts': {'schema_events': len(schema_events), 'catalog_events': len(catalog_events),
                       'historical_seed_options': len(seed_options),
                       **dict(Counter(r['status'] for r in data_migrations))},
            'missing_or_changed_reviewed_files': [name for name, digest in REVIEWED.items() if files.get(name) != digest],
            'missing_or_changed_seed_files': [name for name, rule in SEED_DECLARATIONS.items()
                                              if files.get(name) != rule['sha256']],
            'limitation': 'Definitions only; no migration, patient or trial table is read or executed. '
                          'Unreviewed data operations include work outside field mapping. Absence never establishes retirement.'}
