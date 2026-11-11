"""preview_rederivation reports what a refresh would change and writes nothing."""
import csv
import io
import json
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from omop_core.models import Measurement, PatientRecord, RecordRevision
from omop_core.services import patient_record_service
from omop_core.services.patient_record_service import (
    DERIVATION_VERSION, refresh_patient_record,
)
from omop_core.services.rederivation_preview import PreviewWriteBlocked, preview_record
from tests.factories import MeasurementFactory, OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


def _settled(**factory_kwargs):
    """A record that is exactly what a refresh stores, then marked stale."""
    record = PatientRecordFactory(**factory_kwargs)
    refresh_patient_record(record.person)
    PatientRecord.objects.filter(pk=record.pk).update(derivation_version=0)
    return PatientRecord.objects.get(pk=record.pk)


def _stale(record, **values):
    """Store values the refresh would not derive, without running save()."""
    PatientRecord.objects.filter(pk=record.pk).update(**values)


def _run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command('preview_rederivation', *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def _json(*args):
    out, _ = _run('--format', 'json', *args)
    return json.loads(out)


def test_a_changed_value_is_reported_with_its_before_and_after():
    record = _settled()
    _stale(record, race='Black or African American')

    document = _json()

    assert document['changes'] == [{
        'person_id': record.person_id, 'field': 'race',
        'stored': 'Black or African American', 'rederived': 'White',
        'user_edited': False, 'redacted': False,
    }]
    assert document['summary']['records_scanned'] == 1
    assert document['summary']['records_changed'] == 1
    assert document['summary']['fields'] == {'race': 1}
    assert PatientRecord.objects.get(pk=record.pk).race == 'Black or African American'


def test_an_unchanged_record_is_scanned_and_not_reported():
    _settled()

    document = _json()

    assert document['summary']['records_scanned'] == 1
    assert document['summary']['records_changed'] == 0
    assert document['changes'] == []


def test_a_derived_number_is_compared_as_stored():
    """BMI is computed in save(); its preview must come from the saved row."""
    record = _settled()
    _stale(record, weight=80, weight_units='kg', height=180, height_units='cm', bmi=99.0)

    rows = {row['field']: row for row in _json()['changes']}

    # The extractors find no weight or height facts, so refresh clears all
    # three, and save() has no inputs left to compute a BMI from.
    assert rows['bmi']['stored'] == 99.0 and rows['bmi']['rederived'] is None
    assert rows['weight']['stored'] == 80 and rows['weight']['rederived'] is None


def test_identifier_values_are_redacted_but_their_change_is_reported():
    record = _settled()
    _stale(record, email='someone@example.org', phone_number='555-0100', race='Asian')

    out, _ = _run()
    rows = {row['field']: row for row in _json()['changes']}

    assert 'someone@example.org' not in out and '555-0100' not in out
    assert rows['email'] == {
        'person_id': record.person_id, 'field': 'email', 'stored': '[redacted]',
        'rederived': '[redacted]', 'user_edited': False, 'redacted': True,
    }
    assert rows['phone_number']['redacted'] is True
    assert rows['race']['stored'] == 'Asian'


def test_a_pending_edit_the_refresh_keeps_is_not_a_change():
    """A user's race without OMOP backing survives refresh, so nothing changes."""
    record = _settled()
    _stale(record, race='Asian', user_edited_fields=['race'])

    assert _json()['changes'] == []


def test_a_pending_edit_the_refresh_overrides_is_reported_and_marked():
    """Language fields come only from PersonLanguageSkill (v13): refresh drops the edit."""
    record = _settled()
    _stale(record, languages_skills='English language: speak',
           user_edited_fields=['languages_skills', 'race'], race='Asian')

    rows = {row['field']: row for row in _json()['changes']}

    assert rows['languages_skills'] == {
        'person_id': record.person_id, 'field': 'languages_skills',
        'stored': 'English language: speak', 'rederived': None,
        'user_edited': True, 'redacted': False,
    }
    assert rows['user_edited_fields']['stored'] == ['languages_skills', 'race']
    assert rows['user_edited_fields']['rederived'] == ['race']
    assert 'race' not in rows


def test_a_pending_edit_that_omop_now_backs_resolves():
    record = _settled()
    _stale(record, user_edited_fields=['race'])  # stored race is already 'White'

    rows = _json()['changes']

    assert rows == [{
        'person_id': record.person_id, 'field': 'user_edited_fields',
        'stored': ['race'], 'rederived': [], 'user_edited': False, 'redacted': False,
    }]


def test_cohort_options_mirror_the_backfill():
    org = OrganizationFactory(slug='acme')
    stale = _settled(organization=org)
    current = _settled()
    other_org = _settled()
    for record in (stale, current, other_org):
        _stale(record, race='Asian')
    PatientRecord.objects.filter(pk=current.pk).update(derivation_version=DERIVATION_VERSION)

    def people(*args):
        return sorted(row['person_id'] for row in _json(*args)['changes'])

    assert people() == sorted([stale.person_id, other_org.person_id])
    assert people('--all') == sorted([stale.person_id, current.person_id, other_org.person_id])
    assert people('--target-version', '0') == []
    assert people('--organization', 'acme') == [stale.person_id]
    assert people('--person-id', str(other_org.person_id), '--person-id', str(current.person_id)) == [
        other_org.person_id]
    assert people('--all', '--limit', '1') == [min(stale.person_id, current.person_id, other_org.person_id)]


def test_field_filter_and_row_limit():
    first, second = _settled(), _settled()
    for record in (first, second):
        _stale(record, race='Asian', ethnicity='Hispanic or Latino')

    by_field = _json('--field', 'ethnicity')
    assert {row['field'] for row in by_field['changes']} == {'ethnicity'}
    assert by_field['summary']['fields'] == {'ethnicity': 2}

    limited = _json('--max-rows', '1')
    assert len(limited['changes']) == 1
    assert limited['summary']['changes'] == 4
    assert limited['summary']['changes_shown'] == 1
    assert limited['summary']['fields'] == {'ethnicity': 2, 'race': 2}


def test_custom_fields_are_compared_per_key():
    record = _settled()
    _stale(record, custom_fields={'stale_custom_answer': 'yes'})

    rows = _json('--field', 'custom_fields')['changes']

    assert rows == [{
        'person_id': record.person_id, 'field': 'custom_fields.stale_custom_answer',
        'stored': 'yes', 'rederived': None, 'user_edited': False, 'redacted': False,
    }]


def test_text_output():
    record = _settled()
    _stale(record, race='Asian')

    out, _ = _run()

    assert 'Records scanned: 1' in out
    assert 'Records that would change: 1' in out
    assert 'Nothing was written.' in out
    assert f'person_id={record.person_id} race: "Asian" -> "White"' in out


def test_csv_output_keeps_the_summary_off_stdout():
    record = _settled()
    _stale(record, race='Asian', languages_skills='x', user_edited_fields=['languages_skills'])

    out, err = _run('--format', 'csv', '--field', 'race', '--field', 'languages_skills')

    rows = list(csv.DictReader(io.StringIO(out)))
    assert rows == [
        {'person_id': str(record.person_id), 'field': 'languages_skills', 'stored': '"x"',
         'rederived': 'null', 'user_edited': 'true', 'redacted': 'false'},
        {'person_id': str(record.person_id), 'field': 'race', 'stored': '"Asian"',
         'rederived': '"White"', 'user_edited': 'false', 'redacted': 'false'},
    ]
    assert json.loads(err)['summary']['records_changed'] == 1


def test_negative_limits_are_refused():
    with pytest.raises(CommandError):
        _run('--limit', '-1')
    with pytest.raises(CommandError):
        _run('--max-rows', '-1')


# --- read-only --------------------------------------------------------------


def _database_fingerprint():
    """Row count and content checksum of every table, plus every sequence."""
    fingerprint = {}
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names():
            quoted = connection.ops.quote_name(table)
            cursor.execute(
                f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
                f"FROM {quoted} t"
            )
            fingerprint[table] = cursor.fetchone()
        cursor.execute(
            "SELECT schemaname || '.' || sequencename, last_value FROM pg_sequences"
        )
        fingerprint.update({f'seq:{name}': value for name, value in cursor.fetchall()})
    return fingerprint


def test_the_command_writes_nothing(django_capture_on_commit_callbacks):
    records = [_settled() for _ in range(3)]
    _stale(records[0], race='Asian', email='someone@example.org')
    _stale(records[1], languages_skills='English language: speak',
           user_edited_fields=['languages_skills'])
    MeasurementFactory(person=records[2].person, value_as_number=7)
    RecordRevision.objects.create(patient_record=records[0], field='race', new_value='Asian')
    before = _database_fingerprint()
    assert len(before) > 50  # every table, not an empty comparison

    with mock.patch('celery.app.base.Celery.send_task') as send_task, \
            django_capture_on_commit_callbacks() as callbacks:
        document = _json('--all')

    assert document['summary']['records_changed'] == 2  # the preview did run
    assert _database_fingerprint() == before
    assert callbacks == []
    send_task.assert_not_called()


def test_a_write_outside_the_record_is_refused_and_rolled_back(monkeypatch):
    record = _settled()
    _stale(record, race='Asian')
    measurements = Measurement.objects.count()
    revisions = RecordRevision.objects.count()

    def writes(person, snapshot=None):
        RecordRevision.objects.create(patient_record=record, field='race', new_value='x')
        return {}

    monkeypatch.setattr(patient_record_service, '_get_blood_type_data', writes)

    with pytest.raises(PreviewWriteBlocked):
        preview_record(record)
    out = io.StringIO()
    with pytest.raises(CommandError, match='1 record'):
        call_command('preview_rederivation', '--format', 'json', stdout=out)
    document = json.loads(out.getvalue())

    assert document['errors'] == [{'person_id': record.person_id, 'error': 'PreviewWriteBlocked'}]
    assert RecordRevision.objects.count() == revisions
    assert Measurement.objects.count() == measurements
    assert PatientRecord.objects.get(pk=record.pk).race == 'Asian'


def test_a_task_publish_is_refused(monkeypatch):
    record = _settled()

    def publishes(person, snapshot=None):
        from omop_core.tasks import refresh_patient_record_task
        refresh_patient_record_task.apply_async(args=[person.person_id])
        return {}

    monkeypatch.setattr(patient_record_service, '_get_blood_type_data', publishes)

    with pytest.raises(PreviewWriteBlocked):
        preview_record(record)


def test_a_refused_write_the_refresh_swallows_still_fails_the_preview(monkeypatch):
    record = _settled()

    def swallows(person, snapshot=None):
        try:
            RecordRevision.objects.create(patient_record=record, field='race', new_value='x')
        except Exception:
            pass
        return {}

    monkeypatch.setattr(patient_record_service, '_get_blood_type_data', swallows)

    with pytest.raises(PreviewWriteBlocked):
        preview_record(record)
    assert not RecordRevision.objects.filter(new_value='x').exists()


def test_a_refused_task_the_refresh_swallows_still_fails_the_preview(monkeypatch):
    """Unlike a refused query, this leaves the transaction usable: only the list catches it."""
    record = _settled()

    def swallows(person, snapshot=None):
        from omop_core.tasks import refresh_patient_record_task
        try:
            refresh_patient_record_task.apply_async(args=[person.person_id])
        except Exception:
            pass
        return {}

    monkeypatch.setattr(patient_record_service, '_get_blood_type_data', swallows)

    with pytest.raises(PreviewWriteBlocked, match='Celery'):
        preview_record(record)
