"""Guarded, opt-in repair of ambiguous negative answers (#1988).

No remote operation is authorized by shipping this command. Default execution is
a read-only dry-run. Apply requires a pinned audit, a separately verified authoring
path, an explicitly identified local/staging target, and a new private backup.
"""
import json
import re
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from omop_core.management.commands.audit_pre_existing_conditions import Command as Audit, approval_digest
from omop_core.management.guarded_repair import (
    canonical, digest, lock_rows, make_transaction_read_only, read_json, receipt,
    target_guard, write_private,
)
from omop_core.models import (
    FieldFormula, PatientRecord, PreexistingConditionsAttestation, RecordRevision,
)

FLAG = 'no_pre_existing_conditions'
LEDGER = 'preexisting_conditions_repair'
CONSTRAINT = 'patientrecord_no_pre_existing_flag_needs_empty_list'
REQUIRED = {
    ('omop_core', '0304_deactivate_pre_existing_condition_formula'),
    ('omop_core', '0305_patientrecord_pre_existing_flag_guard'),
    ('omop_core', '0306_preexisting_conditions_attestation'),
}


def snapshot(record):
    # Hash the entire record: rollback must not overwrite a subsequent edit even
    # when that writer bypassed updated_at. Do not put unrelated PHI in the backup.
    values = {field.attname: getattr(record, field.attname)
              for field in record._meta.concrete_fields}
    history = list(RecordRevision.objects.filter(patient_record=record)
                   .exclude(field=LEDGER).order_by('id')
                   .values('id', 'field', 'old_value', 'new_value', 'changed_at', 'changed_by'))
    attestation = PreexistingConditionsAttestation.objects.filter(
        person_id=record.person_id).values('id', 'source', 'confirmed_at').first()
    return json.loads(canonical({
        'id': record.pk, 'person_id': record.person_id,
        'flag': record.no_pre_existing_conditions,
        'categories': record.preexisting_conditions,
        'record_sha256': digest(values), 'history_sha256': digest(history),
        'attestation': attestation,
    }))


