"""Backfill source-side metadata on SourceCodeConceptMapping rows.

Populates ``source_concept``, ``source_code_description``, and
``umls_source_name`` for existing rows that are missing them, using batched
queries grouped by vocabulary — no N+1.

Idempotent: re-running updates ``umls_source_name`` to the current UMLS
preferred name but never overwrites a non-blank ``source_code_description``
unless ``--force-description`` is passed.
"""
import logging

from django.core.management.base import BaseCommand
from django.db.models import Q

from omop_core.models import Concept, SourceCodeConceptMapping, UmlsSourceCode
from omop_core.services.mapping_suggestions import VOCAB_TO_UMLS_ROOT

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Backfill source_concept, source_code_description, and umls_source_name on SCCM rows.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing.',
        )
        parser.add_argument(
            '--force-description', action='store_true',
            help='Overwrite non-blank source_code_description with OMOP concept name.',
        )
        parser.add_argument(
            '--batch-size', type=int, default=500,
            help='Rows per bulk_update batch (default 500).',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        force_desc = options['force_description']
        batch_size = options['batch_size']

        # All SCCM rows with a source vocabulary that could yield enrichment.
        rows = list(
            SourceCodeConceptMapping.objects
            .filter(source_vocabulary_id__gt='')
            .select_related('source_concept')
        )
        if not rows:
            self.stdout.write('No rows with a source vocabulary found.')
            return

        # ── 1. Batch-lookup OMOP source concepts ────────────────────────────
        # Group rows by vocabulary, then one query per vocabulary.
        by_vocab = {}
        for row in rows:
            by_vocab.setdefault(row.source_vocabulary_id, []).append(row)

        concept_map = {}  # (vocabulary_id, code_upper) → Concept
        for vocab_id, vocab_rows in by_vocab.items():
            codes = list({r.source_code.upper() for r in vocab_rows})
            concepts = Concept.objects.filter(
                vocabulary_id=vocab_id,
                concept_code__in=codes,  # case-sensitive; codes are stored normalised
            )
            for c in concepts:
                concept_map[(vocab_id, c.concept_code.upper())] = c

        # ── 2. Batch-lookup UMLS preferred names ────────────────────────────
        umls_name_map = {}  # (root_source, code) → preferred name
        for vocab_id, vocab_rows in by_vocab.items():
            umls_root = VOCAB_TO_UMLS_ROOT.get(vocab_id)
            if not umls_root:
                continue
            codes = list({r.source_code for r in vocab_rows})
            pref_rows = (
                UmlsSourceCode.objects
                .filter(root_source=umls_root, code__in=codes, is_preferred=True)
                .values_list('code', 'name')
            )
            for code, name in pref_rows:
                umls_name_map[(umls_root, code)] = name

        # ── 3. Apply enrichment ─────────────────────────────────────────────
        to_update = []
        stats = {'source_concept': 0, 'description': 0, 'umls_name': 0}

        for row in rows:
            changed = False
            key = (row.source_vocabulary_id, row.source_code.upper())
            concept = concept_map.get(key)

            # source_concept
            if concept and row.source_concept_id != concept.concept_id:
                row.source_concept = concept
                stats['source_concept'] += 1
                changed = True

            # umls_source_name — always set to current canonical value
            umls_root = VOCAB_TO_UMLS_ROOT.get(row.source_vocabulary_id)
            umls_name = umls_name_map.get((umls_root, row.source_code), '') if umls_root else ''
            if umls_name and row.umls_source_name != umls_name:
                row.umls_source_name = umls_name
                stats['umls_name'] += 1
                changed = True

            # source_code_description
            best_desc = (concept.concept_name if concept else '') or umls_name
            if best_desc and (force_desc or not row.source_code_description):
                if row.source_code_description != best_desc[:255]:
                    row.source_code_description = best_desc[:255]
                    stats['description'] += 1
                    changed = True

            if changed:
                to_update.append(row)

        verb = 'Would update' if dry_run else 'Updated'
        self.stdout.write(
            f'{verb} {len(to_update)} of {len(rows)} rows: '
            f'{stats["source_concept"]} source_concept, '
            f'{stats["description"]} description, '
            f'{stats["umls_name"]} umls_source_name.'
        )

        if dry_run or not to_update:
            return

        SourceCodeConceptMapping.objects.bulk_update(
            to_update,
            ['source_concept', 'source_code_description', 'umls_source_name'],
            batch_size=batch_size,
        )
        self.stdout.write(self.style.SUCCESS('Done.'))
