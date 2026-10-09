"""Durable scheduling for long-running Athena vocabulary synchronization."""

from django.db import IntegrityError, transaction
from django.utils import timezone

from omop_core.models import AthenaVocabularySync


ACTIVE_OUTCOMES = ('queued', 'running')


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
    if active:
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
