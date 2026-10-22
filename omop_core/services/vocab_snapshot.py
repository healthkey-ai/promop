"""The versioned byte contract shared by vocabulary publication and download."""

import hashlib
import json
from uuid import uuid4

from django.db import connection, transaction
from psycopg import sql

CANONICALIZATION = 'promop-vocab-ndjson-v1'

# Explicit projections keep later schema additions from changing an existing
# wire format. The first column is the unique ordering key, including the
# exported surrogate IDs on OMOP tables without a single natural primary key.
TABLE_COLUMNS = {
    'concept': (
        'concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
        'concept_class_id', 'standard_concept', 'concept_code',
        'valid_start_date', 'valid_end_date', 'invalid_reason', 'source',
    ),
    'concept_ancestor': (
        'id', 'ancestor_concept_id', 'descendant_concept_id',
        'min_levels_of_separation', 'max_levels_of_separation',
    ),
    'concept_class': ('concept_class_id', 'concept_class_name', 'concept_class_concept_id'),
    'concept_relationship': (
        'id', 'concept_id_1', 'concept_id_2', 'relationship_id',
        'valid_start_date', 'valid_end_date', 'invalid_reason',
    ),
    'concept_synonym': ('id', 'concept_id', 'concept_synonym_name', 'language_concept_id'),
    'domain': ('domain_id', 'domain_name', 'domain_concept_id'),
    'drug_strength': (
        'id', 'drug_concept_id', 'ingredient_concept_id', 'amount_value',
        'amount_unit_concept_id', 'numerator_value', 'numerator_unit_concept_id',
        'denominator_value', 'denominator_unit_concept_id', 'box_size',
        'valid_start_date', 'valid_end_date', 'invalid_reason',
    ),
    'relationship': (
        'relationship_id', 'relationship_name', 'is_hierarchical',
        'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id',
    ),
    'source_to_concept_map': (
        'id', 'source_code', 'source_concept_id', 'source_vocabulary_id',
        'source_code_description', 'target_concept_id', 'target_vocabulary_id',
        'valid_start_date', 'valid_end_date', 'invalid_reason',
    ),
    'vocabulary': (
        'vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
        'vocabulary_version', 'vocabulary_concept_id', 'is_deprecated',
        'deprecated_date', 'deprecated_reason',
    ),
}
TEXT_KEYS = frozenset({'concept_class', 'domain', 'relationship', 'vocabulary'})


def iter_data_lines(table, source=None):
    """Yield exact UTF-8 data lines in key order, without a completion sentinel."""
    columns = TABLE_COLUMNS[table]  # Reject identifiers outside the pinned allowlist.
    order = sql.Identifier('t', columns[0])
    if table in TEXT_KEYS:
        order += sql.SQL(' COLLATE "C"')
    where, params = '', []
    if table == 'concept' and source == 'HealthKey':
        where, params = 'WHERE source = %s', ['HealthKey']
    elif table == 'concept' and source == 'external':
        where = 'WHERE source IS NULL'
    query = sql.SQL(
        'SELECT row_to_json(t)::text FROM (SELECT {} FROM {} {}) t ORDER BY {}'
    ).format(
        sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.Identifier(table), sql.SQL(where), order,
    )
    # Named cursors require an open transaction even when a streaming response
    # is consumed after the request has left the view's transaction.
    with transaction.atomic():
        with connection.cursor() as settings_cursor:
            settings_cursor.execute("SET LOCAL extra_float_digits = 3")
            settings_cursor.execute("SET LOCAL DateStyle = 'ISO, YMD'")
        with connection.connection.cursor(name=f'vocab_snapshot_{uuid4().hex}') as cursor:
            cursor.itersize = 1000
            cursor.execute(query, params)
            for (row_json,) in cursor:
                # Casting to text prevents the driver from decoding JSON numbers
                # and a second encoder from changing precision/escaping/spacing.
                yield row_json.encode('utf-8') + b'\n'


def table_checksum(table):
    digest = hashlib.sha256()
    count = 0
    for line in iter_data_lines(table):
        digest.update(line)
        count += 1
    return {
        'algorithm': 'sha256',
        'canonicalization': CANONICALIZATION,
        'digest': digest.hexdigest(),
        'count': count,
    }


def stream_ndjson(table, source=None):
    count = 0
    for line in iter_data_lines(table, source=source):
        yield line
        count += 1
    yield (json.dumps({'__done': True, 'rows': count}) + '\n').encode('utf-8')
