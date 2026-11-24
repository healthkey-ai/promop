"""Write the deletion log (the audit trail's ERASE rows) as JSON lines.

Keep the output outside the database: after a restore from a backup,
``replay_erasures`` reads it to erase again whoever was erased after the
backup was taken (docs/member_erasure_architecture.md). It names person and
sign-in ids and who asked, and nothing of what was held.
"""
from django.core.management.base import BaseCommand, CommandError

from patient_portal import erasure_replay


class Command(BaseCommand):
    help = 'Export the deletion log as JSON lines, for replay_erasures after a restore.'

    def add_arguments(self, parser):
        parser.add_argument('--output', help='File to write (default: standard output).')
        parser.add_argument('--since', help='Only entries at or after this ISO 8601 time.')

    def handle(self, *args, **options):
        since = None
        if options['since']:
            try:
                since = erasure_replay.parse_time(options['since'])
            except ValueError:
                raise CommandError('--since is not an ISO 8601 date or time.')
        lines = erasure_replay.export(since)
        if not options['output']:
            for line in lines:
                self.stdout.write(line)
            return
        count = 0
        with open(options['output'], 'w', encoding='utf-8') as out:
            for line in lines:
                out.write(line + '\n')
                count += 1
        self.stderr.write(f'{count} entr{"y" if count == 1 else "ies"} written.')
