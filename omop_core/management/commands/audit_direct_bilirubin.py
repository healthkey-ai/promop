"""Read-only audit of direct bilirubin's stored pair and projected unit (#2318).

The pair (`serum_bilirubin_level_direct` + `_units`) is the stored fact, the
unitless `serum_bilirubin_level` is the normalised mg/dL mirror, and OMOP should
carry the reported unit. Historical rows predate that contract, so this command
reports the classes that need a human review before any repair:

- a value without a unit, or a unit outside `MG/DL`/`UMOL/L`;
- an alias that is missing, stale or set while the pair is empty;
- an alias still carrying a pending edit (the old write path allowed it);
- the latest direct-bilirubin Measurement still recorded under the recipe's
  static `mg/dL` while the pair says `UMOL/L` and holds the same number (the
  signature of the old projection), or with no/unknown unit.

It never writes. `--json` emits the counts and up to `--sample` rows per class
(person ids only), which is what a review or a dry-run plan consumes.
"""
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db.models import Q

from omop_core.models import Measurement, PatientRecord
from omop_core.services import direct_bilirubin_units as units

LOINC_DIRECT = '1968-7'


def _same_number(left, right):
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) < Decimal('0.0000001')
    except (InvalidOperation, TypeError, ValueError):
        return False


def _latest_direct_measurements(person_ids):
    """person_id -> newest non-erroneous direct-bilirubin Measurement."""
    rows = (
        Measurement.objects
        .filter(is_erroneous=False)
        .filter(
            Q(measurement_concept__concept_code=LOINC_DIRECT)
            | Q(measurement_source_value=LOINC_DIRECT),
        )
        .order_by('person_id', '-measurement_date', '-measurement_id')
        .values(
            'person_id', 'measurement_date', 'measurement_id',
            'value_as_number', 'unit_source_value', 'unit_concept__concept_code',
        )
    )
    if person_ids is not None:
        rows = rows.filter(person_id__in=person_ids)
    latest = {}
    for row in rows.iterator():
        latest.setdefault(row['person_id'], row)
    return latest


class Command(BaseCommand):
    help = 'Report direct-bilirubin pair/alias/projection rows that need review (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--person-id', type=int, help='audit one patient')
        parser.add_argument('--sample', type=int, default=5,
                            help='examples per class (default 5)')
        parser.add_argument('--limit', type=int, default=0,
                            help='stop after N records (0 = all)')
        parser.add_argument('--json', action='store_true',
                            help='machine-readable output')

    def handle(self, *args, **options):
        sample_size = max(options['sample'], 0)
        records = PatientRecord.objects.only(
            'person_id', 'serum_bilirubin_level_direct',
            'serum_bilirubin_level_direct_units', 'serum_bilirubin_level',
            'user_edited_fields',
        ).order_by('person_id')
        if options['person_id'] is not None:
            records = records.filter(person_id=options['person_id'])
        if options['limit']:
            records = records[:options['limit']]
        person_ids = None
        if options['person_id'] is not None:
            person_ids = {options['person_id']}
        latest = _latest_direct_measurements(person_ids)

        counts = defaultdict(int)
        samples = defaultdict(list)
        scanned = 0

        def flag(bucket, record, detail=None):
            counts[bucket] += 1
            if len(samples[bucket]) < sample_size:
                samples[bucket].append({
                    'person_id': record.person_id,
                    'value': str(record.serum_bilirubin_level_direct)
                    if record.serum_bilirubin_level_direct is not None else None,
                    'unit': record.serum_bilirubin_level_direct_units,
                    'alias': str(record.serum_bilirubin_level)
                    if record.serum_bilirubin_level is not None else None,
                    'detail': detail,
                })

        for record in records.iterator():
            scanned += 1
            value = record.serum_bilirubin_level_direct
            unit = record.serum_bilirubin_level_direct_units
            alias = record.serum_bilirubin_level
            code = units.normalize_unit(unit)
            if value is None and alias is None and code is None:
                continue
            if 'serum_bilirubin_level' in (record.user_edited_fields or []):
                flag('pending_alias', record,
                     'the unitless mirror still carries a pending edit')
            if value is not None and not (unit or '').strip():
                flag('pair_without_unit', record,
                     'no unit; the old reader assumes mg/dL')
            elif value is not None and (unit or '').strip() and code is None:
                flag('unknown_unit', record, f'unit {unit!r} is not convertible')
            if value is None and alias is not None:
                flag('orphan_alias', record,
                     'alias set without a source pair; scale is not established')
            if value is not None and code is not None:
                recognised, expected = units.alias_from_pair(value, code)
                if recognised and not _same_number(alias, expected):
                    flag('alias_mismatch', record,
                         f'alias should be {expected} mg/dL')
            measurement = latest.get(record.person_id)
            if measurement is None or value is None:
                continue
            measured_unit = (
                measurement['unit_source_value']
                or measurement['unit_concept__concept_code']
            )
            measured_code = units.normalize_unit(measured_unit)
            if not (measured_unit or '').strip():
                flag('projection_unitless', record,
                     'latest Measurement carries no unit')
            elif measured_code is None:
                flag('projection_unknown_unit', record,
                     f'latest Measurement unit {measured_unit!r} is not convertible')
            elif (code == 'UMOL/L' and measured_code == 'MG/DL'
                  and _same_number(measurement['value_as_number'], value)):
                flag('projection_raw_unit', record,
                     'latest Measurement keeps the raw number under mg/dL; '
                     'the old projection likely wrote it')

        report = {
            'scanned': scanned,
            'counts': dict(sorted(counts.items())),
            'samples': {bucket: rows for bucket, rows in sorted(samples.items())},
            'read_only': True,
        }
        if options['json']:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self.stdout.write(f'Scanned {scanned} PatientRecord row(s); read-only audit.')
        if not counts:
            self.stdout.write('No direct-bilirubin rows need review.')
            return
        for bucket in sorted(counts):
            self.stdout.write(f'{bucket}: {counts[bucket]}')
            for row in samples[bucket]:
                self.stdout.write(
                    f"  person_id={row['person_id']} value={row['value']} "
                    f"unit={row['unit']} alias={row['alias']} "
                    f"({row['detail']})")
