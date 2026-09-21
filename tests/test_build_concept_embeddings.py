"""Exercise embedding-build recovery against real PostgreSQL/pgvector (#1476)."""
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection

from omop_core.models import ConceptEmbedding
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def concept():
    return ConceptFactory(vocabulary__vocabulary_id='INDEX_TEST',
                          standard_concept='S', invalid_reason=None)


@pytest.fixture
def encoder(monkeypatch):
    model = Mock()
    model.encode.side_effect = lambda names, **kwargs: [
        SimpleNamespace(tolist=lambda: [0.5] * 384) for _ in names
    ]
    constructor = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, 'sentence_transformers',
                        SimpleNamespace(SentenceTransformer=constructor))
    return constructor, model


@pytest.fixture(autouse=True)
def reset_index_and_memory():
    with connection.cursor() as cursor:
        cursor.execute('SHOW maintenance_work_mem')
        original = cursor.fetchone()[0]
        cursor.execute('DROP INDEX IF EXISTS ix_concept_embedding_cosine')
    yield
    with connection.cursor() as cursor:
        cursor.execute('DROP INDEX IF EXISTS ix_concept_embedding_cosine')
        cursor.execute("SELECT set_config('maintenance_work_mem', %s, false)", [original])


def build(concept, **kwargs):
    output = StringIO()
    call_command('build_concept_embeddings', vocabulary_id=concept.vocabulary_id,
                 stdout=output, **kwargs)
    return output.getvalue()


def index_state():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT i.indexrelid, i.indisvalid, pg_get_indexdef(i.indexrelid)
            FROM pg_index i
            WHERE i.indexrelid = to_regclass('ix_concept_embedding_cosine')
        """)
        return cursor.fetchone()


def memory_bytes():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_size_bytes(current_setting('maintenance_work_mem'))")
        return cursor.fetchone()[0]


@pytest.mark.parametrize('initial_memory, expected_mb', [('64MB', 512), ('1GB', 1024)])
def test_completed_embeddings_retry_repairs_index_without_model(
        concept, monkeypatch, initial_memory, expected_mb):
    ConceptEmbedding.objects.create(concept=concept, embedding=[0.5] * 384)
    # No sentence-transformers installation is needed to repair the index.
    monkeypatch.setitem(sys.modules, 'sentence_transformers', None)
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('maintenance_work_mem', %s, false)", [initial_memory])
    original_memory = memory_bytes()
    observed_memory = []

    def capture_build_memory(execute, sql, params, many, context):
        if 'CREATE INDEX ix_concept_embedding_cosine' in sql:
            observed_memory.append(memory_bytes())
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture_build_memory):
        output = build(concept)

    assert 'Nothing to embed.' in output
    assert output.count('Done.') == 1
    assert observed_memory == [expected_mb * 1024 * 1024]
    assert memory_bytes() == original_memory
    state = index_state()
    assert state[1] is True
    assert 'USING ivfflat (embedding vector_cosine_ops)' in state[2]
    assert ConceptEmbedding.objects.get(concept=concept).embedding[0] == 0.5


def test_fresh_build_and_force_rebuild(concept, encoder):
    build(concept)
    assert ConceptEmbedding.objects.filter(concept=concept).exists()
    assert index_state()[1] is True
    build(concept, force=True)
    assert encoder[1].encode.call_count == 2
    assert index_state()[1] is True


@pytest.mark.parametrize('existing_index', [False, True])
def test_failed_index_build_raises_and_retry_recovers(concept, encoder, existing_index):
    if existing_index:
        build(concept)
    original_index = index_state()
    original_memory = memory_bytes()

    def fail_index_build(execute, sql, params, many, context):
        if 'CREATE INDEX ix_concept_embedding_cosine' in sql:
            # A real SQL error aborts the transaction, exercising DDL rollback.
            return execute('SELECT 1 / 0', None, False, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_index_build):
        with pytest.raises(DatabaseError):
            build(concept)

    assert index_state() == original_index
    assert memory_bytes() == original_memory
    assert ConceptEmbedding.objects.filter(concept=concept).exists()
    encoder[0].reset_mock()
    build(concept)
    encoder[0].assert_not_called()
    assert index_state()[1] is True


def test_missing_encoder_fails_when_embeddings_are_needed(concept, monkeypatch):
    monkeypatch.setitem(sys.modules, 'sentence_transformers', None)
    with pytest.raises(CommandError, match='sentence-transformers is not installed'):
        build(concept)
    assert not ConceptEmbedding.objects.filter(concept=concept).exists()
