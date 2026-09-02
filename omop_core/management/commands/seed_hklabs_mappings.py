"""Seed SourceCodeConceptMapping rows for hk-labs source codes.

hk-labs extracts lab test names from patient-uploaded PDFs and maps them to
LOINC concepts. The source codes are uncoded free text — raw lab names as they
appear on reports — and the destinations are standard LOINC concepts.

This command pre-populates SCCM with those mappings so that hk-labs can query
promop's ``/api/v1/code-mappings/lookup/`` endpoint instead of maintaining its
own resolution logic.

Three data sources from ~/hk-labs:

1. ``loinc_common.json`` — 111 LOINC entries with display short names.
   Seeded as ``source_vocabulary_id=''`` (uncoded text),
   ``source_code=<short_name_normalized>``.

2. ``lab_catalog.json`` — 37 curated lab tests with display names.
   Seeded as ``source_vocabulary_id=''`` (uncoded text),
   ``source_code=<name_normalized>``.

3. ``curated_aliases_manual.json`` — 8 curated alias text → LOINC mappings.
   Seeded as ``source_vocabulary_id=''``, ``source_code=<alias_normalized>``.

All rows are created with ``status='approved'`` because they represent
reviewed, known-correct mappings from the hk-labs matching logic.

Idempotent: uses ``get_or_create`` on ``(source_vocabulary_id, source_code)``.
Supports ``--dry-run``.
"""

import json
import logging
import re
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.models import Concept, SourceCodeConceptMapping

logger = logging.getLogger(__name__)

# Default path to hk-labs data files.
HKLABS_ROOT = Path.home() / 'hk-labs' / 'backend' / 'apps' / 'labs'
LOINC_COMMON_PATH = HKLABS_ROOT / 'data' / 'loinc_common.json'
LAB_CATALOG_PATH = HKLABS_ROOT / 'fixtures' / 'lab_catalog.json'
CURATED_ALIASES_PATH = HKLABS_ROOT / 'fixtures' / 'curated_aliases_manual.json'

ORIGIN_SYSTEM = 'hk-labs-seed'


def _normalize(text):
    """Lowercase, collapse whitespace, strip punctuation for matching."""
    return re.sub(r'[^a-z0-9 ]', '', text.lower()).strip()


def _load_loinc_common(path):
    """Parse loinc_common.json → list of (short_name, loinc_code, unit)."""
    data = json.loads(path.read_text())
    return [
        (
            entry['loinc_short_name'],
            entry['loinc_code'],
            entry.get('loinc_default_unit', ''),
        )
        for entry in data['codes']
    ]


def _load_lab_catalog(path):
    """Parse lab_catalog.json → list of dicts with name_normalized, name, abbreviation."""
    data = json.loads(path.read_text())
    entries = []
    for item in data:
        fields = item.get('fields', item)
        entries.append({
            'abbreviation': fields['abbreviation'],
            'name': fields['name'],
            'name_normalized': fields['name_normalized'],
        })
    return entries


def _load_curated_aliases(path):
    """Parse curated_aliases_manual.json → list of (alias, loinc_code, note)."""
    data = json.loads(path.read_text())
    return [
        (entry['alias'], entry['loinc_num'], entry.get('note', ''))
        for entry in data
    ]


# Cross-reference: lab_catalog abbreviation → LOINC code (from loinc_common).
# This is the same mapping hk-labs uses in its matching logic.
_CATALOG_LOINC = {
    'wbc': '6690-2', 'hgb': '718-7', 'plt': '777-3', 'anc': '751-8',
    'alc': '731-0', 'hct': '4544-3', 'mcv': '787-2',
    'creatinine': '2160-0', 'egfr': '62238-1', 'crcl': '2164-2',
    'calcium': '17861-6', 'bun': '3094-0', 'ast': '1920-8', 'alt': '1742-6',
    'alp': '6768-6', 'bili_total': '1975-2', 'bili_direct': '1968-7',
    'albumin': '1751-7', 'ldh': '2532-0',
    'mspike_serum': '33358-3', 'mspike_urine': '34366-5',
    'flc_kappa': '36916-5', 'flc_lambda': '33944-0', 'flc_ratio': '48378-4',
    'bmpc': '26450-7', 'b2m': '1952-1',
    'ca_15_3': '6875-9', 'ca_27_29': '17842-6', 'ki67': '85337-4',
    'lvef': '10230-1', 'hba1c': '4548-4',
    'ldl': '13457-7', 'hdl': '2085-9', 'tsh': '3016-3',
    'hiv_ab': '75622-1', 'hbsag': '5195-3', 'hcv_ab': '16128-1',
}


