"""Migration 0206 must not assume concept_embedding already exists (#1430).

0204 creates the table with raw SQL and skips it on a server without pgvector,
so 0206 cannot emit any DDL: on a database built from empty there is no column
to alter and ``migrate`` dies on ``relation "concept_embedding" does not
exist``.  Existing deployments never hit it, because their table predates the
migration.  These tests pin the shape that keeps 0206 state-only.
"""
from importlib import import_module

from django.db import migrations
from pgvector.django.vector import VectorField

# The module name starts with a digit, so it cannot be imported with `from`.
Migration = import_module(
    'omop_core.migrations.0206_concept_embedding_managed'
).Migration


def _split():
    assert len(Migration.operations) == 1
    operation = Migration.operations[0]
    assert isinstance(operation, migrations.SeparateDatabaseAndState)
    return operation


def test_migration_emits_no_ddl():
    """No database operation at all — not even a RunPython, which sqlmigrate runs."""
    assert _split().database_operations == []


def test_migration_state_makes_the_model_managed_with_a_vector_field():
    """Skipping the DDL must not skip the model state it stands in for."""
    state_operations = _split().state_operations
    options = next(
        operation for operation in state_operations
        if isinstance(operation, migrations.AlterModelOptions)
    )
    field = next(
        operation for operation in state_operations
        if isinstance(operation, migrations.AlterField)
    )
    assert 'managed' not in options.options
    assert isinstance(field.field, VectorField)
    assert field.field.dimensions == 384
