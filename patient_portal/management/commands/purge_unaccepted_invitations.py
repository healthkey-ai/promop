"""Delete patient invitations never accepted, after the retention period (#2786).

    python manage.py purge_unaccepted_invitations             # PATIENT_INVITATION_RETENTION_DAYS
    python manage.py purge_unaccepted_invitations --days 30
    python manage.py purge_unaccepted_invitations --dry-run

See patient_portal.invitation_retention for what goes and what stays.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from patient_portal.invitation_retention import purge_unaccepted_invitations


class Command(BaseCommand):
    help = 'Delete patient invitations never accepted, and the address they left, after the retention period.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, help='Override PATIENT_INVITATION_RETENTION_DAYS for this run.')
        parser.add_argument('--dry-run', action='store_true', help='Count what would go; change nothing.')

    def handle(self, *args, days=None, dry_run=False, **options):
        days = settings.PATIENT_INVITATION_RETENTION_DAYS if days is None else days
        if days < 0:
            raise CommandError('--days must be 0 or more.')
        if days == 0:
            self.stdout.write('PATIENT_INVITATION_RETENTION_DAYS is 0: invitations are kept. Nothing to do.')
            return
        purged = purge_unaccepted_invitations(days, dry_run=dry_run)
        verb = 'Would delete' if dry_run else 'Deleted'
        self.stdout.write(
            f'{verb} {purged.invitations} invitation(s) never accepted after {days} days; '
            f'{"would clear" if dry_run else "cleared"} the address on {purged.emails_cleared} person(s).'
        )
