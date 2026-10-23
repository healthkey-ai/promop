"""The pre-existing-condition answer and its categories cannot contradict (#1988).

`preexisting_conditions` is the evidence; `no_pre_existing_conditions` answers a separate
question and can never be derived from an empty list. Three layers keep them apart: the
serializer refuses a request that states both, the service invalidates a stale flag when
categories exist, and a migration resolves rows that already contradict.
"""

import types

import pytest
from django.apps import apps as django_apps
from django.db import connection

from omop_core.models import FieldFormula, RecordRevision
from omop_core.services.patient_record_service import recompute_patient_record_fields
from patient_portal.api.serializers import PatientRecordSerializer
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


def test_a_record_with_categories_keeps_the_answer_unstated():
    record = PatientRecordFactory(preexisting_conditions=[])

    recompute_patient_record_fields(record)

    assert record.no_pre_existing_conditions is None


def test_a_stale_answer_is_invalidated_when_categories_exist():
    record = PatientRecordFactory(
        preexisting_conditions=['infections'], no_pre_existing_conditions=True,
    )

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None
    assert record.preexisting_conditions == ['infections']


def test_removing_the_last_category_does_not_restore_the_answer():
    record = PatientRecordFactory(preexisting_conditions=[], no_pre_existing_conditions=None)

    recompute_patient_record_fields(record, changed_fields={'preexisting_conditions'})

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None


def test_stating_both_on_a_patch_is_refused():
    record = PatientRecordFactory(preexisting_conditions=['infections'],
                                  no_pre_existing_conditions=None)

    serializer = PatientRecordSerializer(
        record, data={'no_pre_existing_conditions': True}, partial=True)

    assert not serializer.is_valid()
    assert 'no_pre_existing_conditions' in serializer.errors


def test_adding_categories_over_an_existing_answer_is_refused():
    record = PatientRecordFactory(preexisting_conditions=[],
                                  no_pre_existing_conditions=True)

    serializer = PatientRecordSerializer(
        record, data={'preexisting_conditions': ['infections']}, partial=True)

    assert not serializer.is_valid()
    assert 'no_pre_existing_conditions' in serializer.errors


def test_stating_no_conditions_with_an_empty_list_is_allowed():
    record = PatientRecordFactory(preexisting_conditions=[],
                                  no_pre_existing_conditions=None)

    serializer = PatientRecordSerializer(
        record, data={'no_pre_existing_conditions': True}, partial=True)

    assert serializer.is_valid(), serializer.errors


def test_the_migration_resolves_existing_contradictions_and_keeps_the_old_value():
    from importlib import import_module

    migration = import_module(
        'omop_core.migrations.0305_patientrecord_pre_existing_flag_guard')
    contradictory = PatientRecordFactory(
        preexisting_conditions=['infections'], no_pre_existing_conditions=True)
    consistent = PatientRecordFactory(
        preexisting_conditions=[], no_pre_existing_conditions=True)

    migration.resolve_conflicts(
        django_apps, types.SimpleNamespace(connection=connection))

    contradictory.refresh_from_db()
    consistent.refresh_from_db()
    assert contradictory.no_pre_existing_conditions is None
    assert consistent.no_pre_existing_conditions is True
    revision = RecordRevision.objects.get(
        patient_record=contradictory, field='no_pre_existing_conditions')
    assert revision.changed_by == 'migration:1988'
    assert revision.old_value == 'True'


def test_the_formula_for_the_flag_is_not_active():
    assert not FieldFormula.objects.filter(
        field_name='no_pre_existing_conditions', is_active=True).exists()
