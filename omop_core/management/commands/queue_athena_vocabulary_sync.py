from django.core.management.base import BaseCommand, CommandError

from omop_core.services.athena_vocabulary_jobs import enqueue_sync


class Command(BaseCommand):
    help = 'Durably queue an asynchronous Athena freshness check/update.'

    def add_arguments(self, parser):
        parser.add_argument('--gdrive', required=True, help='Governed Athena Drive folder or ZIP URL')

    def handle(self, *args, **options):
        try:
            sync, created = enqueue_sync(options['gdrive'])
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
