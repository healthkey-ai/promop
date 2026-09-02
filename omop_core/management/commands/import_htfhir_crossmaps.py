"""Import the HT-FHIR crossmap artifact into SourceCodeConceptMapping."""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


DEFAULT_ARTIFACT = Path(__file__).resolve().parents[3] / 'docs' / 'ht-fhir-code-concept-mapping.md'


class Command(BaseCommand):
    help = 'Bulk-import the HT-FHIR crossmap artifact into SCCM.'

    def add_arguments(self, parser):
        parser.add_argument('--artifact', default=str(DEFAULT_ARTIFACT), help='HT-FHIR Markdown artifact path.')
        parser.add_argument('--dry-run', action='store_true', help='Report without writing rows.')
        parser.add_argument('--limit', type=int, default=0, help='Maximum artifact rows to process (0 = all).')
        parser.add_argument('--batch-size', type=int, default=1000, help='Rows per bulk insert batch.')

    def handle(self, **options):
        path = Path(options['artifact'])
        try:
            mappings = self._read_artifact(path)
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f'Cannot read HT-FHIR mapping artifact {path}: {exc}') from exc
        if options['limit']:
            mappings = mappings[:options['limit']]

        # Separate mapped vs unmapped rows
        mapped = [row for row in mappings if row['target_vocabulary_id'] and row['target_concept_code']]
        unmapped_count = len(mappings) - len(mapped)

        # Look up target concepts
        target_keys = {(row['target_vocabulary_id'], str(row['target_concept_code'])) for row in mapped}
        targets = {
            (concept.vocabulary_id, concept.concept_code): concept
            for concept in Concept.objects.filter(
                vocabulary_id__in={vocabulary for vocabulary, _code in target_keys},
                standard_concept='S',
            ).filter(concept_code__in={code for _vocabulary, code in target_keys}).only(
                'concept_id', 'concept_code', 'vocabulary_id', 'domain_id',
            )
        }

        # Check existing SCCM rows
        existing = set(SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id__in={row['source_vocabulary_id'] for row in mapped},
            source_code__in={row['source_code'] for row in mapped},
        ).values_list('source_vocabulary_id', 'source_code'))

        pending = []
        stats = {'created': 0, 'existing': 0, 'missing': 0, 'unmapped': unmapped_count}
        for row in mapped:
            target = targets.get((row['target_vocabulary_id'], str(row['target_concept_code'])))
            if not target:
                stats['missing'] += 1
                continue
            key = (row['source_vocabulary_id'], row['source_code'])
            if key in existing:
                stats['existing'] += 1
                continue
            stats['created'] += 1
            pending.append(SourceCodeConceptMapping(
                source_vocabulary_id=row['source_vocabulary_id'],
                source_code=row['source_code'],
                domain_id=target.domain_id or '',
                source_code_description=row.get('source_code_description', ''),
                target_concept=target,
                destination_vocabulary_id=row['target_vocabulary_id'],
                omop_table=DOMAIN_TO_TABLE.get(target.domain_id or '', ''),
                status=row['status'],
                origin='import',
                origin_system='HT-FHIR',
                source='HT-FHIR',
                occurrence_count=row.get('occurrence_count', 0),
            ))
        if not options['dry_run'] and pending:
            SourceCodeConceptMapping.objects.bulk_create(
                pending, batch_size=options['batch_size'], ignore_conflicts=True,
            )
        verb = 'Would create' if options['dry_run'] else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {stats["created"]:,}; existing {stats["existing"]:,}; '
            f'missing target {stats["missing"]:,}; unmapped (skipped) {stats["unmapped"]:,}.'
        ))

    @staticmethod
    def _read_artifact(path):
        text = path.read_text()
        mappings = []
        for line in text.splitlines():
            if not line.startswith('| ') or line.startswith('| ---') or 'Source system' in line:
                continue
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            if len(cells) != 8:
                continue
            source_vocab, source_code, target_vocab, target_code, domain, status, origins, candidate_targets = cells
            mappings.append({
                'source_vocabulary_id': source_vocab,
                'source_code': source_code.replace('\\|', '|'),
                'source_code_description': '',
                'target_vocabulary_id': target_vocab,
                'target_concept_code': target_code,
                'domain_id': domain,
                'status': status,
                'origins': [origin for origin in origins.split(', ') if origin],
                'occurrence_count': int(candidate_targets) if candidate_targets.isdigit() else 0,
            })
        return mappings
