"""Bound webhook history without deleting active work or recent replay keys."""
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models
from django.utils import timezone

from patient_portal.models import InboundWebhookEvent, WebhookDelivery


class Command(BaseCommand):
    help = 'Prune terminal webhook deliveries and processed inbound events in batches.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None)
        parser.add_argument('--batch-size', type=int, default=1000)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        days = options['days'] if options['days'] is not None else settings.WEBHOOK_RETENTION_DAYS
        if days < 1:
            raise CommandError('--days must be at least 1 (longer than the signature replay window).')
        if options['batch_size'] < 1:
            raise CommandError('--batch-size must be positive.')
        cutoff = timezone.now() - timedelta(days=days)
        queries = [
            WebhookDelivery.objects.filter(
                status__in=['delivered', 'dead_letter', 'cancelled'],
                created_at__lt=cutoff, next_attempt_at__lt=cutoff,
            ).exclude(delivered_at__gte=cutoff),
            InboundWebhookEvent.objects.filter(processed_at__lt=cutoff, received_at__lt=cutoff),
        ]
        for query in queries:
            if options['dry_run']:
                self.stdout.write(f'{query.model.__name__}: would prune {query.count()}')
                continue
            deleted = 0
            # Walk forward on pk rather than re-running LIMIT from the start
            # each time. The rows this deletes are exactly the ones the partial
            # index in 0021 excludes — it covers active deliveries — so an
            # unbounded re-scan per batch is quadratic at the size retention
            # exists to handle.
            #
            # The sentinel is typed to the model's own key. WebhookDelivery is
            # keyed by UUID and InboundWebhookEvent by an integer; Django would
            # coerce a literal 0 to the nil UUID for the first, which works but
            # reads as an integer key and invites a wrong fix later.
            after = (uuid.UUID(int=0) if isinstance(query.model._meta.pk, models.UUIDField)
                     else 0)
            while True:
                ids = list(
                    query.filter(pk__gt=after).order_by('pk')
                    .values_list('pk', flat=True)[:options['batch_size']]
                )
                if not ids:
                    break
                after = ids[-1]
                count, _ = query.filter(pk__in=ids).delete()
                deleted += count
            self.stdout.write(f'{query.model.__name__}: pruned {deleted}')
