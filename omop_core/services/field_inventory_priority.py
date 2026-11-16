"""Per-disease priority-field coverage from the reference-only inventory.

An approved storage recipe is distinct from a compatible standard parent
concept. This report never changes a recipe or its clinical review state.
"""
from collections import Counter, defaultdict


def priority_field_coverage(markers, rows, candidates):
    fields = {r['destination_path']: r for r in rows if r['source'] == 'promop_field'}
    records, diseases = [], defaultdict(list)
    for marker in markers:
        field = marker['field_name']
        row = fields.get(field)
        mappings = row.get('existing_mappings', []) if row else []
        mapping = mappings[0] if len(mappings) == 1 else None
        problems = []
        if row is None:
            problems.append('missing_field')
        if mapping is None:
            problems.append('missing_or_ambiguous_mapping')
        else:
            if mapping.get('status') != 'approved':
                problems.append('unapproved_mapping')
            if mapping.get('omop_table') != 'measurement':
                problems.append('parent_requires_measurement')
            if mapping.get('value_kind') != 'json' or mapping.get('multiple') is not True:
                problems.append('parent_requires_multiple_json')
            if not mapping.get('source_value') or len(mapping['source_value']) > 50:
                problems.append('invalid_source_identity')
        evidence = []
        if mapping:
            for candidate in candidates.values():
                if (candidate['concept_id'] == mapping.get('concept_id')
                        or (candidate.get('vocabulary_id'), candidate.get('concept_code')) ==
                        (mapping.get('vocabulary_id'), mapping.get('concept_code'))):
                    evidence.append({k: candidate.get(k) for k in (
                        'concept_id', 'vocabulary_id', 'concept_code', 'concept_name', 'domain_id',
                        'passes_mechanical_screen', 'provenance_verified')})
        compatible = [c['concept_id'] for c in evidence if c['passes_mechanical_screen'] and c['domain_id'] == 'Measurement']
        state = ('storage_recipe_incomplete' if problems else
                 'standard_parent_candidate_requires_review' if compatible else 'source_only_parent')
        record = {'field_name': field, 'marker_key': marker['key'], 'gene': marker['gene'], 'kind': marker['kind'],
                  'diseases': marker['diseases'], 'legacy_keys': marker.get('legacy_keys', []),
                  'storage_mapping': mapping, 'problems': problems, 'state': state,
                  'declared_concept_evidence': evidence, 'compatible_parent_candidate_ids': compatible,
                  'owning_issue': '#1311', 'semantic_approval': False}
        records.append(record)
        for disease in marker['diseases']:
            diseases[disease].append(record)
        if row:
            row['priority_field_coverage'] = {'state': state, 'diseases': marker['diseases'], 'owning_issue': '#1311'}
    return {'owning_issue': '#1311', 'fields': records, 'total_fields': len(records),
            'disease_memberships': sum(len(v) for v in diseases.values()),
            'by_disease': {disease: {'field_names': sorted(r['field_name'] for r in members),
                                   'counts': dict(Counter(r['state'] for r in members))}
                           for disease, members in sorted(diseases.items())},
            'counts': dict(Counter(r['state'] for r in records)),
            'limitation': 'Source-only parents remain supported by the Genomics Measurement/event contract. '
                          'An Observation-domain concept cannot certify a standard Measurement parent. '
                          'Field question coverage does not approve gene/variant answer concepts.'}


STATE_LABELS = {
    'source_only_parent': 'supported source-only parent (concept 0 retained)',
    'standard_parent_candidate_requires_review': 'standard Measurement parent candidate requires review',
    'storage_recipe_incomplete': 'missing, unapproved or contract-incompatible storage recipe',
}


def priority_ready(coverage):
    """The gate: every field needs an approved, Measurement/JSON/multiple recipe.

    A compatible parent candidate still needs review and a source-only parent is
    supported, so neither fails the storage gate; a missing, unapproved or
    incompatible recipe does.
    """
    return not coverage['counts'].get('storage_recipe_incomplete')


def priority_json(coverage):
    import json
    return json.dumps(coverage, indent=2, sort_keys=True) + '\n'


def priority_markdown(coverage):
    """A focused, human-readable extract of the inventory's #1311 section."""
    lines = [
        '# Priority gene/marker storage coverage (#1311)',
        '',
        f"**{coverage['total_fields']} effective fields across "
        f"{coverage['disease_memberships']} disease memberships.**",
        '',
        'Generated from the reference-only inventory '
        '(`manifest.json` → `priority_field_coverage`); it records storage '
        'dispositions, not clinical approval of catalog values or gene/variant '
        'answer concepts.',
        '',
        '## Dispositions',
        '',
        '| Disposition | Fields | Meaning |',
        '| --- | ---: | --- |',
    ]
    for state in ('source_only_parent', 'standard_parent_candidate_requires_review',
                  'storage_recipe_incomplete'):
        lines.append(
            f"| {state} | {coverage['counts'].get(state, 0)} | {STATE_LABELS[state]} |")
    lines += [
        '',
        f"**Gate:** {'PASS' if priority_ready(coverage) else 'FAIL'} — "
        'missing, unapproved and incompatible recipes fail; supported source-only '
        'parents and standard parent candidates pass.',
        '',
        '## Per disease',
        '',
    ]
    for disease, block in coverage['by_disease'].items():
        counts = ', '.join(f'{k}={v}' for k, v in sorted(block['counts'].items()))
        lines.append(f"### {disease} — {len(block['field_names'])} memberships ({counts or 'none'})")
        lines.append('')
        lines.append('| Field | Gene | State | Problems | Parent recipe | Portable code | Concept target |')
        lines.append('| --- | --- | --- | --- | --- | --- | --- |')
        for record in coverage['fields']:
            if record['field_name'] not in block['field_names']:
                continue
            mapping = record.get('storage_mapping') or {}
            evidence = record.get('declared_concept_evidence') or []
            targets = ', '.join(
                f"{item['concept_id']} ({item['domain_id']})" for item in evidence) or '—'
            lines.append(
                f"| `{record['field_name']}` | {record.get('gene') or ''} | {record['state']} | "
                f"{', '.join(record.get('problems') or []) or '—'} | "
                f"{mapping.get('omop_table') or '—'}/{mapping.get('value_kind') or '—'}/"
                f"multiple={mapping.get('multiple')} | {mapping.get('concept_code') or '—'} | "
                f"{targets} |")
        lines.append('')
    lines += [
        '## Regenerate',
        '',
        '```sh',
        'python manage.py export_field_mapping_inventory --output docs/field-mapping-inventory/manifest.json --report docs/field-mapping-inventory/coverage.md',
        'python manage.py export_genomics_priority_coverage --write',
        'python manage.py export_genomics_priority_coverage --check',
        '```',
        '',
        coverage['limitation'],
        '',
    ]
    return '\n'.join(lines)
