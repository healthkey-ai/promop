from django.core.management.base import BaseCommand, CommandError

from omop_core.services.athena_vocabulary_jobs import enqueue_sync


class Command(BaseCommand):
    help = 'Durably queue an asynchronous Athena freshness check/update.'

    def add_arguments(self, parser):
        parser.add_argument('--lfs', help=(
            'Git LFS repository publishing the Athena and LOINC ZIPs; tried first.'
        ))
        parser.add_argument('--gdrive', help=(
            'Governed Athena Drive folder or ZIP URL; the fallback when --lfs is given.'
        ))

    def handle(self, *args, **options):
        # The receipt records one source URL. The worker classifies it by host
        # and, for an LFS source, falls back to ATHENA_VOCABULARY_GDRIVE_URL
        # (#2288), so the primary is what is queued.
        source = options.get('lfs') or options.get('gdrive')
        if not source:
            raise CommandError('Provide --lfs and/or --gdrive.')
        try:
            sync, created = enqueue_sync(source)
        except Exception as exc:
            raise CommandError(f'Could not queue Athena vocabulary sync: {exc}') from exc
        if created:
            self.stdout.write(self.style.SUCCESS(
                f'Queued Athena vocabulary sync {sync.pk}; deployment will not wait for it.'
            ))
        else:
            self.stdout.write(
                f'Athena vocabulary sync {sync.pk} is already {sync.outcome}; not queued twice.'
            )
