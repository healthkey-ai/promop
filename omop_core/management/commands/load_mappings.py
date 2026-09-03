"""Load approved code-to-concept mappings from the consolidated artifact.

Reads ``omop_core/data/code_concept_mappings.json`` (built by
``build_crossmap_artifact``) and creates ``SourceCodeConceptMapping`` rows
for all approved mappings. Target concepts are resolved by
``(vocabulary_id, concept_code)`` at load time — this command must run
after the vocabulary tables are populated.

Also re-attributes any existing SCCM rows with stale ``origin_system``
values (``HK-ETL``, ``etl-cross-map``) to ``HT-One``, since those
mappings are now known to originate from the HT-One/Next repositories.

Idempotent: uses ``bulk_create(ignore_conflicts=True)``.
Called automatically by ``load_athena_vocabularies`` after a successful
vocabulary load.
"""
import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / 'data' / 'code_concept_mappings.json'

# Origin systems that should be re-attributed to HT-One.
_STALE_ORIGIN_SYSTEMS = ('HK-ETL', 'etl-cross-map')


class Command(BaseCommand):
    help = 'Load approved code-to-concept mappings from the bundled artifact into SCCM.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--artifact', default=str(DEFAULT_ARTIFACT),
            help='Path to the code_concept_mappings.json artifact.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report counts without writing.',
        )
        parser.add_argument(
            '--include-proposed', action='store_true',
            help='Also load proposed (ambiguous) mappings. Off by default.',
        )
        parser.add_argument(
            '--batch-size', type=int, default=500,
            help='Rows per bulk_create batch.',
        )

    def handle(self, **options):
        artifact_path = Path(options['artifact'])
        if not artifact_path.exists():
            raise CommandError(f'Artifact not found: {artifact_path}')

        data = json.loads(artifact_path.read_text())
        all_mappings = data.get('mappings', [])

        # Filter to approved only unless --include-proposed.
        if options['include_proposed']:
            mappings = all_mappings
        else:
            mappings = [m for m in all_mappings if m.get('status') == 'approved']

        self.stdout.write(
            f'Artifact has {len(all_mappings):,} total mappings; '
            f'loading {len(mappings):,} ({("approved + proposed" if options["include_proposed"] else "approved only")}).'
        )

        if not mappings:
            self.stdout.write('Nothing to load.')
            return

        # --- Re-attribute stale origin_system values ---
        self._reattribute_stale(options['dry_run'])

        # --- Resolve target concepts ---
        # Collect all (vocabulary_id, concept_code) pairs we need.
        vocab_codes = {}
        for m in mappings:
            vocab = m['target_vocabulary_id']
            code = m['target_concept_code']
            vocab_codes.setdefault(vocab, set()).add(code)

        concepts = {}
        for vocab, codes in vocab_codes.items():
            for c in Concept.objects.filter(
                vocabulary_id=vocab,
                concept_code__in=codes,
            ).only('concept_id', 'concept_code', 'vocabulary_id', 'domain_id'):
                concepts[(vocab, c.concept_code)] = c

        # --- Check existing rows ---
        existing_keys = set(
            SourceCodeConceptMapping.objects.filter(
                source_vocabulary_id__in={m['source_vocabulary_id'] for m in mappings},
            ).values_list('source_vocabulary_id', 'source_code')
        )

        # --- Build new rows ---
        pending = []
        stats = {'created': 0, 'existing': 0, 'missing_concept': 0}

        for m in mappings:
            target_key = (m['target_vocabulary_id'], m['target_concept_code'])
            concept = concepts.get(target_key)
            if not concept:
                stats['missing_concept'] += 1
                if options['verbosity'] >= 2:
                    self.stdout.write(self.style.WARNING(
                        f'  Target concept not found: {target_key[0]}:{target_key[1]} '
                        f'for {m["source_vocabulary_id"]}:{m["source_code"]}'
                    ))
                continue

            sccm_key = (m['source_vocabulary_id'], m['source_code'])
            if sccm_key in existing_keys:
                stats['existing'] += 1
                continue

            domain = m.get('domain_id') or concept.domain_id or 'Measurement'
            origins = m.get('origins', [])
            # Use first origin as origin_system.
            origin_system = origins[0] if origins else 'artifact'

            stats['created'] += 1
            pending.append(SourceCodeConceptMapping(
                source_vocabulary_id=m['source_vocabulary_id'],
                source_code=m['source_code'],
                source_code_description=m.get('source_code_description', '')[:255],
                domain_id=domain,
                target_concept=concept,
                destination_vocabulary_id=m['target_vocabulary_id'],
                omop_table=DOMAIN_TO_TABLE.get(domain, ''),
                status=m.get('status', 'approved'),
                origin='import',
                origin_system=origin_system,
                source=origin_system,
                occurrence_count=0,
            ))

        if not options['dry_run'] and pending:
            SourceCodeConceptMapping.objects.bulk_create(
                pending,
                batch_size=options['batch_size'],
                ignore_conflicts=True,
            )

        verb = 'Would create' if options['dry_run'] else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {stats["created"]:,}; existing {stats["existing"]:,}; '
            f'missing target concept {stats["missing_concept"]:,}.'
        ))

    def _reattribute_stale(self, dry_run):
        """Re-attribute SCCM rows with stale origin_system values to HT-One."""
        count = SourceCodeConceptMapping.objects.filter(
            origin_system__in=_STALE_ORIGIN_SYSTEMS,
        ).count()

        if count == 0:
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Would re-attribute {count:,} rows from {_STALE_ORIGIN_SYSTEMS} '
                f'to HT-One.'
            ))
            return

        updated = SourceCodeConceptMapping.objects.filter(
            origin_system__in=_STALE_ORIGIN_SYSTEMS,
        ).update(origin_system='HT-One', source='HT-One')

        self.stdout.write(self.style.SUCCESS(
            f'Re-attributed {updated:,} rows from {_STALE_ORIGIN_SYSTEMS} to HT-One.'
        ))
