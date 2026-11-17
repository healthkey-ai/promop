"""Delete vote submissions nobody confirmed in time (#2678).

A submission from a public voting page waits 48 hours for its emailed
confirmation. Each new submission clears the expired ones as it arrives, so
this command only matters on an instance where nobody has submitted since:
run it from a scheduler, or by hand, to keep unconfirmed addresses from
sitting in the table. It never touches a confirmed vote.
"""
from django.core.management.base import BaseCommand

from omop_core.services import mapping_votes


class Command(BaseCommand):
    help = 'Delete expired, unconfirmed vote submissions and old confirmation-email records.'

    def handle(self, *args, **options):
        deleted = mapping_votes.purge_expired()
        self.stdout.write(f'Deleted {deleted} expired vote submission(s).')
