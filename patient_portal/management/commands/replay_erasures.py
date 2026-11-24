"""Erase again, after a restore from a backup, what the exported deletion
log says was erased (docs/member_erasure_architecture.md).

Reads the file ``export_erasures`` wrote. Every line must carry the
signature this deployment's audit key gives it, or nothing is replayed
(exit 1): run it with ``AUDIT_HMAC_KEY`` set to the key the entries were
written under. An entry the restored database already holds is left alone,
and so is a row made after the entry (a reused id). Run it straight after
the restore, before the application is opened again, and give ``--since``
the time the backup was taken. ``--dry-run`` only reports. Prints ids, never data.
"""
from django.core.management.base import BaseCommand, CommandError

from patient_portal import erasure_replay


class Command(BaseCommand):
    help = 'Replay an exported deletion log against a restored database.'

    def add_arguments(self, parser):
        parser.add_argument('export', help='The file export_erasures wrote.')
        parser.add_argument(
            '--since', help='When the backup was taken (ISO 8601): entries older than this are not looked at.')
        parser.add_argument('--dry-run', action='store_true', help='Report what would be erased; erase nothing.')
        parser.add_argument(
            '--include-unlinked', action='store_true',
            help='Also erase a person who has no sign-in. Only when nothing has been written to the '
                 'restored database yet: such a person cannot be told from a new one given the same id.')

    def handle(self, *args, **options):
        since = None
        if options['since']:
            try:
                since = erasure_replay.parse_time(options['since'])
            except ValueError:
                raise CommandError('--since is not an ISO 8601 date or time.')
        try:
            with open(options['export'], encoding='utf-8') as source:
                entries = erasure_replay.read(source)
        except OSError as error:
            raise CommandError(f'Cannot read the export: {error.strerror}.')
        except erasure_replay.BadExport as error:
            shown = ', '.join(str(n) for n in error.lines[:20])
            raise CommandError(
                f'{len(error.lines)} line(s) are not erasures signed by this deployment (line {shown}). '
                'Nothing was replayed.')

        dry = options['dry_run']
        report = erasure_replay.replay(entries, since=since, dry_run=dry, include_unlinked=options['include_unlinked'])
        would = 'would be ' if dry else ''
        self.stdout.write(
            f'{report.entries} erasure(s) to look at: {report.known} already in this database, '
            f'{report.gone} with nothing left to erase.')
        self.stdout.write(f'{len(report.accounts)} account(s) {would}closed again.')
        for person_id in report.accounts:
            self.stdout.write(f'  person {person_id}')
        self.stdout.write(f'{len(report.records)} record(s) {would}erased again.')
        for person_id in report.records:
            self.stdout.write(f'  person {person_id}')
        for person_id, why in report.skipped:
            self.stdout.write(self.style.WARNING(f'  skipped person {person_id}: {why}'))
