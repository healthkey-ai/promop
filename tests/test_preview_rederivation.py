"""preview_rederivation reports what a refresh would change and writes nothing."""
import csv
import io
import json
import logging
import re
import threading
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from omop_core.models import Location, Measurement, PatientRecord, Person, RecordRevision
from omop_core.services import patient_record_service
from omop_core.services.patient_record_service import (
    DERIVATION_VERSION, refresh_patient_record,
)
from omop_core.services.rederivation_preview import (
    PreviewWriteBlocked, _statement_is_allowed, describe_change, preview_record, same_value,
)
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


def test_a_change_is_reported_by_field_and_kind():
    record = _settled()
    _stale(record, race='Black or African American')

    document = _json()

    assert document['changes'] == [{
        'person_id': record.person_id, 'field': 'race',
        'change': 'changed', 'direction': None, 'user_edited': False,
    }]
    assert document['summary']['records_scanned'] == 1
    assert document['summary']['records_changed'] == 1
    assert document['summary']['fields'] == {'race': 1}
    assert document['summary']['changes_by_kind'] == {'changed': 1}
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
    assert rows['bmi']['change'] == 'cleared'
    assert rows['weight']['change'] == 'cleared'


@pytest.mark.parametrize('before, after, expected', [
    (None, 'x', ('set', None)),
    ('x', None, ('cleared', None)),
    ('x', 'y', ('changed', None)),
    (1, 2, ('changed', 'up')),
    (2.5, Decimal('1.0'), ('changed', 'down')),
    (False, True, ('changed', None)),   # a flag has no direction
    ([], ['race'], ('set', None)),      # empty is the derivation's empty
    ([{'variant': 'x'}], [], ('cleared', None)),
    ('M', '', ('cleared', None)),
    ({}, {'a': 1}, ('set', None)),
    (0, 1, ('changed', 'up')),          # 0 is an answer, not empty
    (float('nan'), 80.0, ('changed', None)),
    (Decimal('NaN'), Decimal('1'), ('changed', None)),
    (10 ** 400, 1, ('changed', 'down')),  # beyond float range: no OverflowError
])
def test_a_change_is_described_without_its_values(before, after, expected):
    assert describe_change(before, after) == expected


@pytest.mark.parametrize('before, after, same', [
    (1, True, False),
    (0, False, False),
    ([1], [True], False),
    ({'a': 0}, {'a': False}, False),
    (float('nan'), float('nan'), True),
    (1, 1.0, True),
    ({'a': [1, 2]}, {'a': [1, 2]}, True),
])
def test_a_flag_is_not_its_number_and_nan_is_itself(before, after, same):
    assert same_value(before, after) is same


def test_an_empty_string_becoming_null_is_not_a_change():
    record = _settled()
    _stale(record, stage='', tobacco_use_details='')

    rows = _json('--field', 'stage', '--field', 'tobacco_use_details')['changes']

    assert rows == []


def test_undefined_custom_keys_count_records_and_sort_without_their_text():
    from omop_core.models import CustomPatientField, FieldConceptMapping
    for name in ('a_score', 'm_flag'):
        mapping = FieldConceptMapping.objects.create(
            field_name=name, omop_table='observation', source_value=f'phr:{name}',
            value_kind='number', status='approved')
        CustomPatientField.objects.create(
            field_name=name, display_name=name, tab='disease', field_type='number',
            mode='editable', mapping=mapping)
    record = _settled()
    _stale(record, custom_fields={'a_score': 1, 'k_name': 1, 'm_flag': 1, 'zed': 1, 'B': 1})

    document = _json('--field', 'custom_fields')

    assert [row['field'] for row in document['changes']] == [
        'custom_fields.<undefined>', 'custom_fields.<undefined>', 'custom_fields.<undefined>',
        'custom_fields.a_score', 'custom_fields.m_flag']
    assert document['summary']['fields']['custom_fields.<undefined>'] == 1
    assert _json('--field', 'custom_fields.<undefined>')['summary']['changes'] == 3
    with pytest.raises(CommandError):
        _run('--field', 'custom_fields.k_name')