class Command(BaseCommand):
    help = 'Dry-run ambiguous condition-answer repair; explicit guarded apply/rollback only'

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--manifest', help='Full audit manifest v2')
        mode.add_argument('--rollback', help='Repair backup/receipt to roll back')
        parser.add_argument('--expect-sha', required=True, help='Independently approved audit/backup digest')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--backup', help='New private backup file (required on apply)')
        parser.add_argument('--authoring-verified', action='store_true',
                            help='Operator attests deployed UI/API verification; not granted by a PR merge')
        parser.add_argument('--environment', choices=['local', 'staging'], default='local')

    def _lock(self, live, supplied, rolling_back):
        """Lock only the rows this run can write (#2450 review).

        The audit runs first, so a change between audit and lock is caught by
        `_repair`/`_rollback` re-reading the row; a new candidate waits for the
        next audit instead of being written without review.
        """
        lock_rows(PatientRecord, self._target_ids(live, supplied, rolling_back))

    @staticmethod
    def _target_ids(live, supplied, rolling_back):
        if rolling_back:
            return [entry['before']['id'] for entry in supplied.get('entries', [])]
        return [row['id'] for row in live['records']
                if row['classification'] in ('unattributed_true', 'rewritten_true')]

    def _ready(self):
        if FieldFormula.objects.filter(field_name=FLAG, is_active=True).exists():
            raise CommandError('The negative-answer formula is still active.')
        if not REQUIRED <= set(MigrationRecorder(connection).applied_migrations()):
            raise CommandError('Required formula/constraint/attestation migrations are not applied.')
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT convalidated, pg_get_expr(conbin, conrelid) FROM pg_constraint '
                'WHERE conrelid = %s::regclass AND conname = %s AND contype = %s',
                [PatientRecord._meta.db_table, CONSTRAINT, 'c'])
            constraint = cursor.fetchone()
        expected = ("no_pre_existing_conditionsisnottrueorpreexisting_conditionsisnullor"
                    "preexisting_conditions=anyarray['[]'::jsonb,'null'::jsonb]")
        expression = re.sub(r'[\s()]', '', constraint[1]).lower() if constraint else ''
        if not constraint or not constraint[0] or expression != expected:
            raise CommandError('The contradiction CHECK constraint is missing, unvalidated or unexpected.')

    def handle(self, *args, **options):
        applying = options['apply']
        path = options['rollback'] or options['manifest']
        supplied = read_json(path)
        if applying:
            if not options['authoring_verified'] or not options['backup']:
                raise CommandError('Apply requires --authoring-verified and a new --backup path.')
            target_guard(options['environment'])
        owns = not connection.in_atomic_block
        with transaction.atomic():
            if not applying and owns:
                make_transaction_read_only()
            live = Audit()._collect(summary_only=False)
            if applying:
                self._lock(live, supplied, rolling_back=bool(options['rollback']))
                self._ready()
            if options['rollback']:
                output = self._rollback(supplied, live, options['expect_sha'], applying)
            else:
                output = self._repair(supplied, live, options['expect_sha'], applying)
            if applying:
                write_private(options['backup'], output)
        self.stdout.write(canonical({
            'mode': 'applied' if applying else 'dry-run',
            'operation': output['operation'], 'records': len(output['entries']),
            'backup_sha256': output['backup_sha256'] if applying else None,
        }))

    def _repair(self, supplied, live, expected, applying):
        if (supplied.get('manifest_version') != 2
                or supplied.get('approval_sha256') != expected
                or approval_digest(supplied) != expected
                or expected != live['approval_sha256']
                or supplied.get('database') != live['database']
                or len(supplied.get('records', [])) != live['totals']['records']):
            raise CommandError('Audit version, target, full cohort or digest mismatch; rerun audit.')
        run = f'repair:1988:{uuid.uuid4()}'
        entries = []
        for row in live['records']:
            if row['classification'] not in ('unattributed_true', 'rewritten_true'):
                continue
            record = PatientRecord.objects.get(pk=row['id'])
            # Malformed and nonempty lists require separate review, never inference.
            if record.preexisting_conditions not in (None, []):
                continue
            before = snapshot(record)
            if applying:
                PatientRecord.objects.filter(pk=record.pk).update(
                    no_pre_existing_conditions=None, updated_at=timezone.now())
                PreexistingConditionsAttestation.objects.filter(person_id=record.person_id).delete()
                RecordRevision.objects.create(patient_record=record, field=FLAG,
                                              old_value='True', new_value='None', changed_by=run)
                record.refresh_from_db()
                after = snapshot(record)
                ledger = RecordRevision.objects.create(
                    patient_record=record, field=LEDGER, changed_by=run,
                    old_value=canonical(before), new_value=canonical(after))
                entries.append({'before': before, 'after': after, 'ledger_id': ledger.pk})
            else:
                entries.append({'before': before})
        return receipt('repair', run, live['database'], expected, entries)

    def _rollback(self, supplied, live, expected, applying):
        unsigned = {key: value for key, value in supplied.items() if key != 'backup_sha256'}
        if (supplied.get('receipt_version') != 1 or supplied.get('operation') != 'repair'
                or supplied.get('backup_sha256') != expected or digest(unsigned) != expected
                or supplied.get('database') != live['database']):
            raise CommandError('Backup/receipt target, version or digest mismatch.')
        run = f'rollback:1988:{uuid.uuid4()}'
        entries = []
        # Check the complete cohort before changing anything; any conflict aborts all.
        for entry in supplied.get('entries', []):
            before, after = entry['before'], entry['after']
            record = PatientRecord.objects.filter(pk=after['id']).first()
            ledger = RecordRevision.objects.filter(
                pk=entry['ledger_id'], field=LEDGER, changed_by=supplied['run_id'],
                patient_record_id=after['id'],
                old_value=canonical(before), new_value=canonical(after)).exists()
            if not ledger or record is None or snapshot(record) != after:
                raise CommandError('Rollback conflict: ledger or post-repair state no longer matches.')
        for entry in supplied.get('entries', []):
            before = entry['before']
            record = PatientRecord.objects.get(pk=before['id'])
            if applying:
                PatientRecord.objects.filter(pk=record.pk).update(
                    no_pre_existing_conditions=before['flag'], updated_at=timezone.now())
                old_attestation = before['attestation']
                if old_attestation:
                    PreexistingConditionsAttestation.objects.create(
                        pk=old_attestation['id'], person_id=record.person_id,
                        source=old_attestation['source'], confirmed_at=old_attestation['confirmed_at'])
                RecordRevision.objects.create(patient_record=record, field=FLAG,
                                              old_value='None', new_value='True', changed_by=run)
                record.refresh_from_db()
                restored = snapshot(record)
                RecordRevision.objects.create(
                    patient_record=record, field=LEDGER, changed_by=run,
                    old_value=canonical(entry['after']), new_value=canonical(restored))
                entries.append({'before': entry['after'], 'after': restored})
            else:
                entries.append({'before': entry['after']})
        return receipt('rollback', run, live['database'], expected, entries)
