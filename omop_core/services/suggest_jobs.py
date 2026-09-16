"""How a Code Mapping Suggest run gets executed.

A Suggest click costs ~3.5s per code and a tab holds dozens, so the synchronous
version could not finish inside a gunicorn worker's timeout — production's
``start.sh`` runs bare gunicorn, whose default is 30s. It is queued instead, and
the page polls a :class:`~omop_core.models.SuggestRun` row for progress.

The seam sits here rather than in the view, so a test can run the work without a
broker and without reaching into Celery. It deliberately mirrors
``omop_core/services/derivation_jobs.py``: same choice rule (Celery when a
broker is configured, inline otherwise), same reason for having no separate
setting — two settings that can disagree leave every job queued with nothing
consuming it.

Where this differs from derivation: the outcome is a database row, not a signed
id. Derivation only has to answer "did it finish", which an id issued after the
fact can encode. A Suggest run has to answer "how far has it got" *while it is
still running*, from whichever gunicorn worker the poll lands on, so the state
has to be somewhere both the runner and every poller can see.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from omop_core.models import SuggestRun

# What a run may attempt when it is genuinely queued: bounded by
# CELERY_TASK_TIME_LIMIT (900s default) against ~3.5s per code, with wide margin.
QUEUED_MAX_CODES = 100

# What it may attempt when there is no broker and the "queue" is the request
# thread. Deliberately far smaller: `render.yaml` leaves CELERY_BROKER_URL
# dashboard-managed on the web service (`sync: false`), so a deployment that has
# not pasted the Redis URL in yet falls back to inline — and inline runs under
# `start.sh`'s bare gunicorn, whose default timeout is 30s. Fifty codes inline is
# 125s of serial retrieval and a 502: the exact failure this work removes.
# Measured: 3 codes 9.5s, 5 codes 24.1s, 8 codes 28.7s -- and those figures are
# with the embedding model already loaded. `_get_embedding_model()` is lazy and
# `vector_rerank` is the first thing to call it, so the first request a fresh
# worker serves also pays a ~5s SentenceTransformer load. Five would put that
# first click at ~29s against a 30s timeout, which is the 502 this work exists
# to remove; three leaves room for it.
INLINE_MAX_CODES = 3


def selection_summary(params):
    """Snapshot the policy used by _suggestable_queryset_ordered for this run."""
    from omop_core.mapping.suggestions import SUGGESTION_MODEL_VERSION

    return {
        'order': (
            'Codes without destinations first, regardless of provenance, ordered '
            'by Seen count highest first. Then eligible replacements not yet tried '
            'by this model version, followed by previously tried replacements, '
            'each ordered by Seen count highest first. Ties prefer untried codes, '
            'then source code ascending, then mapping row ID ascending.'
        ),
        'eligibility': (
            'Proposed mappings only. Approved and rejected mappings are excluded. '
            'Replacement mode also allows destinations set by previous suggestions; '
            'imported destinations are not replaced.'
        ),
        'limit': params['limit'],
        'source_vocabulary_id': params['source_vocabulary_id'],
        'tables': params.get('tables') or [],
        'strategies': params['strategies'],
        'resuggest': params['resuggest'],
        'dry_run': params['dry_run'],
        'model_version': SUGGESTION_MODEL_VERSION,
    }


class SuggestDispatcher(Protocol):
    #: Ceiling on the codes one run may attempt under this dispatcher.
    max_codes: int

    def dispatch(self, run: SuggestRun, params: dict) -> None:
        """Arrange for *run* to be executed."""


class CeleryDispatcher:
    """Queues the run on a worker."""

    max_codes = QUEUED_MAX_CODES

    def dispatch(self, run: SuggestRun, params: dict) -> None:
        from omop_core.tasks import suggest_mappings_task

        run_id = str(run.id)
        # Deferred to commit: a worker that starts inside the caller's open
        # transaction cannot see the SuggestRun row the caller just created and
        # fails looking it up.
        transaction.on_commit(
            lambda: suggest_mappings_task.apply_async(args=[run_id, params])
        )


class InlineDispatcher:
    """Runs it in the calling thread. What a machine with no broker gets.

    The wire contract is identical — the caller still gets a run id and still
    polls — so the page needs one code path either way. What it does not get is
    progress: the row goes from queued to success in one step, because nothing
    is reading it while the request is blocked.

    It also gets a much smaller ceiling, because "inline" means "inside the
    request" and the request has a timeout. See INLINE_MAX_CODES.
    """

    max_codes = INLINE_MAX_CODES

    def dispatch(self, run: SuggestRun, params: dict) -> None:
        execute_run(str(run.id), params)


class FakeDispatcher:
    """Records what it was asked to run, runs nothing. For tests."""

    max_codes = QUEUED_MAX_CODES

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def dispatch(self, run: SuggestRun, params: dict) -> None:
        self.calls.append((str(run.id), params))


_celery = CeleryDispatcher()
_inline = InlineDispatcher()
_override: SuggestDispatcher | None = None


def get_dispatcher() -> SuggestDispatcher:
    """Celery when a broker is configured, inline otherwise."""
    if _override is not None:
        return _override
    return _celery if getattr(settings, 'CELERY_BROKER_URL', '') else _inline


@contextmanager
def use_dispatcher(dispatcher: SuggestDispatcher) -> Iterator[SuggestDispatcher]:
    """Swap the dispatcher for the duration of a test."""
    global _override
    previous = _override
    _override = dispatcher
    try:
        yield dispatcher
    finally:
        _override = previous


def execute_preview(run_id: str, params: dict) -> None:
    """Retrieve a dialog's candidates without changing any mapping."""
    from copy import deepcopy
    from omop_core.mapping.suggestions import suggest_one_mapping

    runs = SuggestRun.objects.filter(pk=run_id)
    if not runs.exists():
        return
    runs.update(state=SuggestRun.RUNNING)
    events = []

    def activity(event):
        events.append({'at': timezone.now().isoformat(), **deepcopy(event)})
        runs.update(activity=events, retrieved=int(event['stage'] in ('ranking', 'result')))

    try:
        result = suggest_one_mapping(**params['preview'], activity=activity)
        activity({'stage': 'result', **params['preview'], **result, 'dry_run': True, 'updated': False})
        runs.update(state=SuggestRun.SUCCESS, done=1, retrieved=1, finished_at=timezone.now())
    except Exception as exc:  # noqa: BLE001 - retain partial candidates on failure
        activity({'stage': 'failure', 'note': str(exc)[:2000]})
        runs.update(state=SuggestRun.FAILURE, error=str(exc)[:2000], finished_at=timezone.now())


