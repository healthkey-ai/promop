"""Legacy lab columns must agree with their canonical value and unit."""
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from rest_framework.test import APIClient

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.models import Identity
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def editor(settings):
    settings.CELERY_BROKER_URL = ''
    record = PatientRecordFactory()
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='lab-alias-editor@example.test', is_staff=True))
    return record, client


@pytest.mark.parametrize('canonical,alias,value,expected,unit_field,unit', [
    ('anc_thousand_per_ul', 'absolute_neutrophile_count', '2.5', Decimal('2500'),
     'absolute_neutrophile_count_units', 'CELLS/UL'),
    ('alc_thousand_per_ul', 'absolute_lymphocyte_count', '1.5', 1500.0, None, None),
    ('hemoglobin_g_dl', 'hemoglobin_level', '13.0', Decimal('13.0'),
     'hemoglobin_level_units', 'G/DL'),
])
def test_canonical_edit_replaces_stale_legacy_value_and_survives_refresh(
        editor, canonical, alias, value, expected, unit_field, unit):
    record, client = editor
    setattr(record, alias, 9)
    record.save(update_fields=[alias])
    response = client.patch(f'/api/patient-info/{record.person_id}/', {canonical: value}, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert getattr(record, alias) == expected
    assert Decimal(str(response.data[alias])) == Decimal(str(expected))
    if unit_field:
        assert getattr(record, unit_field) == unit
    refreshed = refresh_patient_record(record.person)
    assert getattr(refreshed, alias) == expected


@pytest.mark.parametrize('canonical,alias,unit_field', [
    ('anc_thousand_per_ul', 'absolute_neutrophile_count', 'absolute_neutrophile_count_units'),
    ('alc_thousand_per_ul', 'absolute_lymphocyte_count', None),
    ('hemoglobin_g_dl', 'hemoglobin_level', 'hemoglobin_level_units'),
])
def test_clearing_canonical_clears_legacy_alias(editor, canonical, alias, unit_field):
    record, client = editor
    response = client.patch(f'/api/patient-info/{record.person_id}/', {canonical: '2.5'}, format='json')
    assert response.status_code == 200, response.data
    response = client.patch(f'/api/patient-info/{record.person_id}/', {canonical: None}, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert getattr(record, alias) is None
    if unit_field:
        assert getattr(record, unit_field) is None
    refreshed = refresh_patient_record(record.person)
    assert getattr(refreshed, alias) is None


@pytest.mark.parametrize('canonical,alias,value', [
    ('anc_thousand_per_ul', 'absolute_neutrophile_count', Decimal('2500')),
    ('alc_thousand_per_ul', 'absolute_lymphocyte_count', 2500.0),
    ('hemoglobin_g_dl', 'hemoglobin_level', Decimal('2.5')),
])
def test_legacy_alias_cannot_be_edited_independently(editor, canonical, alias, value):
    record, client = editor
    response = client.patch(f'/api/patient-info/{record.person_id}/', {canonical: '2.5'}, format='json')
    assert response.status_code == 200, response.data
    response = client.patch(f'/api/patient-info/{record.person_id}/', {alias: 1}, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert getattr(record, alias) == value
    assert getattr(record, canonical) == Decimal('2.5')


def test_pending_legacy_edit_survives_refresh_until_a_new_canonical_edit(editor):
    record, client = editor
    PatientRecord.objects.filter(pk=record.pk).update(
        hemoglobin_level=Decimal('9.0'), user_edited_fields=['hemoglobin_level'],
    )
    refreshed = refresh_patient_record(record.person)
    assert refreshed.hemoglobin_level == Decimal('9.0')
    assert 'hemoglobin_level' in refreshed.user_edited_fields
    response = client.patch(f'/api/patient-info/{record.person_id}/', {'hemoglobin_g_dl': '13.0'}, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('13.0')
    assert 'hemoglobin_level' not in record.user_edited_fields


def test_canonical_edit_supersedes_legacy_hemoglobin_in_another_unit(editor):
    record, client = editor
    PatientRecord.objects.filter(pk=record.pk).update(
        hemoglobin_level=Decimal('11.2'), hemoglobin_level_units='mmol/L',
    )
    response = client.patch(
        f'/api/patient-info/{record.person_id}/', {'hemoglobin_g_dl': '13.0'}, format='json',
    )
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('13.0')
    assert record.hemoglobin_level_units == 'G/DL'


def test_migration_repairs_canonical_rows_and_preserves_legacy_only_rows():
    wrong = PatientRecordFactory()
    legacy_only = PatientRecordFactory()
    pending_edit = PatientRecordFactory()
    PatientRecord.objects.filter(pk=wrong.pk).update(
        anc_thousand_per_ul=Decimal('2.5'), absolute_neutrophile_count=Decimal('2.5'),
        absolute_neutrophile_count_units='10*3/uL',
        alc_thousand_per_ul=Decimal('1.5'), absolute_lymphocyte_count=1.5,
        hemoglobin_g_dl=Decimal('13.0'), hemoglobin_level=Decimal('9.0'),
    )
    PatientRecord.objects.filter(pk=legacy_only.pk).update(
        absolute_neutrophile_count=Decimal('800'), absolute_neutrophile_count_units='CELLS/UL',
        absolute_lymphocyte_count=700, hemoglobin_level=Decimal('10'),
    )
    PatientRecord.objects.filter(pk=pending_edit.pk).update(
        hemoglobin_g_dl=Decimal('13'), hemoglobin_level=Decimal('9'),
        user_edited_fields=['hemoglobin_level'],
    )
    repair = import_module('omop_core.migrations.0265_reconcile_lab_alias_units').reconcile_aliases
    schema_editor = SimpleNamespace(connection=connection)
    repair(apps, schema_editor)
    repair(apps, schema_editor)
    wrong.refresh_from_db()
    legacy_only.refresh_from_db()
    pending_edit.refresh_from_db()
    assert wrong.absolute_neutrophile_count == Decimal('2500')
    assert wrong.absolute_neutrophile_count_units == 'CELLS/UL'
    assert wrong.absolute_lymphocyte_count == 1500
    assert wrong.hemoglobin_level == Decimal('13.0')
    assert wrong.hemoglobin_level_units == 'G/DL'
    assert legacy_only.absolute_neutrophile_count == Decimal('800')
    assert legacy_only.absolute_lymphocyte_count == 700
    assert legacy_only.hemoglobin_level == Decimal('10')
    assert pending_edit.hemoglobin_level == Decimal('9')
    assert pending_edit.user_edited_fields == ['hemoglobin_level']
