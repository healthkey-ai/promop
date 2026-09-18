"""Shared plumbing for copying data from another PRomop instance."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

import dj_database_url
from django.core.management.base import CommandError
from django.db import connections

from omop_core.models import Concept

SOURCE_ALIAS: str = 'copy_source'
CHUNK: int = 2000

T = TypeVar('T')
ConceptRef = tuple[str, str]

# Django fills these in only once, when it reads settings.DATABASES. An alias
# added later has to bring them itself or the first query fails on a missing key.
_CONNECTION_DEFAULTS: dict[str, object] = {
    'ATOMIC_REQUESTS': False, 'AUTOCOMMIT': True, 'CONN_MAX_AGE': 0,
    'CONN_HEALTH_CHECKS': False, 'TIME_ZONE': None, 'OPTIONS': {},
}
_TEST_DEFAULTS: dict[str, object] = {
    'CHARSET': None, 'COLLATION': None, 'MIGRATE': True, 'MIRROR': None, 'NAME': None,
}


def register_source_connection(url: str, alias: str = SOURCE_ALIAS) -> None:
    """Add a read-only connection to the source instance under alias.

    Registered at runtime, not in settings, so the test runner never tries to
    build a test database for the source.
    """
    config = dj_database_url.parse(url, conn_max_age=0)
    if not config.get('NAME'):
        raise CommandError('Could not parse a database name out of the source URL.')
    for key, value in _CONNECTION_DEFAULTS.items():
        config.setdefault(key, dict(value) if isinstance(value, dict) else value)
    for key in ('NAME', 'USER', 'PASSWORD', 'HOST', 'PORT'):
        config.setdefault(key, '')
    test = config.setdefault('TEST', {})
    for key, value in _TEST_DEFAULTS.items():
        test.setdefault(key, value)
    if 'postgresql' in config.get('ENGINE', ''):
        options = dict(config.get('OPTIONS') or {})
        options['options'] = '-c default_transaction_read_only=on'
        config['OPTIONS'] = options
    connections.databases[alias] = config


def chunked(rows: Iterable[T], size: int = CHUNK) -> Iterator[list[T]]:
    """Group rows into lists of at most size, consuming lazily."""
    chunk: list[T] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def concept_refs(using: str, concept_ids: Iterable[int | None]) -> dict[int, ConceptRef]:
    """(vocabulary_id, concept_code) for each concept id on the given database."""
    ids = {i for i in concept_ids if i is not None}
    if not ids:
        return {}
    return {
        concept_id: (vocabulary_id, code)
        for concept_id, vocabulary_id, code in Concept.objects.using(using)
        .filter(concept_id__in=ids).values_list('concept_id', 'vocabulary_id', 'concept_code')
    }


def local_concept_ids(refs: Iterable[ConceptRef]) -> dict[ConceptRef, int]:
    """Concept id on this database for each (vocabulary_id, concept_code).

    Matched by code, never by id: Athena ids agree across instances, but
    HealthKey-minted ids are numbered per instance.
    """
    wanted = set(refs)
    if not wanted:
        return {}
    return {
        (vocabulary_id, code): concept_id
        for concept_id, vocabulary_id, code in Concept.objects.filter(
            vocabulary_id__in={v for v, _ in wanted},
            concept_code__in={c for _, c in wanted},
        ).values_list('concept_id', 'vocabulary_id', 'concept_code')
        if (vocabulary_id, code) in wanted
    }
