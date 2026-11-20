"""Run a data migration's RunPython function against the test database.

pytest runs ``--no-migrations``, so a migration's logic is exercised by
calling its function directly with the app registry and a schema-editor
stand-in that carries the connection, as ``RunPython`` would.
"""
import importlib
from types import SimpleNamespace

from django.apps import apps
from django.db import connection


def run_data_migration(migration_name: str, function_name: str) -> None:
    module = importlib.import_module(f'omop_core.migrations.{migration_name}')
    getattr(module, function_name)(apps, SimpleNamespace(connection=connection))
