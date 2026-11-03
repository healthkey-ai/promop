"""Load the current loinc.org release when ours is out of date."""
from django.core.management.base import BaseCommand, CommandError

from omop_core.services.loinc_release import (
    LoincCredentialsMissing,
    LoincReleaseUnavailable,
    current_release,
    loaded_version,
    sync_release,
)


class Command(BaseCommand):
    help = (
        'Check loinc.org for the current LOINC release and load it if ours is '
        'stale. Reads LoincTable/Loinc.csv and AccessoryFiles/PartFile/Part.csv '
        'from one archive; needs LOINC_USER and LOINC_PASSWORD.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--check', action='store_true',
            help='Report the published and loaded versions without downloading.')
        parser.add_argument(
            '--force', action='store_true',
            help='Reload even when the published version is already loaded.')

    def handle(self, *args, **options):
        try:
            if options['check']:
                published = current_release()
                have = loaded_version()
                self.stdout.write(f'published: {published["version"]}')
                self.stdout.write(f'loaded:    {have or "(none)"}')
                self.stdout.write(
                    'up to date' if have == published['version'] else 'OUT OF DATE')
                return
            version = sync_release(force=options['force'], stdout=self.stdout)
            if version is None:
                return
            self.stdout.write(self.style.SUCCESS(f'LOINC {version} loaded.'))
        # Unlike the boot-time check, an operator who ran this deliberately
        # gets the failure rather than a log line.
        except (LoincCredentialsMissing, LoincReleaseUnavailable) as exc:
            raise CommandError(str(exc)) from exc
