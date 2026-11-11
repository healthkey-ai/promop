"""Guarded, opt-in repair of legacy pregnancy/tobacco defaults (#2432).

No remote operation is authorized by shipping this command. Default execution
is a read-only dry-run. Apply requires a pinned audit, a new private backup, an
explicit local/staging target, and the migrations that made the columns
nullable and deactivated the pregnancy formula.

It clears only the two classifications the audit recommends for clearing:
``unattributed_true`` (a pure legacy default) and ``rewritten_true`` (the last
authored value was False and the old formula wrote True again). Authored and
source-backed True values are never touched.
"""
import hashlib
import ipaddress
import json
import os
import uuid
from datetime import datetime
from urllib.parse import unquote, urlsplit

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from omop_core.management.commands.audit_legacy_pregnancy_tobacco import (
    Command as Audit, FIELDS, approval_digest,
)
from omop_core.models import FieldFormula, PatientRecord, RecordRevision

LEDGER = 'legacy_pregnancy_tobacco_repair'
REQUIRED = {
    ('omop_core', '0323_alter_patientrecord_no_tobacco_use_status'),
    ('omop_core', '0329_explicit_pregnancy_answer'),
}


class BackupEncoder(DjangoJSONEncoder):
    def default(self, value):
        # Retain the original timestamp, including microseconds.
        if isinstance(value, datetime):
            return value.isoformat()
        return super().default(value)


def canonical(value):
    return json.dumps(value, cls=BackupEncoder, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


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


def read_json(path):
    try:
        with open(path, encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError('Expected an object')
        return data
    except (OSError, ValueError) as exc:
        raise CommandError('Cannot read the manifest/backup as a JSON object.') from exc


def write_private(path, payload):
    # Exclusive create, restrictive permissions, fsync BEFORE committing DB data.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(canonical(payload) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        parent_fd = os.open(os.path.dirname(os.path.abspath(path)),
                            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise CommandError('Cannot exclusively create and persist the private backup.') from exc


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

    def _target_guard(self, environment):
        cfg = connection.settings_dict
        target = (str(cfg.get('HOST') or ''), str(cfg.get('PORT') or '5432'), str(cfg['NAME']))

        def identity(url):
            parts = urlsplit(url)
            return (parts.hostname or '', str(parts.port or 5432), unquote(parts.path.lstrip('/')))

        prod = os.environ.get('PROD_DATABASE_URL')
        if prod and identity(prod) == target:
            raise CommandError('Production writes are forbidden.')
        if (not target[0] or cfg.get('OPTIONS', {}).get('service') or os.environ.get('PGSERVICE')):
            raise CommandError('Apply requires explicit HOST configuration without connection services.')
        if cfg.get('OPTIONS', {}).get('hostaddr') or os.environ.get('PGHOSTADDR'):
            raise CommandError('Host-address overrides are forbidden for apply.')
        connection.ensure_connection()
        info = connection.connection.info
        effective = (info.host, str(info.port), info.dbname)
        if effective != target:
            raise CommandError('Effective connection does not match the configured target.')
        if environment == 'local':
            if target[0] not in ('localhost', '127.0.0.1', '::1'):
                raise CommandError('Local apply requires an explicitly configured loopback database.')
            try:
                local_peer = ipaddress.ip_address(info.hostaddr).is_loopback
            except (AttributeError, ValueError):
                local_peer = False
            if not local_peer:
                raise CommandError('Local apply requires a verified loopback peer address.')
        else:
            staging = os.environ.get('STAGING_DATABASE_URL')
            if not staging or identity(staging) != target:
                raise CommandError('Staging target must exactly match STAGING_DATABASE_URL.')

    def _lock(self):
        if connection.vendor != 'postgresql':
            raise CommandError('Apply requires PostgreSQL.')
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            names = ', '.join(connection.ops.quote_name(model._meta.db_table)
                              for model in (PatientRecord, RecordRevision))
            cursor.execute(f'LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE')

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
            self._target_guard(options['environment'])
        owns = not connection.in_atomic_block
        with transaction.atomic():
            if applying:
                self._lock()
                self._ready()
            elif owns:
                Audit()._make_transaction_read_only()
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
        return self._receipt('repair', run, live['database'], expected, entries)

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
        return self._receipt('rollback', run, live['database'], expected, entries)

    @staticmethod
    def _receipt(operation, run, database, expected, entries):
        payload = {'receipt_version': 1, 'operation': operation, 'run_id': run,
                   'database': database, 'input_sha256': expected, 'entries': entries}
        payload['backup_sha256'] = digest(payload)
        return payload
