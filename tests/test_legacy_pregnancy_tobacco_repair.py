import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from omop_core.management.commands.repair_legacy_pregnancy_tobacco import REQUIRED
from omop_core.models import FieldFormula, PatientRecord, RecordRevision
from tests.factories import (
    ConceptFactory, MeasurementFactory, ObservationFactory, PatientRecordFactory,
)

pytestmark = pytest.mark.django_db

PREGNANCY = 'no_pregnancy_or_lactation_status'
TOBACCO = 'no_tobacco_use_status'


@pytest.fixture
def ready():
    recorder = MigrationRecorder(connection)
    recorder.ensure_schema()
    for app, name in REQUIRED:
        recorder.record_applied(app, name)


def cohort():
    unattributed = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: True})
    authored = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
    RecordRevision.objects.create(
        patient_record=authored, field=PREGNANCY, old_value='None', new_value='True')
    pending = PatientRecordFactory(
        **{PREGNANCY: False, TOBACCO: True}, user_edited_fields=[TOBACCO])
    rewritten = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
    RecordRevision.objects.create(
        patient_record=rewritten, field=PREGNANCY, old_value='True', new_value='False')
    tobacco_source = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: True})
    ObservationFactory(person=tobacco_source.person,
                       observation_source_value='266919005')
    pregnancy_source = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
    MeasurementFactory(person=pregnancy_source.person,
                       measurement_source_value='2106-3', value_as_string='Negative')
    tobacco_conflict = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: True})
    ObservationFactory(person=tobacco_conflict.person,
                       observation_source_value='77176002')
    pregnancy_conflict = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
    MeasurementFactory(person=pregnancy_conflict.person,
                       measurement_source_value='2106-3', value_as_string='Positive')
    # The save signal re-derives the record from the new fact. Put the legacy
    # True back with a queryset update (no signal), which is the state the audit
    # exists to classify.
    ForRecord = PatientRecord.objects
    for record in (tobacco_source, pregnancy_source, tobacco_conflict, pregnancy_conflict):
        ForRecord.filter(pk=record.pk).update(
            **{PREGNANCY: record.no_pregnancy_or_lactation_status,
               TOBACCO: record.no_tobacco_use_status})
    negative = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: False})
    unknown = PatientRecordFactory(**{PREGNANCY: None, TOBACCO: None})
    return {
        'unattributed': unattributed,
        'authored': authored,
        'pending': pending,
        'rewritten': rewritten,
        'tobacco_source': tobacco_source,
        'pregnancy_source': pregnancy_source,
        'tobacco_conflict': tobacco_conflict,
        'pregnancy_conflict': pregnancy_conflict,
        'negative': negative,
        'unknown': unknown,
    }


def audit(tmp_path, name='audit.json'):
    path = tmp_path / name
    call_command('audit_legacy_pregnancy_tobacco', output=str(path), stdout=StringIO())
    return str(path), json.loads(path.read_text())['approval_sha256']


def repair(manifest, sha, **options):
    call_command('repair_legacy_pregnancy_tobacco', manifest=manifest, expect_sha=sha,
                 stdout=StringIO(), **options)


def apply(tmp_path, manifest, sha, name='backup.json', **options):
    path = tmp_path / name
    repair(manifest, sha, apply=True, backup=str(path), **options)
    return path, json.loads(path.read_text())


def rollback(tmp_path, path, receipt, **options):
    call_command('repair_legacy_pregnancy_tobacco', rollback=str(path),
                 expect_sha=receipt['backup_sha256'], apply=True,
                 backup=str(tmp_path / 'undo.json'), **options)
    return json.loads((tmp_path / 'undo.json').read_text())


def test_audit_classifies_each_kind_of_evidence(tmp_path):
    names = cohort()
    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())

    def field(name, key=PREGNANCY):
        return next(row for row in data['records'] if row['id'] == names[name].pk)['fields'][key]

    assert field('unattributed')['classification'] == 'unattributed_true'
    assert field('unattributed')['recommended_action'] == 'clear'
    assert field('authored')['classification'] == 'authored_true'
    assert field('pending', TOBACCO)['classification'] == 'authored_true'
    assert field('rewritten')['classification'] == 'rewritten_true'
    assert field('tobacco_source', TOBACCO)['classification'] == 'source_backed_true'
    assert field('pregnancy_source')['classification'] == 'source_backed_true'
    assert field('tobacco_conflict', TOBACCO)['classification'] == 'source_conflict'
    assert field('pregnancy_conflict')['classification'] == 'source_conflict'
    assert field('rewritten')['recommended_action'] == 'needs_policy'
    assert field('negative')['classification'] == 'explicit_false'
    assert field('unknown')['classification'] == 'unanswered'


