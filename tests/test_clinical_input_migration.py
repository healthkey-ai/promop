"""The dev clinical schema must not import the separate cb-v2 migration chain."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings


MIGRATION = ('omop_core', '0310_cb_clinical_input_sources')


def test_dev_migration_adds_only_three_nullable_columns_without_seeds():
    module = importlib.import_module('omop_core.migrations.' + MIGRATION[1])
    migration = module.Migration
    assert migration.dependencies == [('omop_core', '0309_condition_context_mappings')]
    assert len(migration.operations) == 3
    assert {op.name for op in migration.operations} == {
        'prothrombin_time_uln', 'activated_partial_thromboplastin_time_uln',
        'able_to_swallow_tablets_capsules',
    }
    for operation in migration.operations:
        assert isinstance(operation, migrations.AddField)
        assert operation.model_name == 'patientrecord'
        assert operation.field.null and operation.field.blank
        assert operation.field.default is models.NOT_PROVIDED
    # The application test runner uses --no-migrations; graph checks must read
    # the real modules, just like migration execution does.
    with override_settings(MIGRATION_MODULES={}):
        graph = MigrationLoader(None).graph
    leaf, = graph.leaf_nodes('omop_core')
    assert MIGRATION in graph.forwards_plan(leaf)


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
