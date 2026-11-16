"""Reversing 0323/0329 must not record an unanswered row as True (#2489).

Both migrations made a self-assessment nullable so Unknown can be stored. Their
reverse AlterField restores NOT NULL with the old default (True), which would
turn every unanswered row into an answer nobody gave. Like
0328.refuse_lossy_reverse, the reverse refuses instead.
"""
from importlib import import_module

import pytest
from django.db import connection, migrations

from omop_core.models import PatientRecord
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

CASES = [
    ('omop_core.migrations.0323_alter_patientrecord_no_tobacco_use_status',
     'no_tobacco_use_status'),
    ('omop_core.migrations.0329_explicit_pregnancy_answer',
     'no_pregnancy_or_lactation_status'),
]


class _Editor:
    connection = connection


@pytest.mark.parametrize('module,field', CASES)
def test_the_reverse_runs_the_refusal_first_and_forward_is_a_no_op(module, field):
    migration = import_module(module)
    last = migration.Migration.operations[-1]

    assert isinstance(last, migrations.RunPython)
    assert last.code is migrations.RunPython.noop
    assert last.reverse_code is migration.refuse_if_nulls


@pytest.mark.parametrize('module,field', CASES)
def test_the_reverse_refuses_an_unanswered_row(module, field):
    guard = import_module(module).refuse_if_nulls
    record = PatientRecordFactory()
    PatientRecord.objects.filter(pk=record.pk).update(**{field: None})

    with pytest.raises(RuntimeError, match='NOT NULL'):
        guard(None, _Editor())

    PatientRecord.objects.filter(pk=record.pk).update(**{field: False})
    guard(None, _Editor())
