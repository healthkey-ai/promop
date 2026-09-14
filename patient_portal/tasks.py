"""Durable, bounded webhook delivery with recovery after worker/broker outages."""

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from patient_portal.models import WebhookDelivery
from patient_portal.webhooks import enqueue_delivery, send_webhook

MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30


@shared_task(soft_time_limit=45, time_limit=60)
def deliver_webhook(delivery_id):
    if not settings.WEBHOOKS_ENABLED:
        return
    with transaction.atomic():
        delivery = (WebhookDelivery.objects.select_for_update()
                    .select_related('subscription__organization').filter(pk=delivery_id).first())
        if delivery is None or delivery.status not in ('pending', 'retry', 'sending'):
            return
        if delivery.next_attempt_at > timezone.now():
            return
        if not delivery.subscription.active or not delivery.subscription.organization.is_active:
            delivery.status = 'cancelled'
            delivery.save(update_fields=['status'])
            return
        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = 'dead_letter'
            delivery.save(update_fields=['status'])
            return
        delivery.attempts += 1
        delivery.status = 'sending'
        # Expired leases recover workers lost between committing an attempt and
        # recording its response. The HTTP task time limit is below this lease.
        delivery.next_attempt_at = timezone.now() + timedelta(minutes=2)
        delivery.save(update_fields=['attempts', 'status', 'next_attempt_at'])

    response_status = None
    error = ''
    try:
        response_status = send_webhook(
            delivery.subscription.url, delivery.payload,
            delivery.subscription.secret, delivery.pk,
        )
        if not 200 <= response_status < 300:
            error = 'http_error'
    except ValueError:
        error = 'unsafe_destination'
    except Exception:
        # Exception text and response bodies can contain URL credentials or PHI.
        error = 'connection_error'

    with transaction.atomic():
        current = WebhookDelivery.objects.select_for_update().filter(pk=delivery_id).first()
        if current is None or current.attempts != delivery.attempts or current.status != 'sending':
            return
        current.response_status = response_status
        current.error = error
        delay = RETRY_BASE_SECONDS * 2 ** (current.attempts - 1)
        if not error:
            current.status = 'delivered'
            current.delivered_at = timezone.now()
        elif current.attempts >= MAX_ATTEMPTS:
            current.status = 'dead_letter'
        else:
            current.status = 'retry'
            current.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            transaction.on_commit(lambda: enqueue_delivery(current.pk, countdown=delay))
        current.save()


@shared_task
def dispatch_pending_webhooks():
    if not settings.WEBHOOKS_ENABLED:
        return
    due = WebhookDelivery.objects.filter(
        status__in=['pending', 'retry', 'sending'], next_attempt_at__lte=timezone.now(),
    ).order_by('next_attempt_at', 'pk').values_list('pk', flat=True)[:1000]
    for delivery_id in due:
        enqueue_delivery(delivery_id)
