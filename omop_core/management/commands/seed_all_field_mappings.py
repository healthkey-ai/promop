"""Seed FieldConceptMapping with every known concept→PatientRecord field mapping.

Collects mappings from the three hardcoded sources:
- ``_LOINC_LAB_FIELDS`` (patient_record_service.py) — labs, vitals, markers
- ``_BEHAVIOR_MEASUREMENT_FIELDS`` (patient_record_service.py) — behavioral/lifestyle
- ``_ASSERTION_FIELDS`` (patient_record_service.py) — clinical assertions
- ``LAB_FIELD_TO_LOINC`` (mappings.py) — write-side lab/vital mappings
- ``DERIVED_FIELD_TO_CODE`` (mappings.py) — derivation-attributed fields
- ``SUGGESTED_FIELD_CODES`` (mappings.py) — curator-oriented suggestions

After dedup by field_name (first source wins), creates one FieldConceptMapping
row per field with ``status='approved'``.

Idempotent: uses ``get_or_create`` on ``field_name`` and skips fields that
already have a mapping. Supports ``--dry-run``.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.models import Concept, FieldConceptMapping

logger = logging.getLogger(__name__)


def _collect_all_mappings():
    """Return a list of dicts, one per field, deduped by field_name (first wins)."""
    from omop_core.services.patient_record_service import (
        _ASSERTION_FIELDS,
        _BEHAVIOR_MEASUREMENT_FIELDS,
        _LOINC_LAB_FIELDS,
    )
    from omop_core.services.mappings import (
        DERIVED_FIELD_TO_CODE,
        LAB_FIELD_TO_LOINC,
        SUGGESTED_FIELD_CODES,
    )

    seen = set()
    mappings = []

    def _add(field_name, vocab_id, concept_code, source_value, omop_table,
             value_kind, unit=''):
        if field_name in seen:
            return
        seen.add(field_name)
        mappings.append({
            'field_name': field_name,
            'vocabulary_id': vocab_id,
            'concept_code': concept_code,
            'source_value': source_value,
            'omop_table': omop_table,
            'value_kind': value_kind,
            'unit': unit,
        })

    # 1. LAB_FIELD_TO_LOINC — most authoritative for labs (has unit + display).
    #    Only the canonical LOINC per field (no mCODE aliases).
    for field_name, (loinc_code, unit, display) in LAB_FIELD_TO_LOINC.items():
        _add(field_name, 'LOINC', loinc_code, display, 'measurement', 'number',
             unit=unit)

    # 2. _LOINC_LAB_FIELDS — picks up fields not already in LAB_FIELD_TO_LOINC.
    #    Many-to-one (multiple LOINC codes per field), so only the first code
    #    per field is used.
    for loinc_code, (field_name, coerce_fn) in _LOINC_LAB_FIELDS.items():
        kind = 'number' if coerce_fn in (int, float) else 'string'
        _add(field_name, 'LOINC', loinc_code, '', 'measurement', kind)

    # 3. _BEHAVIOR_MEASUREMENT_FIELDS
    for loinc_code, (field_name, coerce_fn) in _BEHAVIOR_MEASUREMENT_FIELDS.items():
        kind = 'number' if coerce_fn in (int, float) else 'string'
        _add(field_name, 'LOINC', loinc_code, '', 'measurement', kind)

    # 4. _ASSERTION_FIELDS
    for code, (field_name, vk) in _ASSERTION_FIELDS.items():
        vocab = 'LOINC' if code[0].isdigit() else 'HKO'
        kind = 'boolean' if vk in ('boolean', 'inverse_boolean') else 'string'
        _add(field_name, vocab, code, '', 'measurement', kind)

    # 5. DERIVED_FIELD_TO_CODE — derivation-attributed fields.
    for field_name, (concept_code, vocab_id, _extractor) in DERIVED_FIELD_TO_CODE.items():
        omop_table = 'observation' if vocab_id == 'SNOMED' else 'measurement'
        _add(field_name, vocab_id, concept_code, '', omop_table, 'string')

    # 6. SUGGESTED_FIELD_CODES — curator-oriented suggestions (lower priority).
    for field_name, (concept_code, vocab_id) in SUGGESTED_FIELD_CODES.items():
        omop_table = 'observation' if vocab_id == 'SNOMED' else 'measurement'
        _add(field_name, vocab_id, concept_code, '', omop_table, 'string')

    return mappings


class Command(BaseCommand):
    help = 'Seed FieldConceptMapping rows from all hardcoded mapping dicts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be created without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        all_mappings = _collect_all_mappings()
        self.stdout.write(f'Collected {len(all_mappings)} field mappings from source dicts.')

        created_count = 0
        skipped_count = 0

        with transaction.atomic():
            for entry in all_mappings:
                field_name = entry['field_name']

                if FieldConceptMapping.objects.filter(field_name=field_name).exists():
                    skipped_count += 1
                    if options['verbosity'] >= 2:
                        self.stdout.write(f'  SKIP {field_name} (already exists)')
                    continue

                if dry_run:
                    created_count += 1
                    self.stdout.write(
                        f'  WOULD CREATE {field_name} → '
                        f'{entry["vocabulary_id"]}:{entry["concept_code"]} '
                        f'({entry["omop_table"]}, {entry["value_kind"]})'
                    )
                    continue

                # Try to resolve the concept FK from the concept table.
                concept = Concept.objects.filter(
                    vocabulary_id=entry['vocabulary_id'],
                    concept_code=entry['concept_code'],
                ).first()

                FieldConceptMapping.objects.create(
                    field_name=field_name,
                    vocabulary_id=entry['vocabulary_id'],
                    concept_code=entry['concept_code'],
                    source_value=entry['source_value'],
                    omop_table=entry['omop_table'],
                    value_kind=entry['value_kind'],
                    unit=entry['unit'],
                    concept=concept,
                    status='approved',
                )
                created_count += 1
                if options['verbosity'] >= 2:
                    self.stdout.write(
                        f'  CREATE {field_name} → '
                        f'{entry["vocabulary_id"]}:{entry["concept_code"]} '
                        f'(concept {"found" if concept else "not loaded"})'
                    )

        prefix = 'DRY RUN: ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Created {created_count}, skipped {skipped_count} '
            f'(total fields: {len(all_mappings)}).'
        ))
