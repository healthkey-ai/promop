"""Build complete ICD-10 mapping evidence from an original Athena export.

This has no database dependency. All validity intervals are retained, including
future/expired edges, so deployment-time filtering sees every alternative.
"""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path

CONCEPT_FIELDS = ['concept_id', 'vocabulary_id', 'concept_code', 'domain_id',
                  'standard_concept', 'valid_start_date', 'valid_end_date', 'invalid_reason']
RELATIONSHIP_FIELDS = ['concept_id_1', 'concept_id_2', 'valid_start_date', 'valid_end_date', 'invalid_reason']


def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream, delimiter='\t')


def fingerprint(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pack(row, fields):
    return [int(row[key]) if key in ('concept_id', 'concept_id_1', 'concept_id_2')
            else row[key] for key in fields]


def build(directory, as_of):
    paths = [directory / name for name in ('CONCEPT.csv', 'CONCEPT_RELATIONSHIP.csv')]
    fingerprints = {p.name: fingerprint(p) for p in paths}
    sources = [pack(row, CONCEPT_FIELDS) for row in rows(paths[0])
               if row['vocabulary_id'] in ('ICD10', 'ICD10CM')]
    source_ids = {row[0] for row in sources}
    if not sources or len(source_ids) != len(sources):
        raise ValueError('Source concepts must be present and have unique IDs')
    print(f'Found {len(sources):,} ICD10/ICD10CM source concepts.', flush=True)
    relationships = [pack(row, RELATIONSHIP_FIELDS) for row in rows(paths[1])
                     if row['relationship_id'] == 'Maps to' and int(row['concept_id_1']) in source_ids]
    target_ids = {row[1] for row in relationships}
    targets = [pack(row, CONCEPT_FIELDS) for row in rows(paths[0]) if int(row['concept_id']) in target_ids]
    if not relationships or len({row[0] for row in targets}) != len(targets):
        raise ValueError('Relationships must be present and target IDs unique')
    if fingerprints != {p.name: fingerprint(p) for p in paths}:
        raise ValueError('Export changed during snapshot construction')
    return dict(schema_version=1, as_of=as_of, input_sha256=fingerprints,
                concept_fields=CONCEPT_FIELDS, relationship_fields=RELATIONSHIP_FIELDS,
                sources=sorted(sources), targets=sorted(targets), relationships=sorted(relationships),
                source_count=len(sources), target_count=len(targets), relationship_count=len(relationships))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--as-of', required=True, help='Original export date (YYYY-MM-DD)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    payload = build(args.path.expanduser(), args.as_of)
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(compressed)
    print(json.dumps({key: payload[key] for key in ('source_count', 'target_count', 'relationship_count')}))
    print(f'{len(compressed):,} compressed bytes; sha256={hashlib.sha256(compressed).hexdigest()}')