def test_the_audit_publishes_the_revision_coverage_the_clear_review_needs(tmp_path):
    """A True answered before RecordRevision existed cannot be told apart from
    the default, so the audit has to publish what history it does have."""
    names = cohort()
    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())

    assert data['manifest_version'] == 2
    evidence = data['totals']['evidence']
    assert evidence['revisions_total'] == 2              # authored + rewritten
    assert evidence['earliest_revision_at']
    assert evidence['records_with_any_revision'] == 2
    assert evidence['records_without_any_revision'] == data['totals']['records'] - 2
    assert evidence['clear_candidates'] == 2             # the unattributed record, both fields
    assert evidence['clear_candidates_with_revision_history'] == 0
    rows = {row['id']: row for row in data['records']}
    assert rows[names['authored'].pk]['has_any_revision'] is True
    assert rows[names['unattributed'].pk]['has_any_revision'] is False


def test_apply_locks_only_the_rows_it_clears(tmp_path, ready):
    """The repair used to hold SHARE ROW EXCLUSIVE on the whole table and block
    every patient write for the loop (#2450 review)."""
    from django.test.utils import CaptureQueriesContext

    names = cohort()
    manifest, sha = audit(tmp_path)
    with CaptureQueriesContext(connection) as captured:
        apply(tmp_path, manifest, sha)

    statements = [query['sql'] for query in captured.captured_queries]
    assert not any('LOCK TABLE' in sql for sql in statements)
    assert any('FOR UPDATE' in sql and str(names['unattributed'].pk) in sql
               for sql in statements)


def test_fail_on_makes_defaults_a_release_gate(tmp_path):
    cohort()
    with pytest.raises(CommandError, match='finding gate'):
        call_command('audit_legacy_pregnancy_tobacco',
                     fail_on=['unattributed_true'], stdout=StringIO())


def test_source_parsers_handle_lab_spellings_and_conflicts(tmp_path):
    records = {}
    for name, value in (
            ('neg', 'NEG\nSamples with Specific Gravity <1.010'),
            ('positive', 'Positive\nReference range: Negative'),
            ('not_negative', 'Not negative'),
            ('nd', 'ND'),
            ('blank', '   ')):
        record = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
        MeasurementFactory(person=record.person, measurement_source_value='2106-3',
                           value_as_string=value)
        records[name] = record
    conflict = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: True})
    ObservationFactory(
        person=conflict.person,
        observation_concept=ConceptFactory(concept_code='72166-2'),
        observation_source_value='266919005',
        value_as_concept=ConceptFactory(concept_code='LA18976-3'),
    )
    records['tobacco_conflict'] = conflict
    tobacco_text = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: True})
    ObservationFactory(person=tobacco_text.person,
                       observation_source_value='72166-2',
                       value_as_string='Current smoker, never tried quitting')
    records['tobacco_text'] = tobacco_text
    never_assessed = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: True})
    ObservationFactory(person=never_assessed.person,
                       observation_source_value='72166-2',
                       value_as_string='Never assessed')
    records['never_assessed'] = never_assessed
    never_smoker = PatientRecordFactory(**{PREGNANCY: False, TOBACCO: True})
    ObservationFactory(person=never_smoker.person,
                       observation_source_value='72166-2',
                       value_as_string='Never smoker')
    records['never_smoker'] = never_smoker
    for record in records.values():
        PatientRecord.objects.filter(pk=record.pk).update(
            **{PREGNANCY: True, TOBACCO: True})

    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())
    by_id = {row['id']: row for row in data['records']}
    assert by_id[records['neg'].pk]['fields'][PREGNANCY]['classification'] == 'source_backed_true'
    assert by_id[records['positive'].pk]['fields'][PREGNANCY]['classification'] == 'source_conflict'
    assert by_id[records['not_negative'].pk]['fields'][PREGNANCY]['classification'] == 'source_conflict'
    assert by_id[records['nd'].pk]['fields'][PREGNANCY]['classification'] == 'source_unknown'
    assert by_id[records['blank'].pk]['fields'][PREGNANCY]['classification'] == 'source_unknown'
    assert by_id[records['tobacco_conflict'].pk]['fields'][TOBACCO]['classification'] == 'source_conflict'
    assert by_id[records['tobacco_text'].pk]['fields'][TOBACCO]['classification'] == 'source_conflict'
    assert by_id[records['never_assessed'].pk]['fields'][TOBACCO]['classification'] == 'source_unknown'
    assert by_id[records['never_smoker'].pk]['fields'][TOBACCO]['classification'] == 'source_backed_true'


def test_a_pending_marker_does_not_override_a_recorded_false(tmp_path):
    record = PatientRecordFactory(
        **{PREGNANCY: True, TOBACCO: False}, user_edited_fields=[PREGNANCY])
    RecordRevision.objects.create(
        patient_record=record, field=PREGNANCY, old_value='True', new_value='False')
    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())
    row = next(item for item in data['records'] if item['id'] == record.pk)
    assert row['fields'][PREGNANCY]['classification'] == 'rewritten_true'
    assert row['fields'][PREGNANCY]['recommended_action'] == 'needs_policy'


