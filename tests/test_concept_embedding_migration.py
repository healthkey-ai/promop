"""Migration 0206 must not assume concept_embedding already exists (#1430).

0204 creates the table with raw SQL and skips it on a server without pgvector,
so 0206 cannot emit a declarative ``AlterField``: on a database built from
empty there is no column to alter and ``migrate`` dies on
``relation "concept_embedding" does not exist``.  Existing deployments never hit
it, because their table predates the migration.
"""
from importlib import import_module
from unittest.mock import MagicMock, Mock

from django.db import migrations
from pgvector.django.vector import VectorField

# The module name starts with a digit, so it cannot be imported with `from`.
migration_0206 = import_module(
    'omop_core.migrations.0206_concept_embedding_managed'
)
Migration = migration_0206.Migration
align_embedding_column = migration_0206.align_embedding_column


def _schema_editor(*table_names):
    """A schema editor whose database holds exactly ``table_names``."""
    editor = Mock()
    editor.connection.introspection.table_names.return_value = list(table_names)
    editor.connection.cursor.return_value = MagicMock()
    return editor


def _executed(editor):
    cursor = editor.connection.cursor.return_value.__enter__.return_value
    return [call.args[0] for call in cursor.execute.call_args_list]


def test_no_ddl_when_table_is_absent(caplog):
    """A pgvector-less database gets a warning, not a failed migration."""
    editor = _schema_editor('concept', 'person')

    align_embedding_column(None, editor)

    assert _executed(editor) == []
    assert 'concept_embedding does not exist' in caplog.text


def test_column_typed_as_vector_when_table_is_present():
    """Where the table exists the SQL matches the AlterField it replaces."""
    editor = _schema_editor('concept', 'concept_embedding')

    align_embedding_column(None, editor)

    statements = _executed(editor)
    assert len(statements) == 1
    assert 'ALTER TABLE "concept_embedding"' in statements[0]
    assert 'TYPE vector(384)' in statements[0]


def test_migration_emits_no_unconditional_ddl():
    """Guard the split: state advances declaratively, DDL only via RunPython."""
    assert len(Migration.operations) == 1
    operation = Migration.operations[0]
    assert isinstance(operation, migrations.SeparateDatabaseAndState)
    assert all(
        isinstance(database_operation, migrations.RunPython)
        for database_operation in operation.database_operations
    )


def test_migration_state_makes_the_model_managed_with_a_vector_field():
    """Skipping the DDL must not skip the model state it stands in for."""
    state_operations = Migration.operations[0].state_operations
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
