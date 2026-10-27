"""The clinical-input columns are created once, on either branch's history.

``0306`` (dev) and ``0310`` (cb-v2) both added the same three fields on parallel
chains, and both files are already applied in different environments. Since the
2026-10-28 back-merge ``0306`` owns them and ``0310`` is a placeholder, so a
database that has either one already applied can still migrate.
"""
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ModelState, ProjectState
from django.test import override_settings


MIGRATION = ('omop_core', '0310_cb_clinical_input_sources')
COLUMNS = {
    'prothrombin_time_uln',
    'activated_partial_thromboplastin_time_uln',
    'able_to_swallow_tablets_capsules',
}
ADDING_MIGRATION = '0306_patientrecord_able_to_swallow_tablets_capsules_and_more'


def _adding_migration():
    return importlib.import_module('omop_core.migrations.' + ADDING_MIGRATION).Migration


def _separate_state(migration):
    operation, = [op for op in migration.operations
                  if isinstance(op, migrations.SeparateDatabaseAndState)]
    return operation


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSchemaEditor:
    """Enough of a schema editor to observe what the migration would add."""

    def __init__(self, existing):
        self.added = []
        self.connection = SimpleNamespace(
            cursor=lambda: _FakeCursor(),
            introspection=SimpleNamespace(
                get_table_description=lambda cursor, table: [
                    SimpleNamespace(name=name) for name in existing
                ],
            ),
        )

    def add_field(self, model, field):
        self.added.append(field.column)


def _patient_record_apps():
    state = ProjectState()
    state.add_model(ModelState(
        'omop_core', 'PatientRecord',
        [('id', models.BigAutoField(primary_key=True))],
        {'db_table': 'omop_core_patientrecord'},
    ))
    return state.apps


def test_cb_v2_migration_is_a_placeholder_after_the_back_merge():
    module = importlib.import_module('omop_core.migrations.' + MIGRATION[1])
    migration = module.Migration
    assert migration.dependencies == [('omop_core', '0309_condition_context_mappings')]
    # 0310 is already applied on cb-v2 databases; it must not touch the columns
    # there, and 0306 owns them everywhere else.
    assert migration.operations == []
    # The application test runner uses --no-migrations; graph checks must read
    # the real modules, just like migration execution does.
    with override_settings(MIGRATION_MODULES={}):
        graph = MigrationLoader(None).graph
    leaf, = graph.leaf_nodes('omop_core')
    assert MIGRATION in graph.forwards_plan(leaf)


def test_adding_migration_declares_three_nullable_columns_without_seeds():
    migration = _adding_migration()
    assert migration.dependencies == [('omop_core', '0305_patientrecord_pre_existing_flag_guard')]
    state_operations = _separate_state(migration).state_operations
    assert len(state_operations) == 3
    assert {op.name for op in state_operations} == COLUMNS
    for operation in state_operations:
        assert isinstance(operation, migrations.AddField)
        assert operation.model_name == 'patientrecord'
        assert operation.field.null and operation.field.blank
        assert operation.field.default is models.NOT_PROVIDED


def test_adding_migration_only_creates_columns_the_other_branch_did_not():
    module = importlib.import_module('omop_core.migrations.' + ADDING_MIGRATION)
    apps = _patient_record_apps()

    fresh = _FakeSchemaEditor(existing=[])
    module.add_missing_columns(apps, fresh)
    assert set(fresh.added) == COLUMNS

    # A cb-v2 database already has all three from 0310.
    complete = _FakeSchemaEditor(existing=COLUMNS)
    module.add_missing_columns(apps, complete)
    assert complete.added == []

    # A database branched off before either migration gets only what is missing.
    partial = _FakeSchemaEditor(existing={'prothrombin_time_uln'})
    module.add_missing_columns(apps, partial)
    assert set(partial.added) == COLUMNS - {'prothrombin_time_uln'}


def test_packaged_migration_graph_loads_without_patient_portal():
    script = '''
from django.conf import settings
settings.configure(
    SECRET_KEY='local-migration-graph-test',
    INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes',
                    'django.contrib.postgres', 'oauth2_provider',
                    'omop_core', 'omop_oncology', 'omop_genomics'],
    DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql',
                           'NAME': 'unused_graph_only', 'HOST': 'localhost', 'PORT': '5433'}},
    USE_TZ=True,
)
import django
django.setup()
from django.apps import apps
from django.db.migrations.loader import MigrationLoader
assert not apps.is_installed('patient_portal')
loader = MigrationLoader(None)
assert not loader.detect_conflicts()
leaf, = loader.graph.leaf_nodes('omop_core')
assert ('omop_core', '0310_cb_clinical_input_sources') in loader.graph.forwards_plan(leaf)
assert ('omop_core', '0306_patientrecord_able_to_swallow_tablets_capsules_and_more') in loader.graph.forwards_plan(leaf)
from omop_core.services.clinical_input_contract import CLINICAL_INPUT_CONTRACT_VERSION
assert type(CLINICAL_INPUT_CONTRACT_VERSION) is int and CLINICAL_INPUT_CONTRACT_VERSION == 2
print('package graph valid')
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'package graph valid'
