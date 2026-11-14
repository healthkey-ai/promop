"""Guarded, opt-in repair of legacy pregnancy/tobacco defaults (#2432).

No remote operation is authorized by shipping this command. Default execution
is a read-only dry-run. Apply requires a pinned audit, a new private backup, an
explicit local/staging target, and the migrations that made the columns
nullable and deactivated the pregnancy formula.

It clears only ``unattributed_true`` (a pure legacy default). Authored,
source-backed, source-conflict and rewritten True values are never touched;
those need a person's answer or a policy review.
"""
import json
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from omop_core.management.commands.audit_legacy_pregnancy_tobacco import (
    Command as Audit, FIELDS, approval_digest,
)
from omop_core.management.guarded_repair import (
    canonical, digest, lock_tables, make_transaction_read_only, read_json, receipt,
    target_guard, write_private,
)
from omop_core.models import FieldFormula, PatientRecord, RecordRevision

LEDGER = 'legacy_pregnancy_tobacco_repair'
REQUIRED = {
    ('omop_core', '0323_alter_patientrecord_no_tobacco_use_status'),
    ('omop_core', '0329_explicit_pregnancy_answer'),
}


def snapshot(record, fields):
    # Hash the whole record: rollback must not overwrite a later edit even when
    # that writer bypassed updated_at. Do not put unrelated PHI in the backup.
    values = {item.attname: getattr(record, item.attname)
              for item in record._meta.concrete_fields}
    return json.loads(canonical({
        'id': record.pk, 'person_id': record.person_id,
        'fields': {field: getattr(record, field) for field in fields},
        'record_sha256': digest(values),
    }))


class Command(BaseCommand):
    help = ('Dry-run legacy pregnancy/tobacco repair; explicit guarded apply/rollback only')

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--manifest', help='Full audit manifest')
        mode.add_argument('--rollback', help='Repair backup/receipt to roll back')
        parser.add_argument('--expect-sha', required=True,
                            help='Independently approved audit/backup digest')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--backup', help='New private backup file (required on apply)')
        parser.add_argument('--environment', choices=['local', 'staging'], default='local')

    def _lock(self):
        lock_tables((PatientRecord, RecordRevision))

    def _ready(self):
        if FieldFormula.objects.filter(
                field_name='no_pregnancy_or_lactation_status', is_active=True).exists():
            raise CommandError('The pregnancy formula is still active.')
        if not REQUIRED <= set(MigrationRecorder(connection).applied_migrations()):
            raise CommandError(
                'Required nullable-column migrations (0323/0329) are not applied.')

    def handle(self, *args, **options):
        applying = options['apply']
        path = options['rollback'] or options['manifest']
        supplied = read_json(path)
        if applying:
            if not options['backup']:
                raise CommandError('Apply requires a new --backup path.')
            target_guard(options['environment'])
        owns = not connection.in_atomic_block
        with transaction.atomic():
            if applying:
                self._lock()
                self._ready()
            elif owns:
                make_transaction_read_only()
            live = Audit()._collect(summary_only=False)
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
        if (supplied.get('manifest_version') != 1
                or supplied.get('approval_sha256') != expected
                or approval_digest(supplied) != expected
                or expected != live['approval_sha256']
                or supplied.get('database') != live['database']
                or len(supplied.get('records', [])) != live['totals']['records']):
            raise CommandError('Audit version, target, full cohort or digest mismatch; rerun audit.')
        run = f'repair:2432:{uuid.uuid4()}'
        entries = []
        for row in live['records']:
            fields = [field for field in FIELDS
                      if row['fields'][field]['recommended_action'] == 'clear']
            if not fields:
                continue
            record = PatientRecord.objects.get(pk=row['id'])
            if any(getattr(record, field) is not True for field in fields):
                raise CommandError(
                    f'Record {record.pk} changed after the audit; rerun it.')
            before = snapshot(record, fields)
            if applying:
                updated = (PatientRecord.objects
                           .filter(pk=record.pk, **{field: True for field in fields})
                           .update(**{**{field: None for field in fields},
                                      'updated_at': timezone.now()}))
                if not updated:
                    raise CommandError(
                        f'Record {record.pk} changed after the audit; rerun it.')
                for field in fields:
                    RecordRevision.objects.create(
                        patient_record=record, field=field,
                        old_value='True', new_value='None', changed_by=run)
                record.refresh_from_db()
                after = snapshot(record, fields)
                ledger = RecordRevision.objects.create(
                    patient_record=record, field=f'{LEDGER}:{",".join(fields)}', changed_by=run,
                    old_value=canonical(before), new_value=canonical(after))
                entries.append({'before': before, 'after': after,
                                'fields': fields, 'ledger_id': ledger.pk})
            else:
                entries.append({'before': before, 'fields': fields})
        return receipt('repair', run, live['database'], expected, entries)

    def _rollback(self, supplied, live, expected, applying):
        unsigned = {key: value for key, value in supplied.items() if key != 'backup_sha256'}
        if (supplied.get('receipt_version') != 1 or supplied.get('operation') != 'repair'
                or supplied.get('backup_sha256') != expected or digest(unsigned) != expected
                or supplied.get('database') != live['database']):
            raise CommandError('Backup/receipt target, version or digest mismatch.')
        run = f'rollback:2432:{uuid.uuid4()}'
        entries = []
        # Check the complete cohort before changing anything; any conflict aborts all.
        for entry in supplied.get('entries', []):
            before, after, fields = entry['before'], entry['after'], entry['fields']
            record = PatientRecord.objects.filter(pk=after['id']).first()
            ledger = RecordRevision.objects.filter(
                pk=entry['ledger_id'], field=f'{LEDGER}:{",".join(fields)}',
                changed_by=supplied['run_id'], patient_record_id=after['id'],
                old_value=canonical(before), new_value=canonical(after)).exists()
            if (not ledger or record is None
                    or any(getattr(record, field) is not None for field in fields)
                    or snapshot(record, fields)['record_sha256'] != after['record_sha256']):
                raise CommandError('Rollback conflict: ledger or post-repair state no longer matches.')
        for entry in supplied.get('entries', []):
            before, after, fields = entry['before'], entry['after'], entry['fields']
            record = PatientRecord.objects.get(pk=before['id'])
            if applying:
                PatientRecord.objects.filter(pk=record.pk).update(
                    **{**{field: before['fields'][field] for field in fields},
                       'updated_at': timezone.now()})
                for field in fields:
                    RecordRevision.objects.create(
                        patient_record=record, field=field,
                        old_value='None', new_value='True', changed_by=run)
                record.refresh_from_db()
                restored = snapshot(record, fields)
                RecordRevision.objects.create(
                    patient_record=record, field=f'{LEDGER}:{",".join(fields)}', changed_by=run,
                    old_value=canonical(after), new_value=canonical(restored))
                entries.append({'before': after, 'after': restored, 'fields': fields})
            else:
                entries.append({'before': after, 'fields': fields})
        return receipt('rollback', run, live['database'], expected, entries)
