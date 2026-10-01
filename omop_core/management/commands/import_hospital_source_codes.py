"""Import internal Epic/Cerner source-code snapshots without committing them."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.services.hospital_code_backfill import (
    build_inventory,
    upsert_inventory,
)


PRIMARY_COLUMNS = [
    'resource_type', 'field_path', 'coding_system', 'coding_code',
    'source_label', 'sample_display', 'sample_codeable_concept_text',
    'n_records', 'n_patients', 'n_codings', 'label_group_records_if_mapped',
    'n_records_with_unit', 'pct_records_with_unit',
    'n_records_with_reference_range', 'pct_records_with_reference_range',
    'reference_range_low_p50', 'reference_range_high_p50',
    'unit_from_reference_range_top',
    'category_top', 'category_mix', 'pct_value_quantity',
    'pct_value_codeable_concept', 'pct_value_string',
]
UNIT_COLUMNS = [
    'coding_system', 'coding_code', 'unit_display', 'unit_ucum', 'n_records',
    'n_patients', 'n_values', 'is_suppressed', 'value_min', 'value_p5',
    'value_p25', 'value_p50', 'value_p75', 'value_p95', 'value_max',
]


def parquet_rows(path, columns, batch_size):
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise CommandError(
            'Parquet import requires pyarrow in the invoking environment. '
            'Install it locally; it is intentionally not a production runtime dependency.'
        ) from exc
    try:
        parquet_file = parquet.ParquetFile(path)
    except Exception as exc:
        raise CommandError(f'Cannot open Parquet file {path}: {exc}') from exc
    missing = sorted(set(columns) - set(parquet_file.schema_arrow.names))
    if missing:
        raise CommandError(f'{path} is missing column(s): {", ".join(missing)}')
    for batch in parquet_file.iter_batches(batch_size=batch_size, columns=columns):
        yield from batch.to_pylist()


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        required = {'coding_system', 'coding_code', 'source_label', 'fhir_n_records'}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise CommandError(f'{path} is missing column(s): {", ".join(missing)}')
        yield from reader


class Command(BaseCommand):
    help = (
        'Import exact Epic/Cerner source systems from the internal HealthTree '
        'unmapped-code Parquet, optionally unioning supplement-only CSV keys.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--codes-file', required=True, type=Path)
        parser.add_argument('--units-file', type=Path)
        parser.add_argument('--supplemental-csv', type=Path)
        parser.add_argument('--provenance', default='healthtree-unmapped-v2')
        parser.add_argument('--batch-size', type=int, default=2_000)
        parser.add_argument('--expected-keys', type=int)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, **options):
        codes_path = options['codes_file']
        units_path = options['units_file']
        supplemental_path = options['supplemental_csv']
        for path in (codes_path, units_path, supplemental_path):
            if path is not None and not path.is_file():
                raise CommandError(f'File not found: {path}')
        batch_size = options['batch_size']
        if batch_size < 1 or batch_size > 20_000:
            raise CommandError('--batch-size must be between 1 and 20,000')

        build = build_inventory(
            parquet_rows(codes_path, PRIMARY_COLUMNS, batch_size),
            unit_rows=(
                parquet_rows(units_path, UNIT_COLUMNS, batch_size)
                if units_path else ()
            ),
            supplemental_rows=(csv_rows(supplemental_path) if supplemental_path else ()),
            evidence_source=options['provenance'],
        )
        expected = options['expected_keys']
        if expected is not None and len(build.rows) != expected:
            raise CommandError(
                f'Inventory has {len(build.rows):,} keys; expected {expected:,}. '
                'No database changes were made.'
            )
        outcome = upsert_inventory(
            build,
            provenance=options['provenance'],
            dry_run=options['dry_run'],
            batch_size=batch_size,
        )

        self.stdout.write('Inventory:')
        for key, value in sorted(build.stats.items()):
            self.stdout.write(f'  {key}: {value:,}')
        mode = 'Dry run' if outcome['dry_run'] else 'Imported'
        self.stdout.write(self.style.SUCCESS(
            f"{mode}: {outcome['total']:,} total; {outcome['new']:,} new; "
            f"{outcome['existing']:,} existing; {outcome['updated']:,} updated."
        ))
