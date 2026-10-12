"""Reviewed source-provider routing for expressions outside the literal parser.

Rules are pinned to source hashes. Drift remains an explicit unresolved binding;
this module never executes frontend code or queries patient records.
"""
import hashlib
import json
from pathlib import Path

from omop_core.services.field_inventory import inventory_row


def reconcile_frontend_providers(root, frontend, tables):
    root = Path(root)
    config = json.loads((root / 'omop_core/data/field_inventory_frontend_providers.json').read_text())
    for item in [*frontend['controls'], *frontend['unresolved']]:
        item.pop('provider_resolution', None)
    rows, evidence = [], []
    seen = set()
    for rule in config['rules']:
        controls = [c for c in frontend['controls'] if c['file'] == rule['file']
                    and c['expression'] == rule['expression']
                    and (not rule.get('field') or c['field'] == rule['field'])]
        if not controls:
            continue
        drift = [name for name, digest in rule['sources'].items()
                 if not (root / name).is_file() or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
        if rule['file'] in frontend.get('files', {}) and frontend['files'][rule['file']] != rule['sources'][rule['file']]:
            drift.append(rule['file'])
        record = {'file': rule['file'], 'expression': rule['expression'], 'kind': rule['kind'],
                  'sources': rule['sources'], 'reason': rule['reason'],
                  'status': 'source_changed' if drift else 'source_provider_accounted_for', 'changed_files': drift}
        if rule['kind'] == 'lookup_titles' and rule['table'] not in tables:
            record['status'] = 'reference_table_missing'
        evidence.append(record)
        if record['status'] != 'source_provider_accounted_for':
            continue
        for control in controls:
            control['provider_resolution'] = record
        values = []
        if rule['kind'] == 'lookup_titles':
            record.update(table=rule['table'], value_column='title', fallback_constant=rule['fallback_constant'],
                          descriptor_precedence=True)
            values = [{'value': r['title'], 'label': r['title'], 'reference_id': r['id'], 'reference_code': r['code']}
                      for r in tables[rule['table']]]
        elif rule['kind'] == 'language_capabilities':
            values = rule['values']
            for constant in frontend['unresolved']:
                if constant['file'] == rule['file'] and constant['name'] == rule['expression']:
                    constant['provider_resolution'] = record
        record['option_row_ids'] = []
        for value in values:
            destination = rule.get('field') or rule.get('destination')
            item = inventory_row('frontend_provider', f"{rule['file']}:{rule['expression']}",
                                 value['value'], value['label'], field=destination, value=value['value'],
                                 evidence=[{'method': 'reviewed_source_provider', 'sources': rule['sources'],
                                            'reference_table': rule.get('table'), 'source_record': value}],
                                 reason=rule['reason'])
            if rule['kind'] == 'language_capabilities':
                item.update(disposition='requires_structured_representation', mapping_role='reference')
            record['option_row_ids'].append(item['id'])
            if item['id'] not in seen:
                seen.add(item['id'])
                rows.append(item)
    frontend['provider_reconciliation'] = evidence
    return rows
