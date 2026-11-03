"""Boot-time LOINC staleness check: queue a load if needed, never fail."""
from django.core.management.base import BaseCommand

from omop_core.services.loinc_release import dispatch_release_sync


class Command(BaseCommand):
    help = (
        'Check whether the loaded LOINC release is current and queue a load if '
        'not. Intended for the deployment bootstrap: it reports problems and '
        'exits zero, because a stale LOINC table is not a reason to refuse to '
        'serve.'
    )

    def handle(self, *args, **options):
        if dispatch_release_sync():
            self.stdout.write('  Queued a LOINC release load.')
        else:
            self.stdout.write('  No LOINC load needed.')
