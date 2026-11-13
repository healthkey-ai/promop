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
        patient_record=authored, field=PREGNANCY, old_value='None', new_value='True',
        changed_by='patient@example.test')
    pending = PatientRecordFactory(
        **{PREGNANCY: False, TOBACCO: True}, user_edited_fields=[TOBACCO])
    rewritten = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: False})
    RecordRevision.objects.create(
        patient_record=rewritten, field=PREGNANCY, old_value='True', new_value='False',
        changed_by='patient@example.test')
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
    assert evidence['authored_revisions_total'] == 2      # authored + rewritten
    assert evidence['earliest_authored_revision_at']
    assert evidence['records_with_any_authored_revision'] == 2
    assert evidence['records_without_any_authored_revision'] == data['totals']['records'] - 2
    assert evidence['clear_candidates'] == 2             # the unattributed record, both fields
    assert evidence['clear_candidates_in_authored_records'] == 0
    rows = {row['id']: row for row in data['records']}
    assert rows[names['authored'].pk]['has_any_revision'] is True
    assert rows[names['unattributed'].pk]['has_any_revision'] is False


def test_revision_coverage_counts_a_revision_on_any_field(tmp_path):
    """A patient edited elsewhere before 0125 still has no revision history;
    one edited after it does, even when the audited fields were never touched."""
    record = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: True})
    RecordRevision.objects.create(
        patient_record=record, field='email', old_value='old@example.test',
        new_value='new@example.test', changed_by='patient@example.test')
    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())

    evidence = data['totals']['evidence']
    assert evidence['records_with_any_authored_revision'] == 1
    assert evidence['clear_candidates'] == 2
    assert evidence['clear_candidates_in_authored_records'] == 2


def test_machine_written_revisions_do_not_count_as_authored_history(tmp_path):
    """0321 and the default 'system' author write a revision for nearly every
    legacy record; counting them would report full coverage and never warn."""
    record = PatientRecordFactory(**{PREGNANCY: True, TOBACCO: True})
    for author in ('system', 'migration:0321', None):
        RecordRevision.objects.create(
            patient_record=record, field='no_hiv_status',
            old_value='None', new_value='True', changed_by=author)
    manifest, _sha = audit(tmp_path)
    data = json.loads(open(manifest).read())

    evidence = data['totals']['evidence']
    assert evidence['authored_revisions_total'] == 0
    assert evidence['earliest_authored_revision_at'] is None
    assert evidence['records_with_any_authored_revision'] == 0
    assert evidence['clear_candidates'] == 2
    assert evidence['clear_candidates_in_authored_records'] == 0



def test_apply_aborts_when_authored_evidence_lands_between_audit_and_lock(
        tmp_path, ready, monkeypatch):
    """The audit runs before the lock, so the repair must re-derive the evidence
    under the lock: a person answering True before the write is not a default."""
    from omop_core.management.commands import repair_legacy_pregnancy_tobacco as module

    names = cohort()
    manifest, sha = audit(tmp_path)
    original = module.Command._lock

    def lock_after_a_person_answers(self, live, supplied, rolling_back):
        RecordRevision.objects.create(
            patient_record=names['unattributed'], field=PREGNANCY,
            old_value='None', new_value='True')
        original(self, live, supplied, rolling_back)

    monkeypatch.setattr(module.Command, '_lock', lock_after_a_person_answers)
    with pytest.raises(CommandError, match='changed after the audit'):
        apply(tmp_path, manifest, sha)

    names['unattributed'].refresh_from_db()
    assert names['unattributed'].no_pregnancy_or_lactation_status is True
    assert not RecordRevision.objects.filter(
        changed_by__startswith='repair:2432:').exists()


def test_apply_aborts_when_a_candidate_disappears_between_audit_and_lock(
        tmp_path, ready, monkeypatch):
    from omop_core.management.commands import repair_legacy_pregnancy_tobacco as module

    names = cohort()
    manifest, sha = audit(tmp_path)
    original = module.Command._lock

    def lock_after_the_row_is_gone(self, live, supplied, rolling_back):
        PatientRecord.objects.filter(pk=names['unattributed'].pk).delete()
        original(self, live, supplied, rolling_back)

    monkeypatch.setattr(module.Command, '_lock', lock_after_the_row_is_gone)
    with pytest.raises(CommandError, match='disappeared after the audit'):
        apply(tmp_path, manifest, sha)