def test_a_recorded_clear_then_true_is_rewritten(tmp_path):
    record = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
    RecordRevision.objects.create(
        patient_record=record, field=PREGNANCY, old_value='True', new_value='None')
    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())
    row = next(item for item in data['records'] if item['id'] == record.pk)
    assert row['fields'][PREGNANCY]['classification'] == 'rewritten_true'


def test_dry_run_changes_nothing(tmp_path):
    names = cohort()
    before = RecordRevision.objects.count()
    manifest, sha = audit(tmp_path)
    repair(manifest, sha)

    names['unattributed'].refresh_from_db()
    assert names['unattributed'].no_pregnancy_or_lactation_status is True
    assert RecordRevision.objects.count() == before


def test_apply_clears_only_defaults_and_rewritten_values(tmp_path, ready):
    names = cohort()
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)

    assert path.stat().st_mode & 0o777 == 0o600
    assert len(receipt['entries']) == 1      # only the unattributed record
    for name, expected in (
            ('unattributed', (None, None)),
            ('rewritten', (True, False)),
            ('authored', (True, False)),
            ('pending', (False, True)),
            ('tobacco_source', (False, True)),
            ('pregnancy_source', (True, False)),
            ('tobacco_conflict', (False, True)),
            ('pregnancy_conflict', (True, False)),
            ('negative', (False, False)),
            ('unknown', (None, None))):
        record = names[name]
        record.refresh_from_db()
        assert (record.no_pregnancy_or_lactation_status,
                record.no_tobacco_use_status) == expected, name
    assert RecordRevision.objects.filter(
        patient_record=names['unattributed'], field=PREGNANCY,
        changed_by__startswith='repair:2432:').exists()
    assert RecordRevision.objects.filter(
        patient_record=names['unattributed'],
        field__startswith='legacy_pregnancy_tobacco_repair:').exists()


def test_rollback_restores_the_backup(tmp_path, ready):
    names = cohort()
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    undo = rollback(tmp_path, path, receipt)

    assert len(undo['entries']) == 1
    record = names['unattributed']
    record.refresh_from_db()
    assert record.no_pregnancy_or_lactation_status is True
    assert record.no_tobacco_use_status is True
    # The rollback revision is not authorship: the restored default classifies
    # as a default again.
    manifest, _sha = audit(tmp_path, name='after-rollback.json')
    data = json.loads(open(manifest).read())
    row = next(item for item in data['records'] if item['id'] == record.pk)
    assert row['fields'][PREGNANCY]['classification'] == 'unattributed_true'


def test_rollback_refuses_when_the_record_moved(tmp_path, ready):
    names = cohort()
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    PatientRecord.objects.filter(pk=names['unattributed'].pk).update(
        **{PREGNANCY: False})
    with pytest.raises(CommandError, match='Rollback conflict'):
        rollback(tmp_path, path, receipt)


def test_apply_requires_the_nullable_migrations(tmp_path):
    cohort()
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='0323/0329'):
        apply(tmp_path, manifest, sha)


def test_an_active_pregnancy_formula_blocks_apply(tmp_path, ready):
    cohort()
    # The current model guard refuses this row; an older database predates it.
    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE field_formula DROP CONSTRAINT '
                       'field_formula_explicit_answer_inactive')
    FieldFormula.objects.create(
        field_name=PREGNANCY, formula='1', is_active=True)
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='still active'):
        apply(tmp_path, manifest, sha)


@pytest.mark.parametrize('change', ['value', 'digest', 'target', 'id'])
def test_changed_state_manifest_or_target_is_refused(tmp_path, ready, change):
    names = cohort()
    manifest, sha = audit(tmp_path)
    if change == 'value':
        names['unattributed'].no_pregnancy_or_lactation_status = None
        names['unattributed'].save(update_fields=[PREGNANCY])
    elif change == 'digest':
        sha = '0' * 64
    else:
        data = json.loads(open(manifest).read())
        if change == 'target':
            data['database']['name'] = 'different'
        else:
            data['records'][0]['id'] += 999
        with open(manifest, 'w') as stream:
            json.dump(data, stream)
    with pytest.raises(CommandError, match='mismatch'):
        apply(tmp_path, manifest, sha)


def test_backup_is_exclusive_and_apply_needs_one(tmp_path, ready):
    cohort()
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='backup'):
        repair(manifest, sha, apply=True)
    (tmp_path / 'backup.json').write_text('do not overwrite')
    with pytest.raises(CommandError, match='exclusively create'):
        apply(tmp_path, manifest, sha)
    assert (tmp_path / 'backup.json').read_text() == 'do not overwrite'
