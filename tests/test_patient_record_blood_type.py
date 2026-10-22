import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings

from omop_core.models import Measurement, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db
migration = importlib.import_module('omop_core.migrations.0290_patientrecord_blood_type')


def typing(person, code, value, at='2024-01-01', **kwargs):
    concept = ConceptFactory(
        concept_code=code,
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    with suppress_patient_record_refresh():
        return MeasurementFactory(person=person, measurement_concept=concept,
                                  measurement_date=at, value_as_string=value, **kwargs)


def assert_both_paths(person, expected):
    rows = Measurement.objects.filter(person=person).select_related(
        'measurement_concept', 'measurement_source_concept', 'value_as_concept',
    )
    # The migration must remain frozen and agree with current derivation.
    assert migration.blood_type_from_measurements(rows) == expected
    assert refresh_patient_record(person).blood_type == expected


def test_refresh_creates_record_and_tracks_latest_typing_and_source_deletion():
    person = PersonFactory()
    typing(person, '882-1', 'A negative', '2023-01-01')
    latest = typing(person, '882-1', 'O Rh(D) positive', '2024-01-01')
    typing(person, '882-1', 'AB positive', '2025-01-01', is_erroneous=True)
    assert not PatientRecord.objects.filter(person=person).exists()
    assert_both_paths(person, 'O positive')
    with suppress_patient_record_refresh():
        latest.delete()
    assert_both_paths(person, 'A negative')
    with suppress_patient_record_refresh():
        Measurement.objects.filter(person=person).delete()
    assert_both_paths(person, None)


def test_newer_separate_abo_supersedes_older_combined_result():
    person = PersonFactory()
    typing(person, '882-1', 'O positive', '2023-01-01')
    typing(person, '883-9', 'Group AB', '2024-01-01')
    typing(person, '10331-7', 'Rh negative', '2024-01-01')
    assert_both_paths(person, 'AB negative')


def test_newer_separate_rh_supersedes_rh_in_older_combined_result():
    person = PersonFactory()
    typing(person, '882-1', 'O positive', '2023-01-01')
    typing(person, '10331-7', 'Rh negative', '2024-01-01')
    assert_both_paths(person, 'O negative')


def test_event_time_precedes_id_and_id_breaks_timestamp_ties():
    person = PersonFactory()
    typing(person, '882-1', 'B positive', measurement_datetime=datetime(2024, 1, 1, 18, tzinfo=timezone.utc))
    typing(person, '882-1', 'A negative', measurement_datetime=datetime(2024, 1, 1, 8, tzinfo=timezone.utc))
    assert_both_paths(person, 'B positive')
    typing(person, '882-1', 'AB negative', measurement_datetime=datetime(2024, 1, 1, 18, tzinfo=timezone.utc))
    assert_both_paths(person, 'AB negative')


def test_source_concept_fallback_coded_value_and_vocabulary_collision():
    person = PersonFactory()
    row = typing(person, '882-1', None, value_as_concept=ConceptFactory(concept_name='O Rh(D) positive'))
    source = row.measurement_concept
    with suppress_patient_record_refresh():
        Measurement.objects.filter(pk=row.pk).update(measurement_concept_id=0, measurement_source_concept=source)
        MeasurementFactory(person=person, measurement_date='2025-01-01', value_as_string='B negative',
                           measurement_concept=ConceptFactory(concept_code='882-1', vocabulary=VocabularyFactory(vocabulary_id='OTHER')))
    assert_both_paths(person, 'O positive')


@pytest.mark.parametrize(('abo', 'expected'), [('O', 'O'), ('Weak D, see note', 'Weak D, see note')])
def test_partial_or_unrecognized_values_are_not_invented(abo, expected):
    person = PersonFactory()
    typing(person, '883-9', abo)
    typing(person, '882-1', '   ', '2025-01-01')
    assert_both_paths(person, expected)


def test_backfill_uses_historical_models_is_idempotent_and_preserves_existing_values():
    records = [PatientRecordFactory() for _ in range(3)]
    for record in records:
        typing(record.person, '882-1', 'O positive')
    PatientRecord.objects.filter(pk=records[1].pk).update(blood_type='AB negative')
    PatientRecord.objects.filter(pk=records[2].pk).update(user_edited_fields=['blood_type'])
    with override_settings(MIGRATION_MODULES={}):
        historical_apps = MigrationLoader(connection).project_state([
            ('omop_core', '0290_patientrecord_blood_type'),
        ]).apps
    for _ in range(2):
        migration.backfill_blood_type(historical_apps, SimpleNamespace(connection=connection))
    for record in records:
        record.refresh_from_db()
    assert [record.blood_type for record in records] == ['O positive', 'AB negative', None]


def test_refresh_preserves_a_pending_patient_edit():
    record = PatientRecordFactory(blood_type='AB negative', user_edited_fields=['blood_type'])
    typing(record.person, '882-1', 'O positive')
    assert refresh_patient_record(record.person).blood_type == 'AB negative'