def test_an_unreadable_existing_revision_does_not_block_the_clear(tmp_path, ready):
    """A revision whose value cannot be parsed is not evidence (classify ignores
    it), so the post-loop re-read must not treat it as newly gained."""
    names = cohort()
    RecordRevision.objects.create(
        patient_record=names['unattributed'], field=PREGNANCY,
        old_value='True', new_value='unknown', changed_by='patient@example.test')
    manifest, sha = audit(tmp_path)
    data = json.loads(open(manifest).read())
    row = next(item for item in data['records'] if item['id'] == names['unattributed'].pk)
    assert row['fields'][PREGNANCY]['classification'] == 'unattributed_true'

    apply(tmp_path, manifest, sha)

    names['unattributed'].refresh_from_db()
    assert names['unattributed'].no_pregnancy_or_lactation_status is None


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


def test_apply_rolls_back_when_evidence_appears_during_the_loop(tmp_path, ready, monkeypatch):
    from omop_core.management.commands import repair_legacy_pregnancy_tobacco as module

    names = cohort()
    manifest, sha = audit(tmp_path)
    original = module._revisions
    calls = {'n': 0}

    def evidence_after_the_first_pass(field_name, ids=None):
        calls['n'] += 1
        if calls['n'] <= len(module.FIELDS):
            return original(field_name, ids=ids)
        if field_name == PREGNANCY:
            return {names['unattributed'].pk: [
                {'changed_at': None, 'old': 'True', 'new': 'False'}]}
        return original(field_name, ids=ids)

    monkeypatch.setattr(module, '_revisions', evidence_after_the_first_pass)
    with pytest.raises(CommandError, match='gained evidence during the run'):
        apply(tmp_path, manifest, sha)

    names['unattributed'].refresh_from_db()
    assert names['unattributed'].no_pregnancy_or_lactation_status is True


def test_receipt_records_the_approved_digest(tmp_path, ready):
    """The backup links to the manifest the operator approved; a classification
    name must never replace the digest in the receipt."""
    cohort()
    manifest, sha = audit(tmp_path)

    _path, receipt = apply(tmp_path, manifest, sha)

    assert receipt['input_sha256'] == sha




@pytest.mark.parametrize('route,headers,pending', [
    ('/api/v1/patient-records/{}/', {'HTTP_X_PATIENT_FORM': '1'}, True),
    ('/api/patient-info/{}/', {}, False),
])
def test_an_echoed_true_on_the_native_route_leaves_no_trace(tmp_path, route, headers, pending):
    """Keeping a stored True and saving: the shared form marks the field pending
    (authored), the native patient-info route records nothing for it. The other
    field in the same save gets a person-authored revision, so the record counts
    as covered while its True is still unattributed; the summary must warn about
    every clear candidate, not only those in records without history."""
    from rest_framework.test import APIClient

    from omop_core.models import Person
    from patient_portal.models import Identity

    person = Person.objects.create(person_id=99251)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    PatientRecord.objects.filter(pk=record.pk).update(**{TOBACCO: True, PREGNANCY: None})
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='echo-2506@example.test', is_staff=True))

    response = client.patch(route.format(person.person_id),
                            {TOBACCO: True, 'disease': 'CLL'}, format='json', **headers)
    assert response.status_code == 200, response.data

    record.refresh_from_db()
    assert getattr(record, TOBACCO) is True
    assert not RecordRevision.objects.filter(patient_record=record, field=TOBACCO).exists()
    assert (TOBACCO in (record.user_edited_fields or [])) is pending

    out = StringIO()
    call_command('audit_legacy_pregnancy_tobacco', output=str(tmp_path / 'a.json'), stdout=out)
    data = json.loads((tmp_path / 'a.json').read_text())
    row = next(row for row in data['records'] if row['id'] == record.pk)
    assert row['has_any_revision'] is True
    if pending:
        assert row['fields'][TOBACCO]['classification'] == 'authored_true'
        assert 'cannot be told apart' not in out.getvalue()
    else:
        assert row['fields'][TOBACCO]['classification'] == 'unattributed_true'
        assert data['totals']['evidence']['clear_candidates_in_authored_records'] == 1
        assert ('1 clear candidate(s) cannot be told apart from a genuine True'
                in out.getvalue())
