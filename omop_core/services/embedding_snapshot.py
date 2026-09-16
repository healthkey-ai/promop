"""Persist candidate retrieval across commands without relying on worker memory."""
import hashlib
import json

from django.db import connection
from psycopg import sql


def snapshot_key(options):
    return hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()


def read_snapshot(key):
    from omop_core.mapping.suggestions import suggestable_queryset

    eligible = suggestable_queryset(min_occurrences=1, resuggest=True)
    queue_sql, queue_params = eligible.order_by().values(
        'id', 'source_vocabulary_id', 'source_code', 'source_code_description',
        'umls_source_name', 'source_concept_id', 'domain_id', 'omop_table',
        'occurrence_count', 'target_concept_id', 'last_suggest_attempt',
    ).query.sql_with_params()
    # Order-independent content checksums detect bulk SQL loads and same-count
    # replacements, neither of which emits Django signals. Numeric sums avoid
    # bigint overflow. Counts distinguish empty tables and duplicate rows.
    # This is one query, but scans the inputs; it avoids per-code trigram work.
    with connection.cursor() as cursor:
        # The interpolated fragment is compiled by Django, never caller SQL.
        cursor.execute(sql.SQL("""
            WITH eligible AS ({queue}), inputs AS (
                SELECT 'concept' AS kind, count(*) AS n,
                       coalesce(sum(hashtextextended(
                           ROW(concept_id, concept_name, concept_code, vocabulary_id, domain_id,
                               standard_concept, invalid_reason)::text, 0)::numeric), 0) AS digest
                FROM concept
                UNION ALL
                SELECT 'synonym', count(*),
                       coalesce(sum(hashtextextended(
                           ROW(concept_id, concept_synonym_name)::text, 0)::numeric), 0)
                FROM concept_synonym
                UNION ALL
                SELECT 'queue', count(*),
                       coalesce(sum(hashtextextended(
                           ROW(eligible.*)::text, 0)::numeric), 0)
                FROM eligible
                UNION ALL
                SELECT 'umls', count(*),
                       coalesce(sum(hashtextextended(
                           ROW(id, concept_id, root_source, code, name,
                               is_preferred)::text, 0)::numeric), 0)
                FROM umls_source_code
                WHERE concept_id IN (
                    SELECT concept_id FROM umls_source_code
                    WHERE code IN (SELECT source_code FROM eligible)
                )
            ), fingerprint AS (
                SELECT jsonb_agg(jsonb_build_array(kind, n, digest::text)
                                 ORDER BY kind) AS value
                FROM inputs
            )
            SELECT fingerprint.value, snapshot.fingerprint,
                   snapshot.candidate_ids,
                   EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements_text(snapshot.candidate_ids) AS candidate(id)
                       WHERE NOT EXISTS (
                           SELECT 1 FROM concept_embedding
                           WHERE concept_id = candidate.id::bigint
                       )
                   ) AS missing
            FROM fingerprint
            LEFT JOIN suggest_embedding_snapshot AS snapshot ON snapshot.key = %s
        """).format(queue=sql.SQL(queue_sql)), [*queue_params, key])
        current, cached, candidate_ids, missing = cursor.fetchone()

    # Django's PostgreSQL connection returns raw JSON strings for JSONField
    # decoding; raw cursors do not apply the model field's decoder.
    current = json.loads(current) if isinstance(current, str) else current
    cached = json.loads(cached) if isinstance(cached, str) else cached
    candidate_ids = json.loads(candidate_ids) if isinstance(candidate_ids, str) else candidate_ids
    return current, candidate_ids if current == cached else None, missing
