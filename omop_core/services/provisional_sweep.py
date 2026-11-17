"""Write provisional rows for patients whose codes have a proposal (#2662).

The ETL does not propose concepts: Code to Concept and Concept to Code do, and
store the ranker's confidence on the mapping (``suggestion_confidence``). Rows an
ETL loads for such a code sit at concept 0 until something resolves them, and
the per-person resolver (:func:`resolve_person_source_codes`) already knows how
to make them provisional at the proposal's concept. This module decides *which
persons* to run it for, and when:

* a **sweep** over every person who holds an unresolved row, a provisional row,
  or an unresolved inventory code under a proposal that qualifies, in
  person-id chunks (``manage.py sweep_provisional_rows``, or chained Celery
  tasks);
* a **trigger** when a mapping's proposal changes (any ``.save()`` writer),
  limited to that mapping's code, so a new proposal reaches patients and a
  withdrawn one is taken back;
* a **person job** after ingest (the bulk OMOP write, the FHIR sync) so a new
  patient does not wait for a sweep.

Rules that hold for all three:

* **Off with ``PROVISIONAL_DATA_ENABLED``**: nothing is queried, queued or written.
* **Automatic only with a broker.** A trigger or an ingest hook never resolves
  rows (and so never derives a record) inside a request. With no
  ``CELERY_BROKER_URL`` they do nothing (logged at debug) and
  ``manage.py sweep_provisional_rows`` is the way to run it.
* **Only unscoped proposals qualify**, as :func:`provisional_proposal` requires:
  the per-patient inventory cannot say which hospital a code came from, so an
  organization-scoped proposal writes no provisional rows.
* **Idempotent and resumable.** A person already provisional at the current
  proposal is "already resolved"; a chunk's cursor (the last person id it
  finished) travels in the task arguments, and each chunk stops on a time
  budget, well inside ``CELERY_TASK_TIME_LIMIT``, and hands its cursor on.
* **Coalesced.** One pending sweep per code and one pending job per person (a
  cache key taken when queued and dropped when the job starts), so a batch of
  saves or a burst of ingest POSTs queues one job, not one each.
* **One resolver per person at a time**: a Postgres advisory lock on the person
  id, so two resolves cannot both see "no row yet" and both create one.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, _QUARANTINE_VOCABULARIES, _confidence_percent,
    _usable_destination, normalize_omop_table, provisional_proposal, resolve_person_source_codes,
)
from omop_core.models import (
    Concept, PatientSourceCode, Person, SourceCodeConceptMapping, SourceCodeResolveRun,
)

logger = logging.getLogger(__name__)

SWEEP_CHUNK = 200            # persons per task
CHUNK_BUDGET_SECONDS = 600   # a chunk stops here and hands on its cursor; CELERY_TASK_TIME_LIMIT is 900
SAVE_EVERY = 10              # persons between progress saves
_CODE_CHUNK = 1000           # codes per IN list
_QUALIFYING_TTL = 60         # seconds the qualifying code set is cached
_PENDING_TTL = 300           # a pending-job key outlives a lost job by this long
_LOCK_NAMESPACE = 2662       # first key of the person advisory lock

# A change to any of these on a proposal can change what its rows should be.
TRIGGER_FIELDS = frozenset({
    'target_concept', 'target_concept_id', 'suggestion_confidence', 'status', 'omop_table',
})


def _enabled() -> bool:
    return bool(settings.PROVISIONAL_DATA_ENABLED)


def _celery() -> bool:
    return bool(getattr(settings, 'CELERY_BROKER_URL', ''))


# ---- which proposals qualify -----------------------------------------------------

_QUALIFYING_KEY = 'provisional-sweep:qualifying'


def qualifying_codes(*, use_cache=False) -> dict[str, set[str]]:
    """``{table: {source_code}}`` for proposals a provisional row may be written from.

    The resolver's own rule (:func:`provisional_proposal`): unscoped,
    ``proposed``, a destination that is standard, valid and in the table's
    domain, and a confidence at or above the floor. The cached form (60 s) is for
    the ingest check, which would otherwise load the set on every POST.
    """
    if use_cache:
        cached = cache.get(_QUALIFYING_KEY)
        if cached is not None:
            return cached
    floor = settings.PROVISIONAL_CONFIDENCE_FLOOR
    codes: dict[str, set[str]] = {}
    rows = SourceCodeConceptMapping.objects.filter(
        status='proposed', organization__isnull=True,
        target_concept__isnull=False, suggestion_confidence__isnull=False,
    ).select_related('target_concept').only(
        'omop_table', 'source_code', 'suggestion_confidence', 'target_concept')
    for mapping in rows.iterator():
        table = normalize_omop_table(mapping.omop_table)
        if (table in CLINICAL_TABLES and mapping.source_code
                and _confidence_percent(mapping.suggestion_confidence) >= floor
                and _usable_destination(mapping.target_concept, table)):
            codes.setdefault(table, set()).add(mapping.source_code)
    if use_cache:
        cache.set(_QUALIFYING_KEY, codes, _QUALIFYING_TTL)
    return codes


def _chunks(values, size=_CODE_CHUNK):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _quarantine_concept_ids() -> frozenset[int]:
    if not _QUARANTINE_VOCABULARIES:
        return frozenset()
    return frozenset(Concept.objects.filter(vocabulary_id__in=_QUARANTINE_VOCABULARIES)
                     .values_list('concept_id', flat=True))


def _unresolved_q(concept_col, quarantine):
    """An unresolved row that is not entered in error (an erroneous row is never made provisional)."""
    unresolved = Q(**{concept_col: NO_MATCHING_CONCEPT_ID})
    if quarantine:
        unresolved |= Q(**{f'{concept_col}__in': quarantine})
    return unresolved & Q(is_erroneous=False)


def candidate_person_ids(*, after=0, limit=SWEEP_CHUNK, codes=None,
                         include_provisional_holders=True) -> list[int]:
    """The next *limit* person ids above *after* that a resolve could change.

    A person is a candidate when, for a code in *codes* (default: the qualifying
    proposals), they hold an unresolved row, a provisional row (a proposal may
    have moved or fallen below the floor), or an inventory code with no clinical
    row yet. With *include_provisional_holders* every person holding any
    provisional row is a candidate too, which is how a proposal that stopped
    qualifying, with no hook to say so, gets its rows taken back. One bounded
    query per table and code chunk, never one per person.
    """
    codes = qualifying_codes() if codes is None else codes
    quarantine = _quarantine_concept_ids()
    found: set[int] = set()

    def collect(queryset):
        found.update(queryset.filter(person_id__gt=after).order_by('person_id')
                     .values_list('person_id', flat=True).distinct()[:limit])

    for table, code_set in codes.items():
        model, concept_col, source_col = CLINICAL_TABLES[table]
        for chunk in _chunks(code_set):
            collect(model.objects.filter(**{f'{source_col}__in': chunk})
                    .filter(_unresolved_q(concept_col, quarantine) | Q(confidence__isnull=False)))
            has_row = model.objects.filter(
                person_id=OuterRef('person_id'), **{source_col: OuterRef('source_value')})
            collect(PatientSourceCode.objects.filter(omop_table=table, source_value__in=chunk)
                    .annotate(has_row=Exists(has_row)).filter(has_row=False))
    if include_provisional_holders:
        for model, _concept_col, _source_col in CLINICAL_TABLES.values():
            collect(model.objects.filter(confidence__isnull=False))
    return sorted(found)[:limit]


def mapping_codes(mapping) -> dict[str, set[str]]:
    """The one code a mapping trigger looks at, whether or not it still qualifies.

    A proposal that no longer qualifies still needs its rows looked at, so they
    can be taken back.
    """
    table = normalize_omop_table(mapping.omop_table)
    if table not in CLINICAL_TABLES or not mapping.source_code:
        return {}
    return {table: {mapping.source_code}}


# ---- the per-person resolve ------------------------------------------------------

def locked_resolve(person, **kwargs):
    """``resolve_person_source_codes`` with one resolver per person at a time.

    The advisory lock is taken first and held to the end of the transaction, so
    two resolves of one person run back to back: the second sees the rows the
    first made and creates none. The ETL's bulk write does not take it, so there
    is no ordering to violate.
    """
    with transaction.atomic():
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [_LOCK_NAMESPACE, person.person_id])
        return resolve_person_source_codes(person, **kwargs)


# ---- the sweep -------------------------------------------------------------------

def _bump(run, **counts):
    for name, amount in counts.items():
        setattr(run, name, getattr(run, name) + amount)


_RUN_FIELDS = ['done', 'total', 'resolved', 'created', 'provisional', 'reverted',
               'skipped', 'already_resolved', 'errors', 'error_detail']


def start_run_record(user=None, total=0):
    return SourceCodeResolveRun.objects.create(total=total, created_by=user)


def _finish(run, notes=None, state=SourceCodeResolveRun.COMPLETED):
    run.state = state
    run.finished_at = timezone.now()
    if notes:
        run.code_details = notes
    run.save(update_fields=['state', 'finished_at', 'code_details', 'error_detail'])


def run_sweep(run_id, *, after=0, mapping_id=None, limit=SWEEP_CHUNK,
              budget_seconds=CHUNK_BUDGET_SECONDS):
    """Resolve one chunk of candidate persons into *run*; the next cursor, or None when done.

    A person that fails is recorded and skipped, not fatal, as in the bulk
    resolve. Progress is saved every few persons, the chunk stops on its time
    budget (or a soft time limit) and returns the cursor it reached, so a task
    killed or paused loses at most a few persons. With the switch off nothing is
    done and the run ends at once.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    run = SourceCodeResolveRun.objects.filter(pk=run_id).first()
    if run is None:
        logger.error('SourceCodeResolveRun %s not found.', run_id)
        return None
    if not _enabled():
        _finish(run, [{'reason': 'PROVISIONAL_DATA_ENABLED is off; nothing was written.'}])
        return None
    mapping = None
    if mapping_id is not None:
        mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
        if mapping is None:
            _finish(run, [{'reason': f'Mapping {mapping_id} no longer exists.'}])
            return None
        if not after:
            # The pending mark covers queued-but-not-started; a change from here
            # on queues a new sweep.
            _release_pending(_code_key(mapping))
    run.state = SourceCodeResolveRun.RUNNING
    run.save(update_fields=['state'])

    ids = candidate_person_ids(
        after=after, limit=limit,
        codes=mapping_codes(mapping) if mapping else None,
        include_provisional_holders=mapping is None,
    )
    started = time.monotonic()
    last_done, errors, processed = after, [], 0
    try:
        for pid in ids:
            if processed and time.monotonic() - started > budget_seconds:
                break
            try:
                person = Person.objects.get(person_id=pid)
                kwargs = {} if mapping is None else {
                    'source_values': [mapping.source_code],
                    'omop_tables': [normalize_omop_table(mapping.omop_table)]}
                result = locked_resolve(person, **kwargs)
                _bump(run, resolved=result['resolved'], created=result.get('created', 0),
                      provisional=result.get('provisional', 0), reverted=result.get('reverted', 0),
                      skipped=result['skipped'], already_resolved=result['already_resolved'])
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 -- one person must not stop the sweep
                logger.exception('Provisional sweep failed for person %s in run %s', pid, run_id)
                _bump(run, errors=1)
                errors.append({'person_id': pid, 'error': str(exc)[:200]})
            _bump(run, done=1)
            last_done, processed = pid, processed + 1
            if processed % SAVE_EVERY == 0:
                _save_progress(run, errors)
                errors = []
    except SoftTimeLimitExceeded:
        logger.warning('Provisional sweep %s hit its soft time limit after person %s.', run_id, last_done)
        _save_progress(run, errors)
        return last_done if processed else after
    _save_progress(run, errors)
    if processed == len(ids) and len(ids) < limit:
        _finish(run)
        return None
    return last_done


