"""Build the reviewable HealthTree crossmap artifact.

This command is the only place that reads the HealthTree application
repositories. Runtime imports use the generated JSON artifact instead.
"""
import json
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


PROJECTS = {
    'one': ('HT-One', Path.home() / 'one'),
    'next': ('HT-Next', Path.home() / 'healthtree-platform'),
}
FHIR_ROOT = Path('functions/main/firestore/apps/curehub')
DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / 'data' / 'healthtree_crossmaps.json'
DEFAULT_MARKDOWN = Path(__file__).resolve().parents[3] / 'docs' / 'HealthTree_Code_To_Concept_Mapping.md'


class Command(BaseCommand):
    help = 'Build a reviewable HealthTree One/Next source-code mapping artifact.'

    def add_arguments(self, parser):
        parser.add_argument('--one-root', help='Path to the HealthTree One repository.')
        parser.add_argument('--next-root', help='Path to the HealthTree Next repository.')
        parser.add_argument('--output', default=str(DEFAULT_ARTIFACT), help='Artifact JSON output path.')
        parser.add_argument('--markdown-output', default=str(DEFAULT_MARKDOWN), help='Reviewable Markdown output path.')

    def handle(self, **options):
        roots = {
            'one': Path(options['one_root']) if options['one_root'] else PROJECTS['one'][1],
            'next': Path(options['next_root']) if options['next_root'] else PROJECTS['next'][1],
        }
        candidates = defaultdict(lambda: defaultdict(lambda: {'count': 0, 'origins': set()}))
        source_metadata = {}
        for project, root in roots.items():
            origin = PROJECTS[project][0]
            if not root.is_dir():
                raise CommandError(f'HealthTree {project} repository not found: {root}')
            for row in self._read_project(root / FHIR_ROOT):
                key = (row['source_vocabulary_id'], row['source_code'])
                target_key = (row['target_vocabulary_id'], row['target_concept_code'], row['domain_id'])
                candidate = candidates[key][target_key]
                candidate['count'] += 1
                candidate['origins'].add(origin)
                metadata = source_metadata.setdefault(key, {
                    'source_code_description': row['source_code_description'],
                    'source_concept_id': row['source_concept_id'],
                    'origins': set(),
                })
                if not metadata['source_code_description'] and row['source_code_description']:
                    metadata['source_code_description'] = row['source_code_description']
                if metadata['source_concept_id'] is None and row['source_concept_id'] is not None:
                    metadata['source_concept_id'] = row['source_concept_id']
                metadata['origins'].add(origin)

        mappings = []
        for key in sorted(candidates):
            ranked = sorted(
                candidates[key].items(),
                key=lambda item: (-item[1]['count'], -len(item[1]['origins']), item[0]),
            )
            selected, selected_meta = ranked[0]
            metadata = source_metadata[key]
            ambiguous = len(ranked) > 1
            mappings.append({
                'source_vocabulary_id': key[0],
                'source_code': key[1],
                'source_code_description': metadata['source_code_description'],
                'source_concept_id': metadata['source_concept_id'],
                'target_vocabulary_id': selected[0],
                'target_concept_code': selected[1],
                'domain_id': selected[2],
                'status': 'proposed' if ambiguous else 'approved',
                'origins': sorted(metadata['origins']),
                'selection_reason': self._selection_reason(selected_meta, len(ranked)),
                'candidates': [
                    {
                        'target_vocabulary_id': target[0],
                        'target_concept_code': target[1],
                        'domain_id': target[2],
                        'occurrences': candidate['count'],
                        'origins': sorted(candidate['origins']),
                    }
                    for target, candidate in ranked
                ],
            })
        artifact = {
            'schema_version': 1,
            'selection_policy': (
                'Select the target with the greatest occurrence count across HealthTree One and Next; '
                'break ties by contributing-project count, then target vocabulary/code/domain.'
            ),
            'mappings': mappings,
        }
        output = Path(options['output'])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + '\n')
        markdown_output = Path(options['markdown_output'])
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        self._write_markdown(markdown_output, artifact)
        proposed = sum(row['status'] == 'proposed' for row in mappings)
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(mappings):,} mappings to {output} and {markdown_output}; {proposed:,} proposed.'
        ))

    @staticmethod
    def _write_markdown(path, artifact):
        lines = [
            '# HealthTree Code-to-Concept Mapping',
            '',
            'Generated by `manage.py build_healthtree_crossmap_artifact`; do not edit by hand.',
            '',
            f"Selection policy: {artifact['selection_policy']}",
            '',
            '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |',
        ]
        for row in artifact['mappings']:
            def cell(value):
                return str(value or '').replace('|', '\\|').replace('\n', ' ')
            lines.append('| ' + ' | '.join([
                cell(row['source_vocabulary_id']), cell(row['source_code']),
                cell(row['target_vocabulary_id']), cell(row['target_concept_code']),
                cell(row['domain_id']), cell(row['status']), ', '.join(row['origins']),
                str(len(row['candidates'])),
            ]) + ' |')
        path.write_text('\n'.join(lines) + '\n')

    @staticmethod
    def _selection_reason(selected_meta, candidate_count):
        if candidate_count == 1:
            return 'Only target observed in HealthTree resolver data.'
        return (
            f"Selected from {selected_meta['count']} occurrence(s) across "
            f"{len(selected_meta['origins'])} project(s), ahead of {candidate_count - 1} alternative target(s)."
        )

    @staticmethod
    def _load_json(path):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Cannot read {path}: {exc}') from exc

    def _read_project(self, root):
        for code, targets in self._load_json(root / 'FHIR/resourcesTypes/r4/Condition/_icd10ToSnomedMappings.json').items():
            for target in targets or []:
                yield self._row('ICD10', code, 'SNOMED', target, 'Condition')
        for entry in self._load_json(root / 'FHIR/codeSystems/cptToSnomedMap.json').values():
            if entry.get('snomedId') and entry['snomedId'] != '0':
                yield self._row('CPT4', entry.get('cptCode', ''), 'SNOMED', entry['snomedId'], 'Procedure', entry.get('cptDescriptor', ''), entry.get('cptConceptId'))
        for source, target in self._load_json(root / 'FHIR/codeSystems/snomedToRxNormMap.json').items():
            yield self._row('SNOMED', source, 'RxNorm', target, 'Drug')
        for entry in self._load_json(root / 'medicalResources/_DocumentReferenceAI/linesOfTherapy/adverseEvents/_utils/MDRToSnomed.json'):
            if entry.get('mdr_code') and entry.get('snomed_code'):
                yield self._row('MedDRA', entry['mdr_code'], 'SNOMED', entry['snomed_code'], 'Condition', entry.get('mdr_name', ''))

    @staticmethod
    def _row(source_vocab, source_code, target_vocab, target_code, domain, description='', source_id=None):
        return {
            'source_vocabulary_id': source_vocab,
            'source_code': str(source_code)[:100],
            'source_code_description': str(description or '')[:255],
            'source_concept_id': int(source_id) if source_id and str(source_id).isdigit() else None,
            'target_vocabulary_id': target_vocab,
            'target_concept_code': str(target_code),
            'domain_id': domain,
        }
