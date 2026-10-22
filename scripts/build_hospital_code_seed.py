#!/usr/bin/env python3
"""Build the deployable HealthTree hospital-code seed artifact.

This is an operator-side build tool.  It deliberately requires pyarrow while
the generated ZIP is JSONL and can be consumed by the production runtime using
only Python's standard library.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


SCHEMA_VERSION = 1
MEMBER_NAME = 'hospital_source_codes.jsonl'
SOURCE_CODE_MAX = 100
SYSTEM_MAX = 255

PRIMARY_COLUMNS = [
    'resource_type', 'field_path', 'coding_system', 'coding_code',
    'source_label', 'sample_display', 'sample_codeable_concept_text',
    'n_records', 'n_patients', 'n_codings', 'label_group_records_if_mapped',
    'n_records_with_unit', 'pct_records_with_unit',
    'n_records_with_reference_range', 'pct_records_with_reference_range',
    'reference_range_low_p50', 'reference_range_high_p50',
    'unit_from_reference_range_top', 'category_top', 'category_mix',
    'pct_value_quantity', 'pct_value_codeable_concept', 'pct_value_string',
]
UNIT_COLUMNS = [
    'coding_system', 'coding_code', 'unit_display', 'unit_ucum', 'n_records',
    'n_patients', 'n_values', 'is_suppressed', 'value_min', 'value_p5',
    'value_p25', 'value_p50', 'value_p75', 'value_p95', 'value_max',
]
FACILITY_COLUMNS = [
    'coding_system', 'coding_code', 'facility_name', 'facility_id',
    'facility_level', 'attribution_method', 'confidence',
    'low_confidence_reason', 'facility_name_alt', 'facility_alias',
    'parent_name', 'parent_id', 'facility_rank', 'n_records', 'n_patients',
    'n_codings',
]
NIKITA_COLUMNS = {
    'code_system', 'code', 'raw_unit', 'raw_quantity_code', 'raw_unit_system',
    'ucum_unit', 'ucum_source', 'unit_verdict', 'unit_verdict_reason',
    'records', 'numeric_records', 'patients',
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def text(value) -> str:
    return str(value or '').strip()


def count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def hospital_vendor(system: str) -> str:
    value = text(system).casefold().rstrip('/')
    if value.startswith('urn:oid:1.2.840.114350.') or value.startswith(
        ('http://open.epic.com/', 'https://open.epic.com/')
    ):
        return 'EPIC'
    if value.startswith(('http://fhir.cerner.com/', 'https://fhir.cerner.com/')) \
            and '/codeset/' in value:
        return 'CERNER'
    return ''


def source_key(system, code):
    system = text(system).rstrip('/')
    code = text(code)
    if not hospital_vendor(system) or not code or len(system) > SYSTEM_MAX:
        return None
    return system, code[:SOURCE_CODE_MAX]


def parquet_rows(path: Path, columns, batch_size=10_000):
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise SystemExit('pyarrow is required to build the seed artifact') from exc
    source = parquet.ParquetFile(path)
    missing = sorted(set(columns) - set(source.schema_arrow.names))
    if missing:
        raise SystemExit(f'{path} is missing columns: {", ".join(missing)}')
    for batch in source.iter_batches(batch_size=batch_size, columns=columns):
        yield from batch.to_pylist()


def configure_django(repo: Path):
    sys.path.insert(0, str(repo))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'promop.settings')
    os.environ.setdefault('DEBUG', 'True')
    os.environ.setdefault('SECRET_KEY', 'hospital-seed-build-only')
    import django
    django.setup()


def create_evidence_db(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript('''
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE facility (
            system TEXT NOT NULL, code TEXT NOT NULL,
            name TEXT NOT NULL, facility_id TEXT NOT NULL,
            level TEXT NOT NULL, method TEXT NOT NULL,
            confidence TEXT NOT NULL, reason TEXT NOT NULL,
            alternate_name TEXT NOT NULL, alias TEXT NOT NULL,
            parent_name TEXT NOT NULL, parent_id TEXT NOT NULL,
            rank INTEGER NOT NULL, records INTEGER NOT NULL,
            patients INTEGER NOT NULL, codings INTEGER NOT NULL,
            PRIMARY KEY (
                system, code, name, facility_id, level, method, confidence,
                reason, alternate_name, alias, parent_name, parent_id
            )
        ) WITHOUT ROWID;
        CREATE TABLE unit (
            system TEXT NOT NULL, code TEXT NOT NULL,
            display TEXT NOT NULL, raw_code TEXT NOT NULL,
            raw_system TEXT NOT NULL, normalized TEXT NOT NULL,
            normalized_source TEXT NOT NULL, verdict TEXT NOT NULL,
            reason TEXT NOT NULL, records INTEGER NOT NULL,
            patients INTEGER NOT NULL, numeric_records INTEGER NOT NULL,
            PRIMARY KEY (
                system, code, display, raw_code, raw_system, normalized,
                normalized_source, verdict, reason
            )
        ) WITHOUT ROWID;
    ''')
    return connection


def load_facilities(connection, rows, source_keys):
    sql = '''
        INSERT INTO facility VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            rank = MIN(rank, excluded.rank),
            records = records + excluded.records,
            patients = patients + excluded.patients,
            codings = codings + excluded.codings
    '''
    batch = []
    accepted = 0
    for raw in rows:
        key = source_key(raw.get('coding_system'), raw.get('coding_code'))
        name = text(raw.get('facility_name'))
        if key not in source_keys or not name:
            continue
        batch.append((
            *key, name, text(raw.get('facility_id')),
            text(raw.get('facility_level')), text(raw.get('attribution_method')),
            text(raw.get('confidence')), text(raw.get('low_confidence_reason')),
            text(raw.get('facility_name_alt')), text(raw.get('facility_alias')),
            text(raw.get('parent_name')), text(raw.get('parent_id')),
            count(raw.get('facility_rank')) or 2_147_483_647,
            count(raw.get('n_records')), count(raw.get('n_patients')),
            count(raw.get('n_codings')),
        ))
        accepted += 1
        if len(batch) == 10_000:
            connection.executemany(sql, batch)
            batch.clear()
    if batch:
        connection.executemany(sql, batch)
    connection.commit()
    return accepted


def load_nikita_units(connection, path: Path, source_keys):
    sql = '''
        INSERT INTO unit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            records = records + excluded.records,
            patients = patients + excluded.patients,
            numeric_records = numeric_records + excluded.numeric_records
    '''
    accepted = 0
    batch = []
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        missing = sorted(NIKITA_COLUMNS - set(reader.fieldnames or ()))
        if missing:
            raise SystemExit(f'{path} is missing columns: {", ".join(missing)}')
        for raw in reader:
            key = source_key(raw.get('code_system'), raw.get('code'))
            display = text(raw.get('raw_unit'))
            raw_code = text(raw.get('raw_quantity_code'))
            if key not in source_keys or not (display or raw_code):
                continue
            batch.append((
                *key, display, raw_code, text(raw.get('raw_unit_system')),
                text(raw.get('ucum_unit')), text(raw.get('ucum_source')),
                text(raw.get('unit_verdict')), text(raw.get('unit_verdict_reason')),
                count(raw.get('records')), count(raw.get('patients')),
                count(raw.get('numeric_records')),
            ))
            accepted += 1
            if len(batch) == 10_000:
                connection.executemany(sql, batch)
                batch.clear()
        if batch:
            connection.executemany(sql, batch)
    connection.commit()
    return accepted


def grouped_rows(cursor):
    current_key = None
    current = []
    for row in cursor:
        key = row[:2]
        if current_key is not None and key != current_key:
            yield current_key, current
            current = []
        current_key = key
        current.append(row[2:])
    if current_key is not None:
        yield current_key, current


class GroupLookup:
    def __init__(self, groups):
        self.groups = iter(groups)
        self.current = next(self.groups, None)

    def get(self, key):
        while self.current is not None and self.current[0] < key:
            self.current = next(self.groups, None)
        if self.current is not None and self.current[0] == key:
            value = self.current[1]
            self.current = next(self.groups, None)
            return value
        return []


def facility_objects(rows):
    return [{
        'name': row[0], 'id': row[1], 'level': row[2],
        'attribution_method': row[3], 'confidence': row[4],
        'low_confidence_reason': row[5], 'alternate_name': row[6],
        'alias': row[7], 'parent_name': row[8], 'parent_id': row[9],
        'rank': row[10], 'records': row[11], 'patients': row[12],
        'codings': row[13],
    } for row in rows]


def merged_units(alex_units, rows, *, unit_source):
    # Alex is the distribution source. Nikita is the authoritative unit
    # parsing/validation source. Match on the two untouched FHIR strings.
    distributions = {
        (text(unit.get('display')), text(unit.get('code'))): unit
        for unit in alex_units
    }
    result = []
    matched = set()
    for row in rows:
        display, raw_code = row[0], row[1]
        evidence = {
            'display': display, 'code': raw_code, 'system': row[2],
            'normalized': row[3], 'normalized_source': row[4],
            'verdict': row[5], 'verdict_reason': row[6],
            'count': row[7], 'patients': row[8], 'values': row[9],
            'source': unit_source,
        }
        alex = distributions.get((display, raw_code))
        if alex:
            matched.add((display, raw_code))
            for field in ('distribution', 'suppressed'):
                if field in alex:
                    evidence[field] = alex[field]
            evidence['distribution_source'] = 'alex-unmapped-source-codes-v2'
        result.append({key: value for key, value in evidence.items() if value not in ('', None, 0) or key == 'count'})
    for key, alex in distributions.items():
        if key in matched:
            continue
        result.append({**alex, 'source': 'alex-unmapped-source-codes-v2'})
    result.sort(key=lambda row: (-count(row.get('count')), text(row.get('display')), text(row.get('code'))))
    return result


def write_jsonl(path, build, connection, *, unit_source):
    facilities = GroupLookup(grouped_rows(connection.execute('''
        SELECT system, code, name, facility_id, level, method, confidence,
               reason, alternate_name, alias, parent_name, parent_id, rank,
               records, patients, codings
        FROM facility ORDER BY system, code, records DESC, rank, name
    ''')))
    units = GroupLookup(grouped_rows(connection.execute('''
        SELECT system, code, display, raw_code, raw_system, normalized,
               normalized_source, verdict, reason, records, patients,
               numeric_records
        FROM unit ORDER BY system, code, records DESC, display, raw_code
    ''')))
    counts = {'total': 0, 'epic': 0, 'cerner': 0, 'with_facilities': 0, 'with_units': 0}
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        for key in sorted(build.rows):
            row = build.rows[key]
            facility_list = facility_objects(facilities.get(key))
            unit_list = merged_units(
                row.source_unit_evidence, units.get(key), unit_source=unit_source,
            )
            metadata = dict(row.source_metadata)
            if facility_list:
                metadata['facilities'] = facility_list
                metadata['facility_count'] = len(facility_list)
                counts['with_facilities'] += 1
            if unit_list:
                counts['with_units'] += 1
            vendor = hospital_vendor(row.source_vocabulary_id)
            counts[vendor.lower()] += 1
            counts['total'] += 1
            payload = {
                'source_vocabulary_id': row.source_vocabulary_id,
                'source_code': row.source_code,
                'source_code_description': row.source_code_description,
                'occurrence_count': row.occurrence_count,
                'source_group_occurrence_count': row.source_group_occurrence_count,
                'source_metadata': metadata,
                'domain_id': row.domain_id,
                'omop_table': row.omop_table,
                'source_unit_evidence': unit_list,
            }
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--alex-zip', required=True, type=Path)
    parser.add_argument('--nikita-csv', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--artifact-identity', required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    for source in (args.alex_zip, args.nikita_csv):
        if not source.is_file():
            parser.error(f'file not found: {source}')
    if len(args.artifact_identity) > 50:
        parser.error('--artifact-identity must be at most 50 characters')
    configure_django(args.repo)
    from omop_core.management.commands.import_hospital_source_codes import parquet_rows as app_parquet_rows
    from omop_core.services.hospital_code_backfill import build_inventory

    with tempfile.TemporaryDirectory(prefix='promop-hospital-seed-build-') as raw_tmp:
        temp = Path(raw_tmp)
        with ZipFile(args.alex_zip) as archive:
            wanted = {
                'unmapped_source_codes_v2.parquet',
                'unmapped_source_code_facilities_v2.parquet',
                'unmapped_source_code_value_distribution_v2.parquet',
            }
            members = {Path(name).name: name for name in archive.namelist() if Path(name).name in wanted}
            missing = sorted(wanted - set(members))
            if missing:
                raise SystemExit(f'Alex archive is missing: {", ".join(missing)}')
            for basename, member in members.items():
                destination = temp / basename
                with archive.open(member) as source, destination.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)

        print('Building Alex Epic/Cerner cohort...', flush=True)
        build = build_inventory(
            app_parquet_rows(temp / 'unmapped_source_codes_v2.parquet', PRIMARY_COLUMNS, 10_000),
            unit_rows=app_parquet_rows(
                temp / 'unmapped_source_code_value_distribution_v2.parquet', UNIT_COLUMNS, 10_000,
            ),
            evidence_source='alex-unmapped-source-codes-v2',
        )
        print(f'  {len(build.rows):,} source keys', flush=True)
        evidence_db = create_evidence_db(temp / 'evidence.sqlite3')
        keys = set(build.rows)
        facility_rows = load_facilities(
            evidence_db,
            parquet_rows(temp / 'unmapped_source_code_facilities_v2.parquet', FACILITY_COLUMNS),
            keys,
        )
        print(f'  accepted {facility_rows:,} facility rows', flush=True)
        nikita_rows = load_nikita_units(evidence_db, args.nikita_csv, keys)
        print(f'  accepted {nikita_rows:,} Nikita unit rows', flush=True)

        jsonl = temp / MEMBER_NAME
        counts = write_jsonl(
            jsonl, build, evidence_db,
            unit_source=f'nikita-{args.nikita_csv.stem}',
        )
        member_sha = sha256_file(jsonl)
        manifest = {
            'schema_version': SCHEMA_VERSION,
            'artifact_identity': args.artifact_identity,
            'member': MEMBER_NAME,
            'member_sha256': member_sha,
            'counts': counts,
            'sources': {
                'alex': {'filename': args.alex_zip.name, 'sha256': sha256_file(args.alex_zip)},
                'nikita': {'filename': args.nikita_csv.name, 'sha256': sha256_file(args.nikita_csv)},
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(args.output, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(jsonl, MEMBER_NAME)
            archive.writestr('manifest.json', json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print(json.dumps({
        'path': str(args.output), 'sha256': sha256_file(args.output),
        'bytes': args.output.stat().st_size, **counts,
    }, indent=2, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