def execute_run(run_id: str, params: dict) -> None:
    """Do the work for one SuggestRun and record how it went.

    Never raises: a failure belongs on the row, where the page is already
    looking, rather than in a worker log the curator cannot see.
    """
    if 'preview' in params:
        execute_preview(run_id, params)
        return

    from omop_core.mapping.suggestions import (
        SUGGESTION_MODEL_VERSION, suggest_mappings, suggestable_queryset,
    )

    run = SuggestRun.objects.filter(pk=run_id).first()
    if run is None:
        return

    SuggestRun.objects.filter(pk=run.pk).update(
        state=SuggestRun.RUNNING, model_version=SUGGESTION_MODEL_VERSION,
        selection=selection_summary(params),
    )

    events = list(run.activity or [])

    def activity(event):
        # All callbacks run on this thread. Persist immediately so other web
        # processes can show the current code, and partial logs survive failure.
        events.append({'at': timezone.now().isoformat(), **event})
        SuggestRun.objects.filter(pk=run.pk).update(activity=events)

    # One call for the whole run. The rows carry their own clinical table, so
    # there is nothing to iterate per table -- and iterating applied `limit` to
    # each of them, letting a tab that maps to five tables evaluate five times
    # its ceiling.
    total = run.total

    # `run.total` was counted in the request, before dispatch. Ingest creates
    # queue rows continuously, so by the time a worker picks the job up the real
    # count can differ -- and clamping progress to a stale total left the bar at
    # 100% with "Searching for candidates..." beside it. Phase 1 knows the true
    # number, so take it from there.
    counted = {'total': run.total}

    def progress(stage, done, table_total):
        if table_total != counted['total']:
            counted['total'] = table_total
            SuggestRun.objects.filter(pk=run.pk).update(total=table_total)
        field = 'retrieved' if stage == 'retrieving' else 'done'
        SuggestRun.objects.filter(pk=run.pk).update(
            **{field: min(done, counted['total']) if counted['total'] else done}
        )

    try:
        results = suggest_mappings(
            params.get('tables') or None,
            min_occurrences=params['min_occurrences'],
            limit=params['limit'],
            dry_run=params['dry_run'],
            source_vocabulary_id=params['source_vocabulary_id'],
            strategies=params['strategies'],
            lexical_limit=params['lexical_limit'],
            resuggest=params['resuggest'],
            progress=progress,
            activity=activity,
        )
    except Exception as exc:                      # noqa: BLE001 - record, never crash the worker
        activity({'stage': 'failure', 'note': str(exc)[:2000]})
        SuggestRun.objects.filter(pk=run.pk).update(
            state=SuggestRun.FAILURE, error=str(exc)[:2000],
            finished_at=timezone.now(),
        )
        return

    landed: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    for entry in results:
        # Only rows that actually got a destination. `updated` means the row was
        # written -- which includes recording that the ranker declined -- so
        # counting that here would claim destinations nobody proposed.
        suggested = entry.get('suggested')
        if entry.get('updated') and suggested:
            vocab = suggested['vocabulary_id'] or (params['source_vocabulary_id'] or '')
            landed[vocab] = landed.get(vocab, 0) + 1
        # Gated on a destination for the same reason `landed` is: a code the
        # ranker declined, or one Athena already supplies, resolved to nothing,
        # and counting it made the banner claim a strategy had answered it.
        strategy = entry.get('strategy_used')
        if strategy and suggested:
            strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    # What a *next* run would newly work on: eligible, and not already attempted
    # by this model version. Counting every eligible row instead would include
    # the codes this run just declined -- still without a destination, but
    # re-running only re-declines them, so "run Suggest again" would be advice
    # that goes nowhere. Once every code has been tried this reads 0, which is
    # the honest answer: the next thing to move it is a new model version.
    try:
        remaining = suggestable_queryset(
            params.get('tables') or None,
            source_vocabulary_id=params['source_vocabulary_id'],
            min_occurrences=params['min_occurrences'],
            resuggest=params['resuggest'],
        ).exclude(last_suggest_attempt=SUGGESTION_MODEL_VERSION).count()
    except Exception:                             # noqa: BLE001 - a count must not fail a run
        remaining = 0

    activity({'stage': 'completed', 'note': f'Finished processing {len(results)} code(s).'})
    SuggestRun.objects.filter(pk=run.pk).update(
        state=SuggestRun.SUCCESS,
        total=len(results),
        remaining=remaining,
        retrieved=len(results),
        done=len(results),
        destinations=sum(1 for r in results if r.get('updated') and r.get('suggested')),
        strategy_counts=strategy_counts,
        landed_in=landed,
        finished_at=timezone.now(),
    )
