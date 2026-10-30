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
from django.db.models import Q
from django.utils import timezone

from omop_core.mapping import axis_evidence, axis_variants
from omop_core.models import AxisVariantRefresh

logger = logging.getLogger(__name__)

ACTIVE_OUTCOMES = ('queued', 'running')
#: The worker task's hard limit, shared with ``refresh_axis_variants_task``.
TIME_LIMIT = 6 * 60 * 60
#: A running request whose heartbeat is older than this lost its worker: a
#: deploy replaced the pool mid-walk (#2023). A batch of 500 keys takes far
#: less, so the next sweep, at worker boot or on the worker's five-minute
#: recheck (#2048), requeues it and the walk resumes after its ``cursor``.
STALE = 15 * 60
#: Raised inside the task shortly before the hard limit, so an overrun is
#: recorded as ``failed`` instead of a row stuck in ``running``.
SOFT_TIME_LIMIT = TIME_LIMIT - 5 * 60


def task_id_for(request):
    return f'axis-variant-refresh-{request.pk}'


def request_refresh(reason='', rebuild_evidence=False):
    """Record a refresh request and return ``(request, created)``.

    At most one request is active: a second one while the first is queued or
    running would walk the same codes again. The partial unique constraint on
    the row closes the race between two callers. A rebuild of the unit
    evidence (#2043) asked for while a plain refresh is still queued is folded
    into it. Publishing is best effort here. A broker that is missing or
    unreachable leaves the row queued for the worker's own sweep
    (:func:`dispatch_queued_refreshes`), or for ``manage.py
    refresh_axis_variants`` run by hand (:func:`run_pending_inline`).
    """
    active = AxisVariantRefresh.objects.filter(outcome__in=ACTIVE_OUTCOMES).order_by('pk').first()
    if active is not None:
        # Folded only while still queued: a worker that has claimed the row
        # has read the flag already. The caller sees what the row now says.
        if rebuild_evidence and not active.rebuild_evidence and active.outcome == 'queued' \
                and AxisVariantRefresh.objects.filter(pk=active.pk, outcome='queued').update(rebuild_evidence=True):
            active.rebuild_evidence = True
        return active, False
    try:
        with transaction.atomic():
            request = AxisVariantRefresh.objects.create(reason=reason, rebuild_evidence=rebuild_evidence)
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
    ``running`` with no heartbeat for :data:`STALE` lost its worker, most often
    to the deploy that booted this one, and is requeued with its ``done``
    offset and ``cursor`` kept, so the walk resumes rather than restarts. The
    rechecks repeat for the life of the worker (``tasks.keep_rechecking``),
    because the kill can come after the replacement booted (#2048).
    """
    cutoff = timezone.now() - timedelta(seconds=STALE)
    stranded = AxisVariantRefresh.objects.filter(outcome='running').filter(
        Q(progress_at__lt=cutoff) | Q(progress_at__isnull=True, started_at__lt=cutoff))
    waiting = AxisVariantRefresh.objects.filter(outcome='queued').order_by('pk').first()
    if waiting is not None:
        # Only one request may be queued, and the waiting one will walk the
        # same codes, so the stranded one is closed rather than requeued. A
        # rebuild it was asked for is carried over, not dropped (#2048).
        if stranded.filter(rebuild_evidence=True).exists():
            AxisVariantRefresh.objects.filter(pk=waiting.pk, outcome='queued').update(rebuild_evidence=True)
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


def requeue_on_shutdown():
    """Move a running refresh back to queued ahead of the stale sweep.

    Called from ``worker_shutting_down``: the worker knows it is dying, so
    waiting for the heartbeat to go stale wastes 15-20 minutes on the
    replacement worker.  The cursor, done offset and rebuild flag are kept,
    so the next worker resumes rather than restarts.
    """
    requeued = AxisVariantRefresh.objects.filter(outcome='running').update(
        outcome='queued', task_id='', started_at=None)
    if requeued:
        logger.info('Worker shutting down: requeued %d running refresh request(s).', requeued)
    return requeued


def run_pending_inline(reason='', rebuild_evidence=False):
    """Perform the refresh here, completing a queued request if there is one.

    The path for an instance with no worker: ``manage.py refresh_axis_variants``
    without ``--queue``. A request a migration left queued is claimed and
    closed rather than left to block every later request; a rebuild asked
    for here is folded into it.
    """
    request = AxisVariantRefresh.objects.filter(outcome='queued').order_by('pk').first()
    if request is None:
        request = AxisVariantRefresh.objects.create(reason=reason, rebuild_evidence=rebuild_evidence)
    elif rebuild_evidence and not request.rebuild_evidence:
        AxisVariantRefresh.objects.filter(pk=request.pk, outcome='queued').update(rebuild_evidence=True)
    return run_refresh(request.pk)


def run_refresh(request_id, batch_size=axis_variants.REFRESH_BATCH):
    """Perform one request on the worker and return a summary.

    Claims the row first: a request that is not ``queued`` is running or
    finished elsewhere and is left alone. Progress, a heartbeat and the last
    key walked are written after every batch, so an operator can watch
    ``done`` against ``keys`` and the sweep can tell a killed walk from a live
    one. A requeued request resumes after its ``cursor``: keys are walked in
    one fixed order, and the codes up to the cursor were done by the attempt
    that was killed. The cursor, not an offset, because a rebuild changes the
    key set as it goes (a code left with no units leaves it), which would
    shift an offset onto codes never walked (#2043).
    """
    now = timezone.now()
    claimed = AxisVariantRefresh.objects.filter(pk=request_id, outcome='queued').update(
        outcome='running', started_at=now, progress_at=now, completed_at=None, failure_reason='')
    if not claimed:
        request = AxisVariantRefresh.objects.filter(pk=request_id).first()
        return {'request_id': request_id, 'skipped': True,
                'outcome': request.outcome if request is not None else 'missing'}
    request = AxisVariantRefresh.objects.get(pk=request_id)
    offset, already, cursor = request.done, request.refreshed, request.cursor
    # Every write below is this attempt's: should the sweep decide this walk
    # is dead and hand the request to another worker, the other attempt
    # claims it with a new ``started_at`` and this one's writes land nowhere
    # (#2048). The walk itself is idempotent, so running on costs only time.
    attempt = AxisVariantRefresh.objects.filter(pk=request_id, outcome='running', started_at=now)

    # A rebuild also counts the axis evidence it meets (#2051); the summary
    # is written with every batch, so a resumed walk continues its counts.
    summary = axis_evidence.Summary(request.axis_evidence) if request.rebuild_evidence else None

    def progress(done, refreshed, last_key):
        attempt.update(
            done=offset + done, refreshed=already + refreshed, progress_at=timezone.now(),
            cursor=axis_variants.key_text(last_key),
            **({'axis_evidence': summary.as_dict()} if summary is not None else {}))

    try:
        # A rebuild walks every code with evidence or patient data and rewrites
        # its stored units from that data first (#2043); a plain refresh walks
        # only the codes seen in several units.
        if request.rebuild_evidence and not cursor:
            # The Seen counts are set for every code in one pass before the
            # walk (#2121); a resumed walk did it already.
            axis_variants.rebuild_seen_counts()
        keys = axis_variants.evidence_keys() if request.rebuild_evidence else axis_variants.multi_unit_keys()
        keys = sorted(keys, key=axis_variants.key_text)
        remaining = [k for k in keys if axis_variants.key_text(k) > cursor] if cursor else keys
        # Collecting the keys is the longest stretch with no batch to report,
        # so it is a heartbeat of its own.
        attempt.update(keys=offset + len(remaining), progress_at=timezone.now())
        if cursor:
            logger.info('Axis variant refresh %s resumes after %s with %s codes left.',
                        request_id, cursor.replace('\x1f', ':'), len(remaining))
        refreshed = already + axis_variants.refresh_all(
            remaining, batch_size=batch_size, on_batch=progress,
            rebuild_evidence=request.rebuild_evidence, summary=summary)
        keys = offset + len(remaining)
    except Exception as exc:
        attempt.update(outcome='failed', failure_reason=str(exc)[:2000], completed_at=timezone.now())
        raise
    attempt.update(outcome='done', completed_at=timezone.now())
    logger.info('Axis variant refresh %s walked %s codes and refreshed %s mappings%s.',
                request_id, keys, refreshed,
                ', rebuilding their unit evidence from patient data' if request.rebuild_evidence else '')
    return {'request_id': request_id, 'skipped': False, 'outcome': 'done',
            'keys': keys, 'refreshed': refreshed, 'rebuild_evidence': request.rebuild_evidence}