def test_a_value_appearing_from_null_is_set():
    record = _settled()
    Person.objects.filter(pk=record.person_id).update(email='someone@example.org')

    rows = _json('--field', 'email')['changes']

    assert rows == [{
        'person_id': record.person_id, 'field': 'email',
        'change': 'set', 'direction': None, 'user_edited': False,
    }]


def test_no_value_is_printed_in_any_format(monkeypatch, capsys, caplog):
    """Round 3 printed source text through disease, stage, race and an
    unchecked choices field; round 2 through genomic findings. No value of any
    field reaches the output, whatever the field."""
    marker = 'Jane Doe Qzx'
    record = _settled()
    _stale(record, email='jane@example.org', phone_number='555-0100',
           death_date='2024-03-01', latitude=45.123987, longitude=-73.654321,
           custom_fields={'note': marker, 'mrn': 4815162342, marker: 3})

    def derived(person, snapshot=None):
        # The refresh's own diagnostics quote what they diagnose.
        logging.getLogger('omop_core.services.patient_record_service').warning(
            'unit_source_value=%r', marker)
        return {'blood_type': None, 'tobacco_use_details': marker, 'planned_therapies': marker}

    monkeypatch.setattr(patient_record_service, '_get_blood_type_data', derived)
    PatientRecord.objects.filter(pk=record.pk).update(
        disease=marker, disease_slug=marker, stage=marker, race=marker,
        ethnicity=marker, genetic_mutations=[{'variant': marker}])

    caplog.set_level(logging.DEBUG)
    outputs = [_run(*args) for args in (
        (), ('--format', 'json'), ('--format', 'csv'), ('--max-rows', '0'))]
    captured = capsys.readouterr()
    outputs.append((captured.out + caplog.text, captured.err))

    assert _json()['summary']['changes'] >= 10
    for out, err in outputs:
        for value in ('Jane', 'Doe', 'Qzx', 'jane@', '555-0100', '2024-03-01',
                      '45.123987', '73.654321', '4815162342'):
            assert value not in out + err, value


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
        'change': 'cleared', 'direction': None, 'user_edited': True,
    }
    assert rows['user_edited_fields']['change'] == 'changed'
    assert 'race' not in rows


