"""Read migration definitions as evidence without importing or running them."""
import ast
import hashlib
from collections import Counter
from pathlib import Path

from omop_core.services.cancerbot_reference_options import REFERENCE_MODELS


REVIEWED = {
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


def collect_cancerbot_history(root):
    base = Path(root) / 'trials/migrations' if root else None
    if base is None or not base.is_dir():
        return {'status': 'source_unavailable', 'complete': False, 'schema_events': [], 'data_migrations': [], 'catalog_events': []}
    allowed = {name.lower() for name in REFERENCE_MODELS} | {'patientinfo'}
    schema_events, data_migrations, catalog_events, files = [], [], [], {}
    for path in sorted(base.glob('[0-9]*.py')):
        content = path.read_text()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[path.name] = digest
        tree = ast.parse(content)
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
                data_migrations.append({**evidence, 'kind': kind,
                                        'status': 'reviewed_catalog_rule' if reviewed else 'data_operation_requires_review'})
        if REVIEWED.get(path.name) == digest:
            catalog_events += [{**event, 'file': path.name, 'sha256': digest,
                                'status': 'reviewed_source_rule_not_execution_receipt'} for event in _reviewed_events(path.name)]
    return {'status': 'source_history_indexed', 'complete': False, 'files': files,
            'schema_events': schema_events, 'data_migrations': data_migrations, 'catalog_events': catalog_events,
            'counts': {'schema_events': len(schema_events), 'catalog_events': len(catalog_events),
                       **dict(Counter(r['status'] for r in data_migrations))},
            'missing_or_changed_reviewed_files': [name for name, digest in REVIEWED.items() if files.get(name) != digest],
            'limitation': 'Definitions only; no migration, patient or trial table is read or executed. '
                          'Unreviewed data operations include work outside field mapping. Absence never establishes retirement.'}
