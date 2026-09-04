"""Import the reviewed, generated HealthTree crossmap artifact into SCCM."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


DEFAULT_ARTIFACT = Path(__file__).resolve().parents[3] / 'docs' / 'ht-code-concept-mapping.md'


class Command(BaseCommand):
    help = 'Bulk-import the generated HealthTree crossmap artifact into SCCM.'

    def add_arguments(self, parser):
        parser.add_argument('--artifact', default=str(DEFAULT_ARTIFACT), help='Generated HealthTree Markdown or JSON artifact path.')
        parser.add_argument('--dry-run', action='store_true', help='Report without writing rows.')
        parser.add_argument('--limit', type=int, default=0, help='Maximum artifact rows to process (0 = all).')
        parser.add_argument('--batch-size', type=int, default=1000, help='Rows per bulk insert batch.')

    def handle(self, **options):
        path = Path(options['artifact'])
        try:
            mappings = self._read_artifact(path)
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f'Cannot read HealthTree mapping artifact {path}: {exc}') from exc
        if options['limit']:
            mappings = mappings[:options['limit']]
        target_keys = {(row['target_vocabulary_id'], str(row['target_concept_code'])) for row in mappings}
        targets = {
            (concept.vocabulary_id, concept.concept_code): concept
            for concept in Concept.objects.filter(
                vocabulary_id__in={vocabulary for vocabulary, _code in target_keys},
                standard_concept='S',
                # An Athena release can retire a formerly standard concept.
                # HealthTree's historical resolver data remains useful for
                # review, but must never seed a proposal to an invalid target.
                invalid_reason__isnull=True,
            ).filter(concept_code__in={code for _vocabulary, code in target_keys}).only(
                'concept_id', 'concept_code', 'vocabulary_id', 'domain_id',
            )
        }
        source_ids = {row['source_concept_id'] for row in mappings if row.get('source_concept_id')}
        sources = {concept.concept_id: concept for concept in Concept.objects.filter(concept_id__in=source_ids).only('concept_id')}
        existing = set(SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id__in={row['source_vocabulary_id'] for row in mappings},
            source_code__in={row['source_code'] for row in mappings},
        ).values_list('source_vocabulary_id', 'source_code'))
        pending = []
        stats = {'created': 0, 'existing': 0, 'missing': 0, 'proposed': 0}
        for row in mappings:
            target = targets.get((row['target_vocabulary_id'], str(row['target_concept_code'])))
            if not target:
                stats['missing'] += 1
                continue
            if row['status'] == 'proposed':
                stats['proposed'] += 1
            key = (row['source_vocabulary_id'], row['source_code'])
            if key in existing:
                stats['existing'] += 1
                continue
            stats['created'] += 1
            # Both HealthTree projects contribute every generated mapping. Prefer
            # One as the canonical provenance; Next remains meaningful for a
            # future Next-only artifact row.
            origin_system = (
                'HT-One' if 'HT-One' in row.get('origins', [])
                else (row['origins'][0] if row.get('origins') else 'HT-One')
            )
            pending.append(SourceCodeConceptMapping(
                source_vocabulary_id=row['source_vocabulary_id'],
                source_code=row['source_code'],
                domain_id=target.domain_id or row['domain_id'],
                source_code_description=row.get('source_code_description', ''),
                source_concept=sources.get(row.get('source_concept_id')),
                target_concept=target,
                destination_vocabulary_id=row['target_vocabulary_id'],
                omop_table=DOMAIN_TO_TABLE.get(target.domain_id or row['domain_id'], ''),
                status=row['status'],
                origin='import',
                origin_system=origin_system,
                source=origin_system,
                occurrence_count=0,
            ))
        if not options['dry_run'] and pending:
            SourceCodeConceptMapping.objects.bulk_create(
                pending, batch_size=options['batch_size'], ignore_conflicts=True,
            )
        verb = 'Would create' if options['dry_run'] else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {stats["created"]:,}; existing {stats["existing"]:,}; '
            f'missing target {stats["missing"]:,}; proposed {stats["proposed"]:,}.'
        ))

    @staticmethod
    def _read_artifact(path):
        text = path.read_text()
        if path.suffix == '.json':
            return json.loads(text)['mappings']
        mappings = []
        for line in text.splitlines():
            if not line.startswith('| ') or line.startswith('| ---') or 'Source system' in line:
                continue
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            if len(cells) != 8:
                continue
            source_vocab, source_code, target_vocab, target_code, domain, status, origins, _candidate_count = cells
            mappings.append({
                'source_vocabulary_id': source_vocab,
                'source_code': source_code.replace('\\|', '|'),
                'source_code_description': '',
                'source_concept_id': None,
                'target_vocabulary_id': target_vocab,
                'target_concept_code': target_code,
                'domain_id': domain,
                'status': status,
                'origins': [origin for origin in origins.split(', ') if origin],
            })
        return mappings
