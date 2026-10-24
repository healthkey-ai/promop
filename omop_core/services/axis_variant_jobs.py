"""The bulk LOINC axis variant refresh as a worker job (#2021).

Migrations 0300 and 0301 ran ``axis_variants.refresh_all()`` inside
``migrate``. On Render staging that took minutes. On HealthTree's GCP
instance, which holds the data with the most multi-unit codes, it outlived the
Cloud Run migrate job's one-hour task limit on every attempt, so no deploy
reached traffic for three days. A backfill whose size depends on the clinical
tables belongs on the worker, behind a durable request, like the Athena sync
and the hospital-code seed.

The request row is the intent. ``migrate`` writes it and publishes when it
can; a worker that knows the task publishes whatever is still queued when it
boots, so a message an older worker discarded as unregistered during a
rolling deployment is not lost. The task claims the row atomically, so a
duplicate publish runs nothing twice.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from omop_core.mapping import axis_variants
from omop_core.models import AxisVariantRefresh

logger = logging.getLogger(__name__)

ACTIVE_OUTCOMES = ('queued', 'running')
#: The worker task's hard limit, shared with ``refresh_axis_variants_task`` so
#: the stranded-row cutoff below cannot drift from it. A ``running`` row older
#: than this was left behind by a worker that died, and is requeued.
TIME_LIMIT = 6 * 60 * 60
#: Raised inside the task shortly before the hard limit, so an overrun is
#: recorded as ``failed`` instead of a row stuck in ``running``.
SOFT_TIME_LIMIT = TIME_LIMIT - 5 * 60


def task_id_for(request):
    return f'axis-variant-refresh-{request.pk}'


def request_refresh(reason=''):
    """Record a refresh request and return ``(request, created)``.

    At most one request is active: a second one while the first is queued or
    running would walk the same codes again. The partial unique constraint on
    the row closes the race between two callers. Publishing is best effort
    here. A broker that is missing or unreachable leaves the row queued for the
    worker's own sweep (:func:`dispatch_queued_refreshes`), or for
    ``manage.py refresh_axis_variants`` run by hand (:func:`run_pending_inline`).
    """
    active = AxisVariantRefresh.objects.filter(outcome__in=ACTIVE_OUTCOMES).order_by('pk').first()
    if active is not None:
        return active, False
    try:
        with transaction.atomic():
            request = AxisVariantRefresh.objects.create(reason=reason)
    except IntegrityError:
        return AxisVariantRefresh.objects.filter(outcome__in=ACTIVE_OUTCOMES).order_by('pk').first(), False
    publish(request)
    return request, True


def publish(request):
    """Publish ``request`` to the worker. Returns whether it was published."""
    if not getattr(settings, 'CELERY_BROKER_URL', ''):
        logger.warning(
            'No Celery broker: axis variant refresh %s stays queued. '
            '`manage.py refresh_axis_variants` performs it here.', request.pk)
        return False
    from omop_core.tasks import refresh_axis_variants_task

    try:
        refresh_axis_variants_task.apply_async(args=[request.pk], task_id=task_id_for(request))
    except Exception:
        logger.exception('Could not publish axis variant refresh %s; it stays queued.', request.pk)
        return False
    request.task_id = task_id_for(request)
    AxisVariantRefresh.objects.filter(pk=request.pk, outcome='queued').update(task_id=request.task_id)
    return True


def dispatch_queued_refreshes(republish=True):
    """Publish queued requests. Called when a worker becomes ready.

    A request ``migrate`` published may have reached a worker from before this
    task existed, which dropped it. So the pass a worker runs on boot
    republishes queued rows whether or not they carry a task id; the claim in
    :func:`run_refresh` makes a duplicate harmless. The later rechecks pass
    ``republish=False`` and publish only rows nothing has published yet, so a
    request waiting behind a busy worker is not sent three times. A request
    stranded in ``running`` past the task's hard limit lost its worker and is
    requeued; the refresh is idempotent.
    """
    cutoff = timezone.now() - timedelta(seconds=TIME_LIMIT)
    stranded = AxisVariantRefresh.objects.filter(outcome='running', started_at__lt=cutoff)
    waiting = AxisVariantRefresh.objects.filter(outcome='queued').order_by('pk').first()
    if waiting is not None:
        # Only one request may be queued, and the waiting one will walk the
        # same codes, so the stranded one is closed rather than requeued.
        stranded.update(outcome='failed', completed_at=timezone.now(),
                        failure_reason=f'Worker lost; request {waiting.pk} replaces it.')
    else:
        stranded.update(outcome='queued', task_id='', started_at=None)
    queued = AxisVariantRefresh.objects.filter(outcome='queued').order_by('pk')
    if not republish:
        queued = queued.filter(task_id='')
    dispatched = 0
    for request in queued:
        if publish(request):
            dispatched += 1
    return dispatched


def run_pending_inline(reason=''):
    """Perform the refresh here, completing a queued request if there is one.

    The path for an instance with no worker: ``manage.py refresh_axis_variants``
    without ``--queue``. A request a migration left queued is claimed and
    closed rather than left to block every later request.
    """
    request = AxisVariantRefresh.objects.filter(outcome='queued').order_by('pk').first()
    if request is None:
        request = AxisVariantRefresh.objects.create(reason=reason)
    return run_refresh(request.pk)


def run_refresh(request_id, batch_size=axis_variants.REFRESH_BATCH):
    """Perform one request on the worker and return a summary.

    Claims the row first: a request that is not ``queued`` is running or
    finished elsewhere and is left alone. Progress is written after every
    batch, so an operator can watch ``done`` against ``keys``.
    """
    claimed = AxisVariantRefresh.objects.filter(pk=request_id, outcome='queued').update(
        outcome='running', started_at=timezone.now(), completed_at=None, failure_reason='')
    if not claimed:
        request = AxisVariantRefresh.objects.filter(pk=request_id).first()
        return {'request_id': request_id, 'skipped': True,
                'outcome': request.outcome if request is not None else 'missing'}

    def progress(done, refreshed):
        AxisVariantRefresh.objects.filter(pk=request_id).update(done=done, refreshed=refreshed)

    try:
        keys = axis_variants.multi_unit_keys()
        AxisVariantRefresh.objects.filter(pk=request_id).update(keys=len(keys))
        refreshed = axis_variants.refresh_all(keys, batch_size=batch_size, on_batch=progress)
    except Exception as exc:
        AxisVariantRefresh.objects.filter(pk=request_id).update(
            outcome='failed', failure_reason=str(exc)[:2000], completed_at=timezone.now())
        raise
    AxisVariantRefresh.objects.filter(pk=request_id).update(
        outcome='done', completed_at=timezone.now())
    logger.info('Axis variant refresh %s walked %s codes and refreshed %s mappings.',
                request_id, len(keys), refreshed)
    return {'request_id': request_id, 'skipped': False, 'outcome': 'done',
            'keys': len(keys), 'refreshed': refreshed}