def test_a_pending_edit_that_omop_now_backs_resolves():
    record = _settled()
    _stale(record, user_edited_fields=['race'])  # stored race is already 'White'

    rows = _json()['changes']

    assert rows == [{
        'person_id': record.person_id, 'field': 'user_edited_fields',
        'change': 'cleared', 'direction': None, 'user_edited': False,
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


def test_text_qualifies_a_field_filter_and_keeps_the_suppressed_count():
    first, second = _settled(), _settled()
    for record in (first, second):
        _stale(record, race='Asian', ethnicity='Hispanic or Latino')

    filtered, _ = _run('--field', 'ethnicity')
    assert 'Records that would change in the selected fields: 2' in filtered
    assert 'Field changes in the selected fields: 2' in filtered

    limited, _ = _run('--max-rows', '0')
    assert '4 more row(s) not shown (--max-rows)' in limited


def test_a_post_save_formula_update_is_reported():
    """The preview reads the record after the refresh, including the formula
    update that runs after PatientRecord.save()."""
    from omop_core.models import FieldFormula
    record = _settled()
    _stale(record, bmi=Decimal('1.00'))
    FieldFormula.objects.create(field_name='bmi', formula='42', is_active=True)

    bmi = [row for row in _json()['changes'] if row['field'] == 'bmi']

    assert bmi and bmi[0]['direction'] == 'up'


def test_custom_fields_are_compared_per_key():
    from omop_core.models import CustomPatientField, FieldConceptMapping
    mapping = FieldConceptMapping.objects.create(
        field_name='stale_custom_answer', omop_table='observation',
        source_value='phr:stale-custom-answer', value_kind='number', status='approved')
    CustomPatientField.objects.create(
        field_name='stale_custom_answer', display_name='Stale', tab='disease',
        field_type='number', mode='editable', mapping=mapping)
    record = _settled()
    _stale(record, custom_fields={'stale_custom_answer': 3, 'Jane Doe MRN 77': 1})

    rows = _json('--field', 'custom_fields')['changes']

    # A key nobody defined is whatever was written into the JSON: no name.
    assert sorted(rows, key=lambda row: row['field']) == [
        {'person_id': record.person_id, 'field': 'custom_fields.<undefined>',
         'change': 'cleared', 'direction': None, 'user_edited': False},
        {'person_id': record.person_id, 'field': 'custom_fields.stale_custom_answer',
         'change': 'cleared', 'direction': None, 'user_edited': False},
    ]


def test_text_output():
    record = _settled()
    _stale(record, race='Asian')

    out, _ = _run()

    assert 'Records scanned: 1' in out
    assert 'Records that would change: 1' in out
    assert 'Nothing was written.' in out
    assert 'Field changes: 1, changed 1' in out
    assert f'person_id={record.person_id} race: changed' in out
    assert 'Asian' not in out and 'White' not in out


def test_csv_output_keeps_the_summary_off_stdout():
    record = _settled()
    _stale(record, race='Asian', languages_skills='x', user_edited_fields=['languages_skills'])

    out, err = _run('--format', 'csv', '--field', 'race', '--field', 'languages_skills')

    rows = list(csv.DictReader(io.StringIO(out)))
    assert rows == [
        {'person_id': str(record.person_id), 'field': 'languages_skills', 'change': 'cleared',
         'direction': '', 'user_edited': 'true'},
        {'person_id': str(record.person_id), 'field': 'race', 'change': 'changed',
         'direction': '', 'user_edited': 'false'},
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

    with django_capture_on_commit_callbacks() as callbacks:
        document = _json('--all')

    assert document['summary']['records_changed'] == 2  # the preview did run
    # A publish attempt would have been refused and reported as an error.
    assert document['summary']['errors'] == 0
    assert _database_fingerprint() == before
    assert callbacks == []


@pytest.mark.django_db(transaction=True)
def test_the_command_writes_nothing_under_real_commits():
    """Outside a test transaction each preview is the outermost transaction."""
    records = [_settled() for _ in range(4)]
    for record in records[:3]:
        _stale(record, race='Asian')
    before = _database_fingerprint()

    document = _json('--all', '--batch-size', '1')

    assert document['summary']['records_scanned'] == 4
    assert document['summary']['records_changed'] == 3
    assert document['summary']['errors'] == 0
    assert _database_fingerprint() == before


def _other_connection():
    import psycopg

    settings = connection.settings_dict
    return psycopg.connect(
        dbname=settings['NAME'], user=settings['USER'], password=settings['PASSWORD'],
        host=settings['HOST'] or None, port=settings['PORT'] or None, autocommit=True,
    )


@pytest.mark.django_db(transaction=True)
def test_the_baseline_is_read_under_the_row_lock(monkeypatch):
    """A save landing mid-preview must wait, not slip in after the baseline read."""
    record = _settled()
    real_refresh = patient_record_service.refresh_patient_record
    outcome = {}

    def refresh_while_someone_saves(person):
        def concurrent_save():
            import psycopg

            with _other_connection() as other:
                try:
                    other.execute("SET lock_timeout = '300ms'")
                    other.execute(
                        "UPDATE patient_record SET race = 'Asian' WHERE id = %s", [record.pk])
                    outcome['save'] = 'committed'
                except psycopg.errors.LockNotAvailable:
                    outcome['save'] = 'waited'

        worker = threading.Thread(target=concurrent_save)
        worker.start()
        worker.join()
        return real_refresh(person)

    monkeypatch.setattr(patient_record_service, 'refresh_patient_record', refresh_while_someone_saves)

    preview_record(record)

    assert outcome == {'save': 'waited'}
    assert PatientRecord.objects.get(pk=record.pk).race == 'White'


def test_a_failure_is_logged_without_the_row(caplog, capsys):
    """A CHECK violation's message quotes the whole row: it must not reach the log."""
    record = _settled()
    Location.objects.create(location_id=8801, latitude=500, longitude=10, zip='02139')
    Person.objects.filter(pk=record.person_id).update(location_id=8801, email='someone@example.org')
    _stale(record, email='someone@example.org', phone_number='555-0100')

    out, err = io.StringIO(), io.StringIO()
    with caplog.at_level(logging.DEBUG), pytest.raises(CommandError):
        call_command('preview_rederivation', '--format', 'json', stdout=out, stderr=err)

    captured = capsys.readouterr()
    logged = caplog.text + err.getvalue() + captured.out + captured.err + ''.join(
        str(r.exc_info) + str(r.exc_text) for r in caplog.records)
    for value in ('someone@example.org', '555-0100', '02139', 'Failing row', 'Traceback'):
        assert value not in logged, value
        assert value not in out.getvalue(), value
    assert all(r.exc_info is None for r in caplog.records)
    errors = json.loads(out.getvalue())['errors']
    at = errors[0].pop('at')
    # promop's own frame, not psycopg's cursor where every DB error is raised.
    assert re.fullmatch(r'\w+\.py:\d+ in \w+', at) and not at.startswith('cursor.py'), at
    assert errors == [{
        'person_id': record.person_id, 'error': 'IntegrityError',
        'sqlstate': '23514', 'constraint': 'patientrecord_latitude_range',
    }]
    assert 'sqlstate=23514 constraint=patientrecord_latitude_range' in caplog.text


def test_a_backfill_failure_is_logged_without_the_row(caplog, capsys):
    """The backfill the preview leads to logged the exception, whose message
    quotes the whole row, at ERROR, which Sentry forwards."""
    record = _settled()
    Location.objects.create(location_id=8802, latitude=500, longitude=10, zip='02139')
    Person.objects.filter(pk=record.person_id).update(location_id=8802, email='someone@example.org')
    _stale(record, email='someone@example.org', phone_number='555-0100')

    out = io.StringIO()
    with caplog.at_level(logging.DEBUG):
        call_command('backfill_patient_records', stdout=out)

    captured = capsys.readouterr()
    logged = caplog.text + out.getvalue() + captured.out + captured.err + ''.join(
        str(r.exc_info) + str(r.exc_text) for r in caplog.records)
    for value in ('someone@example.org', '555-0100', '02139', 'Failing row', 'Traceback'):
        assert value not in logged, value
    assert all(r.exc_info is None for r in caplog.records)
    assert (f'person_id={record.person_id}: error=IntegrityError at=' in caplog.text
            and 'sqlstate=23514 constraint=patientrecord_latitude_range' in caplog.text)
    assert 'at=cursor.py' not in caplog.text
    assert '1 error(s)' in out.getvalue()


def test_an_unknown_field_is_refused():
    _settled()

    with pytest.raises(CommandError, match='Unknown field.*disese'):
        _run('--field', 'disese')
    # Only a defined custom field has a name to ask for.
    with pytest.raises(CommandError, match='Unknown field.*custom_fields.anything'):
        _run('--field', 'custom_fields.anything')


@pytest.mark.parametrize('sql', [
    'SELECT "patient_record"."id" FROM "patient_record" WHERE "id" = %s FOR UPDATE',
    'UPDATE "patient_record" SET "race" = %s WHERE "id" = %s',
    'WITH x AS (SELECT 1) SELECT * FROM x',
    'SAVEPOINT "s1"',
    'RELEASE SAVEPOINT "s1"',
    'ROLLBACK TO SAVEPOINT "s1"',
    'SELECT 1;',
])
def test_the_guard_allows_reads_and_the_record_update(sql):
    assert _statement_is_allowed(sql)


@pytest.mark.parametrize('sql', [
    'INSERT INTO "measurement" ("id") VALUES (%s)',
    'INSERT INTO "patient_record" ("person_id") VALUES (%s)',
    'UPDATE "person" SET "email" = %s',
    'DELETE FROM "record_revision"',
    'SET LOCAL lock_timeout = 1',
    "SELECT nextval('measurement_id_seq')",
    "SELECT setval('measurement_id_seq', 1)",
    'SELECT pg_advisory_lock(1)',
    'SELECT pg_advisory_xact_lock(1)',
    'SELECT pg_try_advisory_lock(1)',
    "SELECT set_config('x.y', 'z', false)",
    'SELECT lo_create(0)',
    "SELECT dblink_exec('x', 'y')",
    'SELECT * INTO new_table FROM person',
    'WITH x AS (SELECT 1) SELECT * INTO new_table FROM x',
    'WITH d AS (DELETE FROM person RETURNING *) SELECT * FROM d',
    'SELECT 1; DELETE FROM person',
    'UPDATE "patient_record" SET "race" = %s; DELETE FROM person',
])
def test_the_guard_refuses_anything_that_can_outlive_the_rollback(sql):
    assert not _statement_is_allowed(sql)



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

    table = RecordRevision._meta.db_table
    [error] = document['errors']
    assert 'at' not in error  # the re-raise is not where the write was
    assert (error['person_id'], error['error']) == (record.person_id, 'PreviewWriteBlocked')
    # Where the refresh tried the write: this test's patched extractor.
    assert re.fullmatch(
        rf'a write outside patient_record: INSERT INTO "{table}" '
        r'\(at test_preview_rederivation\.py:\d+ in writes\)', error['refused']), error
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


@pytest.mark.parametrize('sql, kind', [
    ('INSERT INTO "record_revision" ("field") VALUES (%s)', 'INSERT INTO "record_revision"'),
    ('update person set given_name = \'Jane\' where id = 1', 'UPDATE person'),
    ('DELETE FROM "measurement" WHERE id = 1', 'DELETE FROM "measurement"'),
    ("SELECT set_config('app.name', 'Jane Doe', true)", 'SELECT'),
    ("'Jane Doe'", 'statement'),
    ('INSERT INTO "public"."record_revision" ("field") VALUES (%s)',
     'INSERT INTO "public"."record_revision"'),
    ('UPDATE ONLY "person" SET "x" = %s', 'UPDATE ONLY "person"'),
])
def test_a_refusal_names_the_statement_kind_never_its_text(sql, kind):
    from omop_core.services.rederivation_preview import _statement_kind
    assert _statement_kind(sql) == kind


@pytest.mark.parametrize('args, expected', [
    (('--organization', 'acme'), {'acme stale'}),
    ((), {'acme stale', 'other stale'}),
    (('--all',), {'acme stale', 'acme current', 'other stale', 'other current'}),
    (('--all', '--organization', 'acme'), {'acme stale', 'acme current'}),
    (('--target-version', '0'), set()),
    (('--target-version', '1'), {'acme stale', 'other stale'}),
])
def test_the_preview_and_the_backfill_select_the_same_cohort(args, expected):
    """A current insider and a stale outsider: dropping either filter shows."""
    acme = OrganizationFactory(slug='acme')
    records = {
        'acme stale': _settled(organization=acme), 'acme current': _settled(organization=acme),
        'other stale': _settled(), 'other current': _settled(),
    }
    for name in ('acme current', 'other current'):
        PatientRecord.objects.filter(pk=records[name].pk).update(
            derivation_version=DERIVATION_VERSION)
    for name in records:
        _stale(records[name], race='Asian')  # every record has a change to report
    names = {record.person_id: name for name, record in records.items()}

    out = io.StringIO()
    call_command('backfill_patient_records', '--dry-run', *args, stdout=out)
    preview = _json(*args)

    if expected:
        assert f'{len(expected)} record(s) matching' in out.getvalue()
    else:
        assert 'No stale records found.' in out.getvalue()
    assert {names[row['person_id']] for row in preview['changes']} == expected


@pytest.mark.parametrize('sql, reason', [
    ('UPDATE "patient_record" SET "a" = 1; DELETE FROM "x"', 'more than one statement'),
    ('UPDATE "patient_record" SET "a" = nextval(%s)', 'a call that outlives the rollback'),
    ('INSERT INTO "x" VALUES (1)', 'a write outside patient_record'),
])
def test_a_refusal_says_why(sql, reason):
    from omop_core.services.rederivation_preview import _refusal_reason
    assert _refusal_reason(sql) == reason
