"""Retire inverse viral fields without losing answers or pending edits."""
import importlib
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader

from omop_core.models import PatientRecord
from patient_portal.api.serializers import PatientRecordSerializer

migration = importlib.import_module('omop_core.migrations.0321_positive_infection_status')


@pytest.mark.parametrize('positive,inverse,pending,expected', [
    (None, True, [], False), (None, False, [], True), (None, None, [], None),
    (True, True, [], True), (False, False, [], False),
    (True, True, ['no_hiv_status'], False),
    (False, False, ['hiv_status', 'no_hiv_status'], False),
    (True, None, ['no_hiv_status'], None),
    (None, True, ['hiv_status'], None),
])
def test_preserve_answers_and_pending_intent(positive, inverse, pending, expected):
    record = SimpleNamespace(
        hiv_status=positive, no_hiv_status=inverse,
        hepatitis_b_status=None, no_hepatitis_b_status=None,
        hepatitis_c_status=None, no_hepatitis_c_status=None,
        user_edited_fields=pending + ['weight'],
        custom_fields={'no_hiv_status': True, 'other': 'keep'},
    )
    changes = migration.positive_values(record)
    assert changes.get('hiv_status', positive) is expected
    assert 'no_hiv_status' not in changes['user_edited_fields']
    assert 'weight' in changes['user_edited_fields']
    assert changes['custom_fields'] == {'other': 'keep'}
    if 'hiv_status' in changes:
        assert 'hiv_status' in changes['user_edited_fields']


def test_retired_fields_absent_from_model_and_api():
    fields = {field.name for field in PatientRecord._meta.fields}
    serializer = PatientRecordSerializer()
    for negative in migration.RETIRED:
        assert negative not in fields
        assert negative not in serializer.fields
        assert negative[3:] in serializer.fields


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_answers_and_retires_formulas(settings):
    settings.MIGRATION_MODULES = {}
    apps = MigrationLoader(None).project_state(migration.Migration.dependencies).apps
    Record = apps.get_model('omop_core', 'PatientRecord')
    with connection.schema_editor() as editor:
        for name in migration.RETIRED:
            editor.add_field(Record, Record._meta.get_field(name))
    try:
        Person = apps.get_model('omop_core', 'Person')
        person = Person.objects.create(person_id=9982118)
        Record = apps.get_model('omop_core', 'PatientRecord')
        record = Record.objects.create(
            person_id=person.pk, hiv_status=None, no_hiv_status=True,
            hepatitis_b_status=True, no_hepatitis_b_status=True,
            user_edited_fields=['no_hiv_status', 'weight'],
        )
        Formula = apps.get_model('omop_core', 'FieldFormula')
        Formula.objects.update_or_create(field_name='no_hiv_status', defaults={
            'formula': '@not(hiv_status)', 'is_active': True,
        })
        with connection.schema_editor() as editor:
            migration.preserve_and_retire(apps, editor)
        saved = apps.get_model('omop_core', 'PatientRecord').objects.get(pk=record.pk)
        assert saved.hiv_status is False
        assert saved.hepatitis_b_status is True
        assert saved.hepatitis_c_status is None
        assert set(saved.user_edited_fields) == {'hiv_status', 'weight'}
        assert not apps.get_model('omop_core', 'FieldFormula').objects.get(
            field_name='no_hiv_status',
        ).is_active
        assert apps.get_model('omop_core', 'RecordRevision').objects.filter(
            patient_record_id=saved.pk, field='no_hiv_status', old_value='True',
        ).exists()
    finally:
        with connection.schema_editor() as editor:
            for name in migration.RETIRED:
                editor.remove_field(Record, Record._meta.get_field(name))
