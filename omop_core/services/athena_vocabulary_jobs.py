"""Durable scheduling for long-running Athena vocabulary synchronization."""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from omop_core.models import AthenaVocabularySync


ACTIVE_OUTCOMES = ('queued', 'running')
# A running sync logs progress every few seconds and heartbeats with it; the
# longest quiet stretch inside the loader (an index refresh) is minutes. A
# worker retired by a rolling deploy mid-load never beats again.
STALE_RUNNING = timedelta(minutes=30)


def _stale_queued():
    """A queued receipt whose message the broker would already have redelivered."""
    return timedelta(seconds=settings.CELERY_BROKER_TRANSPORT_OPTIONS.get(
        'visibility_timeout', 25_200,
    ))


def stale_reason(sync, now=None):
    """Why an active receipt no longer describes live work, or ``None``."""
    now = now or timezone.now()
    if sync.outcome == 'running':
        last = sync.heartbeat_at or sync.started_at
        if now - last > STALE_RUNNING:
            return (
                f'Worker lost: no heartbeat since {last:%Y-%m-%d %H:%M:%S %Z} '
                f'(a running sync beats as it logs; limit {STALE_RUNNING}).'
            )
    elif sync.outcome == 'queued':
        if now - sync.started_at > _stale_queued():
            return (
                f'Never started: queued at {sync.started_at:%Y-%m-%d %H:%M:%S %Z}, '
                'longer than the broker keeps an unconsumed message.'
            )
    return None


def retire_stale(sync):
    """Mark a stale active receipt failed so a new one can be queued; True if it was."""
    reason = stale_reason(sync)
    if reason is None:
        return False
    updated = AthenaVocabularySync.objects.filter(
        pk=sync.pk, outcome=sync.outcome,
    ).update(outcome='failed', failure_reason=reason, completed_at=timezone.now())
    return bool(updated)


def enqueue_sync(source_url):
    """Queue at most one active sync for a source and return ``(row, created)``.

    The database row is the durable intent. Publishing happens after it commits,
    and the dedicated queue keeps an older worker from consuming a task name it
    does not know during a rolling deployment.
    """
    source_url = str(source_url).strip()
    if not source_url:
        raise ValueError('An Athena vocabulary source URL is required.')

    active = AthenaVocabularySync.objects.filter(
        source_url=source_url, outcome__in=ACTIVE_OUTCOMES,
    ).order_by('-started_at', '-pk').first()
    if active and not retire_stale(active):
        return active, False

    try:
        with transaction.atomic():
            sync = AthenaVocabularySync.objects.create(
                source_url=source_url,
                outcome='queued',
                started_at=timezone.now(),
            )
    except IntegrityError:
        # A concurrent web boot or pre-deploy won the partial unique constraint.
        return AthenaVocabularySync.objects.get(
            source_url=source_url, outcome__in=ACTIVE_OUTCOMES,
        ), False

    try:
        from omop_core.tasks import sync_athena_vocabulary_task

        result = sync_athena_vocabulary_task.apply_async(
            args=[sync.pk], queue='athena',
        )
    except Exception as exc:
        AthenaVocabularySync.objects.filter(pk=sync.pk).update(
            outcome='failed', failure_reason=str(exc), completed_at=timezone.now(),
        )
        raise

    AthenaVocabularySync.objects.filter(pk=sync.pk).update(task_id=result.id or '')
    sync.task_id = result.id or ''
    return sync, True
