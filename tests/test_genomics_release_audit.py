import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from omop_core.models import FieldConceptMapping
from tests.test_genomics_writer_readiness import ready  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def audit(passes=True):
    out = StringIO()
    if passes:
        call_command('audit_genomics_release', environment='isolated-test', check=True, stdout=out)
    else:
        with pytest.raises(CommandError, match='not ready'):
            call_command('audit_genomics_release', environment='isolated-test', check=True, stdout=out)
    return json.loads(out.getvalue())


def test_complete_release_schema_and_vocabulary_pass_without_mutation(ready):
    before = list(FieldConceptMapping.objects.order_by('pk').values())
    report = audit()
    assert report['ready'] and report['writer_prerequisites_passed']
    assert report['pending_migrations'] == [] and report['missing_columns'] == {}
    assert list(FieldConceptMapping.objects.order_by('pk').values()) == before


def test_missing_physical_provenance_fails_even_when_migration_is_recorded(ready):
    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE field_concept_mapping RENAME COLUMN provenance TO held_provenance')
    try:
        report = audit(False)
        assert report['missing_columns']['field_concept_mapping'] == ['provenance']
        assert not report['writer_prerequisites_passed']
    finally:
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE field_concept_mapping RENAME COLUMN held_provenance TO provenance')


def test_pending_migration_blocks_release_even_with_usable_writer(ready, settings):
    name = '0237_merge_concurrent_0236_branches'
    from django.db.migrations.loader import MigrationLoader
    settings.MIGRATION_MODULES = {}  # Exercise the real graph despite pytest --no-migrations.
    recorder = MigrationRecorder(connection)
    original = list(recorder.migration_qs.values())
    for app, migration in MigrationLoader(connection).disk_migrations:
        recorder.record_applied(app, migration)
    recorder.record_unapplied('omop_core', name)
    try:
        report = audit(False)
        assert 'omop_core.' + name in report['pending_migrations']
        assert report['writer_prerequisites_passed']
    finally:
        recorder.migration_qs.all().delete()
        recorder.migration_qs.bulk_create([recorder.Migration(**row) for row in original])


def test_bad_parent_recipe_blocks_release(ready):
    FieldConceptMapping.objects.filter(field_name='genomics_tp53').update(source_value='')
    assert not audit(False)['writer_prerequisites_passed']
