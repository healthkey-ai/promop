"""Versioned vocabulary release manifests (issue #236, ADR 0001).

promop is the governed source of coded vocabulary data; consumers mirror the
concept tables.  This module builds and serves the release manifest that makes
mirroring safe:

  * ``publish_release`` — snapshot the current corpus tables into an immutable
    ``VocabRelease`` (scope declaration, per-vocabulary versions, per-table
    checksums and row counts).  Until the loader stages+publishes atomically
    (PR 3), this is a one-shot manifest with no change rows.
  * ``current_release`` — the latest published release, i.e. the one consumers
    should pin to and the only one snapshots are served from (PR 4).
  * ``current_corpus_scope`` — the declared corpus boundary: the loader's
    VOCAB_SCOPE plus the vocabularies actually loaded (including local HK-*
    quarantine vocabularies, which are part of the published corpus but are
    never touched by the Athena loader).

Nothing here mutates the corpus tables themselves.
"""
import hashlib
import secrets

from django.db import connection
from django.utils import timezone

from omop_core.models import Vocabulary, VocabRelease

RELEASE_SCHEMA_VERSION = '1.0'

# Tables whose contents make up the distributable corpus.  The snapshot and
# delta endpoints (PR 4) expose exactly these tables, in Athena column order.
CORPUS_TABLES = (
    'concept',
    'concept_synonym',
    'concept_relationship',
    'concept_ancestor',
    'drug_strength',
    'source_to_concept_map',
    'vocabulary',
    'concept_class',
    'domain',
    'relationship',
)

_FETCH_CHUNK = 10_000


def new_release_id():
    """Opaque release id: 'rel-<yyyymmdd>-<6hex>' (D5 — date sortable, collision-free)."""
    return f"rel-{timezone.now():%Y%m%d}-{secrets.token_hex(3)}"


def current_release():
    """The latest published VocabRelease, or None if none has been published."""
    return (
        VocabRelease.objects
        .filter(status=VocabRelease.STATUS_PUBLISHED)
        .order_by('-published_at', '-release_id')
        .first()
    )


def current_release_id():
    """release_id of the current published release, or None — cheap helper for
    stamping concept-endpoint responses."""
    release = current_release()
    return release.release_id if release else None


def current_corpus_scope():
    """Declared corpus boundary: the loader's VOCAB_SCOPE plus the vocabularies
    actually present in the DB (so consumers know exactly what a release
    governs — and that HK-* local vocabularies are intentionally included).
    """
    from omop_core.management.commands.load_athena_vocabularies import (
        LOINC_DOMAIN_SCOPE,
        RXNORM_CLASS_SCOPE,
        VOCAB_SCOPE,
    )
    loaded = sorted(Vocabulary.objects.values_list('vocabulary_id', flat=True))
    return {
        'declared_vocabularies': sorted(VOCAB_SCOPE),
        'loaded_vocabularies': loaded,
        'hk_vocabularies': [v for v in loaded if v.startswith('HK-')],
        'rxnorm_classes': sorted(RXNORM_CLASS_SCOPE),
        'loinc_domains': sorted(LOINC_DOMAIN_SCOPE),
    }


def compute_table_checksums():
    """Per-table sha256 + row count over every corpus table.

    Streams rows in first-column (PK) order so the digest is deterministic for
    identical contents and changes on any insert/update/delete.  Table names
    come from the CORPUS_TABLES constant (never user input) and are quoted.
    """
    checksums = {}
    counts = {}
    for table in CORPUS_TABLES:
        digest = hashlib.sha256()
        n = 0
        with connection.cursor() as cur:
            cur.execute(f'SELECT * FROM {connection.ops.quote_name(table)} ORDER BY 1')
            while True:
                rows = cur.fetchmany(_FETCH_CHUNK)
                if not rows:
                    break
                for row in rows:
                    digest.update(repr(row).encode('utf-8'))
                    n += 1
        checksums[table] = digest.hexdigest()
        counts[table] = n
    return checksums, counts


def publish_release(*, notes=''):
    """Build a manifest from the current corpus tables and publish it.

    Returns the new VocabRelease (status='published').  Prior releases remain
    published for history; ``current_release()`` always points at the newest.
    """
    checksums, counts = compute_table_checksums()
    versions = {
        row['vocabulary_id']: row['vocabulary_version']
        for row in Vocabulary.objects.values('vocabulary_id', 'vocabulary_version')
    }
    return VocabRelease.objects.create(
        release_id=new_release_id(),
        status=VocabRelease.STATUS_PUBLISHED,
        schema_version=RELEASE_SCHEMA_VERSION,
        corpus_scope=current_corpus_scope(),
        vocabulary_versions=versions,
        table_checksums=checksums,
        row_counts=counts,
        notes=notes,
        published_at=timezone.now(),
    )