class Command(BaseCommand):
    help = 'Seed SourceCodeConceptMapping rows from hk-labs mapping data.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created without writing.',
        )
        parser.add_argument(
            '--hklabs-root', type=str, default=None,
            help='Override path to hk-labs/backend/apps/labs/ directory.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        root = Path(options['hklabs_root']) if options['hklabs_root'] else HKLABS_ROOT

        loinc_path = root / 'data' / 'loinc_common.json'
        catalog_path = root / 'fixtures' / 'lab_catalog.json'
        aliases_path = root / 'fixtures' / 'curated_aliases_manual.json'

        # Collect all mappings: (source_code, description, loinc_code)
        # All are uncoded text (source_vocabulary_id='') because hk-labs
        # extracts raw text from lab reports, not coded values.
        mappings = {}  # keyed by normalized source_code to dedup

        # 1. LOINC short names from loinc_common.json
        loinc_count = 0
        if loinc_path.exists():
            for short_name, loinc_code, unit in _load_loinc_common(loinc_path):
                normalized = _normalize(short_name)[:100]
                if not normalized:
                    continue
                desc = f'{short_name} ({unit})' if unit else short_name
                if normalized not in mappings:
                    mappings[normalized] = (desc, loinc_code)
                    loinc_count += 1
            self.stdout.write(f'Loaded {loinc_count} short names from loinc_common.json')
        else:
            self.stderr.write(self.style.WARNING(f'Not found: {loinc_path}'))

        # 2. Lab catalog display names (normalized text → LOINC)
        catalog_count = 0
        if catalog_path.exists():
            for entry in _load_lab_catalog(catalog_path):
                loinc_code = _CATALOG_LOINC.get(entry['abbreviation'])
                if not loinc_code:
                    self.stderr.write(self.style.WARNING(
                        f'No LOINC code for catalog entry: {entry["abbreviation"]} — skipping'
                    ))
                    continue
                normalized = _normalize(entry['name_normalized'])[:100]
                if not normalized:
                    continue
                if normalized not in mappings:
                    mappings[normalized] = (entry['name'], loinc_code)
                    catalog_count += 1
            self.stdout.write(f'Loaded {catalog_count} catalog names from lab_catalog.json')
        else:
            self.stderr.write(self.style.WARNING(f'Not found: {catalog_path}'))

        # 3. Curated aliases (uncoded text → LOINC)
        alias_count = 0
        if aliases_path.exists():
            for alias, loinc_code, note in _load_curated_aliases(aliases_path):
                normalized = _normalize(alias)[:100]
                if not normalized:
                    continue
                if normalized not in mappings:
                    mappings[normalized] = (alias, loinc_code)
                    alias_count += 1
            self.stdout.write(f'Loaded {alias_count} curated aliases from curated_aliases_manual.json')
        else:
            self.stderr.write(self.style.WARNING(f'Not found: {aliases_path}'))

        if not mappings:
            self.stderr.write(self.style.ERROR('No mappings found. Check --hklabs-root path.'))
            return

        # Batch-resolve LOINC concepts from the concept table.
        all_loinc_codes = {loinc_code for _, loinc_code in mappings.values()}
        concept_cache = {}
        for c in Concept.objects.filter(
            vocabulary_id='LOINC', concept_code__in=all_loinc_codes,
        ).only('concept_id', 'concept_code', 'vocabulary_id'):
            concept_cache[c.concept_code] = c

        created = existed = unresolved = 0
        with transaction.atomic():
            for src_code, (description, loinc_code) in sorted(mappings.items()):
                target = concept_cache.get(loinc_code)

                if not target:
                    unresolved += 1
                    if options['verbosity'] >= 2:
                        self.stdout.write(
                            f'  UNRESOLVED (uncoded):{src_code} '
                            f'-> LOINC:{loinc_code} (concept not loaded)'
                        )

                if dry_run:
                    status_label = 'MAPPED' if target else 'UNMAPPED'
                    self.stdout.write(
                        f'  [{status_label}] (uncoded):{src_code} '
                        f'-> LOINC:{loinc_code}'
                    )
                    continue

                _, was_created = SourceCodeConceptMapping.objects.get_or_create(
                    source_vocabulary_id='',
                    source_code=src_code,
                    defaults={
                        'domain_id': 'Measurement',
                        'source_code_description': description[:255],
                        'target_concept': target,
                        'destination_vocabulary_id': 'LOINC' if target else '',
                        'omop_table': 'measurement',
                        'status': 'approved' if target else 'proposed',
                        'origin': 'import',
                        'origin_system': ORIGIN_SYSTEM,
                        'source': 'HealthKey',
                        'occurrence_count': 0,
                    },
                )
                if was_created:
                    created += 1
                else:
                    existed += 1

        total = len(mappings)
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: {total} mappings, {total - unresolved} resolved, '
                f'{unresolved} unresolved (concept not loaded).'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Created {created}, existed {existed}, unresolved {unresolved} '
                f'(total: {total}).'
            ))