def _save_progress(run, errors):
    run.total = max(run.total, run.done)
    if errors:
        run.error_detail = (run.error_detail + errors)[:100]
    run.save(update_fields=_RUN_FIELDS)


def start_sweep(*, mapping=None, user=None, limit=SWEEP_CHUNK):
    """Start a sweep of everything, or of one mapping's code; the run, or None when off.

    With a broker the chunks chain from a task, each queuing the next with its
    cursor. With none they run here, to the end: that is the command's path, and
    it is never reached from a request.
    """
    if not _enabled():
        return None
    run = start_run_record(user)
    mapping_id = mapping.pk if mapping is not None else None
    if _celery():
        def enqueue():
            try:
                from omop_core.tasks import provisional_sweep_task
                provisional_sweep_task.delay(str(run.id), 0, mapping_id, limit)
            except Exception as exc:  # noqa: BLE001 -- a queue outage must be visible on the run
                logger.exception('Could not queue the provisional sweep %s.', run.id)
                run.error_detail = [{'error': f'Could not queue the sweep: {str(exc)[:200]}'}]
                _finish(run, state=SourceCodeResolveRun.FAILED)
        transaction.on_commit(enqueue, robust=True)
        return run
    after = 0
    while after is not None:
        after = run_sweep(run.id, after=after, mapping_id=mapping_id, limit=limit)
    return run


