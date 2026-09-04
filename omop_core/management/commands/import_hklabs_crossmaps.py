"""Import curated HK-LABS LOINC mappings into SourceCodeConceptMapping.

Reads the loinc_common.json and curated_aliases_manual.json files from
the hk-labs project and creates approved SCCM rows with:
  - source_vocabulary_id = ''  (Uncoded)
  - origin_system = 'HK-LABS'
  - source = 'HK-LABS'
  - status = 'approved'

Each source code is the lab test short name (lowercased, normalised) and
the target is the LOINC concept looked up in the OMOP concept table.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


DEFAULT_HK_LABS_ROOT = Path.home() / 'hk-labs'
LOINC_COMMON = 'backend/apps/labs/data/loinc_common.json'
MANUAL_ALIASES = 'backend/apps/labs/fixtures/curated_aliases_manual.json'


class Command(BaseCommand):
    help = 'Import curated HK-LABS LOINC mappings into SCCM as approved rows.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hk-labs-root', default=str(DEFAULT_HK_LABS_ROOT),
            help='Path to the hk-labs repository root.',
        )
        parser.add_argument('--dry-run', action='store_true', help='Report without writing rows.')
        parser.add_argument('--batch-size', type=int, default=500, help='Rows per bulk insert batch.')

    def handle(self, **options):
        root = Path(options['hk_labs_root'])
        mappings = self._read_mappings(root)
        self.stdout.write(f'Read {len(mappings)} unique LOINC mappings from hk-labs.')

        # Look up LOINC concepts in OMOP
        loinc_codes = {m['loinc_code'] for m in mappings}
        concepts = {
            c.concept_code: c
            for c in Concept.objects.filter(
                vocabulary_id='LOINC',
                concept_code__in=loinc_codes,
                standard_concept='S',
            ).only('concept_id', 'concept_code', 'vocabulary_id', 'domain_id')
        }

        # Check existing SCCM rows (uncoded source)
        existing = set(
            SourceCodeConceptMapping.objects.filter(
                source_vocabulary_id='',
                source_code__in={m['source_code'] for m in mappings},
            ).values_list('source_vocabulary_id', 'source_code')
        )

        pending = []
        stats = {'created': 0, 'existing': 0, 'missing': 0}
        for m in mappings:
            concept = concepts.get(m['loinc_code'])
            if not concept:
                stats['missing'] += 1
                self.stdout.write(self.style.WARNING(
                    f'  LOINC {m["loinc_code"]} ({m["source_code"]}) not found as standard concept'
                ))
                continue
            key = ('', m['source_code'])
            if key in existing:
                stats['existing'] += 1
                continue
            stats['created'] += 1
            pending.append(SourceCodeConceptMapping(
                source_vocabulary_id='',
                source_code=m['source_code'],
                source_code_description=m['description'],
                domain_id=concept.domain_id or 'Measurement',
                target_concept=concept,
                destination_vocabulary_id='LOINC',
                omop_table=DOMAIN_TO_TABLE.get(concept.domain_id or 'Measurement', 'measurement'),
                status='approved',
                origin='import',
                origin_system='HK-LABS',
                source='HK-LABS',
                occurrence_count=0,
            ))

        if not options['dry_run'] and pending:
            SourceCodeConceptMapping.objects.bulk_create(
                pending, batch_size=options['batch_size'], ignore_conflicts=True,
            )

        verb = 'Would create' if options['dry_run'] else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {stats["created"]:,}; existing {stats["existing"]:,}; '
            f'missing target {stats["missing"]:,}.'
        ))

    @staticmethod
    def _read_mappings(root):
        """Read loinc_common.json and curated_aliases_manual.json, returning
        a deduplicated list of {loinc_code, source_code, description}."""
        seen = set()
        mappings = []

        # loinc_common.json — source_code is the short name
        common_path = root / LOINC_COMMON
        if not common_path.exists():
            raise CommandError(f'Not found: {common_path}')
        data = json.loads(common_path.read_text())
        for entry in data['codes']:
            code = entry['loinc_code']
            if code in seen:
                continue
            seen.add(code)
            mappings.append({
                'loinc_code': code,
                'source_code': entry['loinc_short_name'].lower().strip(),
                'description': entry['loinc_short_name'],
            })

        # curated_aliases_manual.json — source_code is the alias
        manual_path = root / MANUAL_ALIASES
        if manual_path.exists():
            for entry in json.loads(manual_path.read_text()):
                code = entry['loinc_num']
                if code in seen:
                    continue
                seen.add(code)
                mappings.append({
                    'loinc_code': code,
                    'source_code': entry['alias'].lower().strip(),
                    'description': entry['alias'],
                })

        return mappings