# ---- the mapping trigger ---------------------------------------------------------

def _code_key(mapping) -> str:
    return f'provisional-sweep:code:{normalize_omop_table(mapping.omop_table)}:{(mapping.source_code or "").upper()}'


def _take_pending(key) -> bool:
    """True when no job is pending for *key*, and marks one pending (``cache.add`` is atomic)."""
    return bool(cache.add(key, 1, _PENDING_TTL))


def _release_pending(key):
    cache.delete(key)


def _state(mapping) -> tuple | None:
    """What a mapping says about its code, for deciding whether a save changed the answer."""
    if mapping is None:
        return None
    qualifies = (
        mapping.status == 'proposed' and mapping.organization_id is None
        and mapping.target_concept_id is not None and mapping.suggestion_confidence is not None
        and _confidence_percent(mapping.suggestion_confidence) >= settings.PROVISIONAL_CONFIDENCE_FLOOR
        and normalize_omop_table(mapping.omop_table) in CLINICAL_TABLES
    )
    return (qualifies, mapping.target_concept_id,
            None if mapping.suggestion_confidence is None else _confidence_percent(mapping.suggestion_confidence),
            normalize_omop_table(mapping.omop_table))


def mapping_pre_save(sender, instance, **kwargs):
    """Remember what the mapping said before this save (one query, only with the switch on)."""
    if not _enabled() or instance.pk is None:
        instance._provisional_before = None
        return
    before = SourceCodeConceptMapping.objects.filter(pk=instance.pk).first()
    instance._provisional_before = _state(before)


def mapping_saved(sender, instance, created=False, update_fields=None, **kwargs):
    """post_save receiver: queue a sweep of the code when the proposal's answer changed.

    Queued when the mapping qualifies now or did before and the answer (qualifies,
    destination, confidence, table) differs, so a new or changed proposal reaches
    patients and a withdrawn one (rejected, destination cleared, below the
    floor) is taken back, while a save that only changed notes queues nothing.
    """
    if not _enabled():
        return
    if update_fields is not None and not (set(update_fields) & TRIGGER_FIELDS):
        return
    before = getattr(instance, '_provisional_before', None)
    now = _state(instance)
    if (before is None or not before[0]) and not now[0]:
        return
    if before == now:
        return
    cache.delete(_QUALIFYING_KEY)
    queue_mapping_sweep(instance)


def queue_mapping_sweep(mapping) -> None:
    """Queue a sweep of the persons holding this mapping's code, after commit.

    Only with a broker (never inline in a request), and coalesced: one pending
    sweep per code, so a batch of saves for one code queues one.
    """
    if not _enabled():
        return
    if not _celery():
        logger.debug('Provisional sweep for mapping %s not queued: no broker. '
                     'Run manage.py sweep_provisional_rows.', mapping.pk)
        return
    if not _take_pending(_code_key(mapping)):
        return
    mapping_id = mapping.pk
    transaction.on_commit(lambda: dispatch_mapping_sweep(mapping_id), robust=True)


def dispatch_mapping_sweep(mapping_id):
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None:
        return None
    return start_sweep(mapping=mapping)


# ---- after ingest ----------------------------------------------------------------

def person_needs_resolve(person_id) -> bool:
    """Whether resolving this person would change anything.

    True for an unresolved row (not entered in error) or an inventory code with
    no row under a qualifying proposal, and for a provisional row that no longer
    matches its code's proposal (moved, or no longer qualifying). A person
    already provisional at the current proposal is not. Uses the cached
    qualifying set and the resolver's own proposal rule, so it never says yes for
    a proposal the resolver would reject.
    """
    codes = qualifying_codes(use_cache=True)
    if not codes:
        # Nothing qualifies: only a provisional row left behind needs taking back.
        return any(model.objects.filter(person_id=person_id, confidence__isnull=False).exists()
                   for model, _c, _s in CLINICAL_TABLES.values())
    quarantine = _quarantine_concept_ids()
    for table, code_set in codes.items():
        model, concept_col, source_col = CLINICAL_TABLES[table]
        rows = model.objects.filter(person_id=person_id)
        for chunk in _chunks(code_set):
            held = list(rows.filter(**{f'{source_col}__in': chunk})
                        .filter(_unresolved_q(concept_col, quarantine) | Q(confidence__isnull=False))
                        .values_list(source_col, concept_col, 'confidence').distinct())
            for code, concept_id, confidence in held:
                proposal = provisional_proposal(code, table)
                if confidence is None:
                    if proposal is not None:
                        return True           # unresolved, and a proposal qualifies
                    continue
                if proposal is None or (proposal[0].pk, proposal[1]) != (concept_id, confidence):
                    return True               # provisional, but the proposal moved or went
            has_row = model.objects.filter(
                person_id=OuterRef('person_id'), **{source_col: OuterRef('source_value')})
            if PatientSourceCode.objects.filter(person_id=person_id, omop_table=table,
                                                source_value__in=chunk).annotate(
                    has_row=Exists(has_row)).filter(has_row=False).exists():
                return True
    # A provisional row under a code that no longer qualifies at all.
    return any(model.objects.filter(person_id=person_id, confidence__isnull=False)
               .exclude(**{f'{source_col}__in': sorted(codes.get(table, ()))}).exists()
               for table, (model, _c, source_col) in CLINICAL_TABLES.items())


def _person_key(person_id) -> str:
    return f'provisional-sweep:person:{person_id}'


def queue_person_resolve(person_id) -> None:
    """After a person's rows are written: resolve them if a proposal covers any.

    A no-op when the switch is off or there is no broker, and when nothing the
    person holds would change, so the common ingest costs one cheap check.
    Coalesced per person: twenty POSTs for one patient queue one job. Queued
    after commit; a failed check never fails an ingest.
    """
    if not _enabled():
        return
    if not _celery():
        logger.debug('Provisional resolve of person %s not queued: no broker.', person_id)
        return
    try:
        if not person_needs_resolve(person_id) or not _take_pending(_person_key(person_id)):
            return
    except Exception:  # noqa: BLE001
        logger.exception('Provisional check failed for person %s.', person_id)
        return
    transaction.on_commit(lambda: dispatch_person_resolve(person_id), robust=True)


def dispatch_person_resolve(person_id):
    try:
        from omop_core.tasks import provisional_resolve_person_task
        provisional_resolve_person_task.delay(person_id)
    except Exception:  # noqa: BLE001 -- the next ingest or the sweep will pick the person up
        logger.exception('Could not queue the provisional resolve of person %s.', person_id)
        _release_pending(_person_key(person_id))


def run_person_resolve(person_id):
    """The job body: drop the pending mark first, so a later write queues another job."""
    _release_pending(_person_key(person_id))
    if not _enabled():
        return None
    person = Person.objects.filter(person_id=person_id).first()
    if person is None:
        return None
    return locked_resolve(person)
