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
  ``CELERY_BROKER_URL`` they do nothing (logged once) and
  ``manage.py sweep_provisional_rows`` is the way to run it.
* **Only unscoped proposals qualify**, as :func:`provisional_proposal` requires:
  the per-patient inventory cannot say which hospital a code came from, so an
  organization-scoped proposal writes no provisional rows.
* **Idempotent and resumable.** A person already provisional at the current
  proposal is "already resolved"; a chunk's cursor (the last person id it
  finished) travels in the task arguments, and each chunk stops on a time
  budget, well inside ``CELERY_TASK_TIME_LIMIT``, and hands its cursor on.
* **Coalesced.** One pending sweep per code and one pending job per person (a
  cache key taken when the job is queued, after commit, and dropped when it
  starts), so a batch of saves or a burst of ingest POSTs queues one job. The
  cache is best effort: an outage means a duplicate job, never a failed request.
* **One resolver per person at a time**, with a Postgres *session* advisory lock
  on the person id, held while the resolver runs its own short per-group
  transactions. It is deliberately not one outer transaction: that accumulated
  row locks across code groups and deadlocked with the ETL's keyed bulk write.
  A person someone else is resolving is skipped and retried, never waited on
  for long.
* **The ingest check is cheap**: one query per clinical table for the person's own
  rows, compared in Python against a cached index of the qualifying proposals.
  It never builds that index inside a request; a cold cache queues a warm-up in
  the worker and the check says "not now".
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, _QUARANTINE_VOCABULARIES, _confidence_percent,
    _usable_destination, choose_proposal, normalize_omop_table, resolve_person_source_codes,
)
from omop_core.models import (
    Concept, PatientSourceCode, Person, SourceCodeConceptMapping, SourceCodeResolveRun,
)
from omop_core.services.source_vocabularies import canonical_source_vocabulary

logger = logging.getLogger(__name__)

SWEEP_CHUNK = 200            # persons per task
CHUNK_BUDGET_SECONDS = 600   # a chunk stops here and hands on its cursor
SOFT_TIME_LIMIT_SECONDS = 840  # below CELERY_TASK_TIME_LIMIT (900), so the handler can fire
SAVE_EVERY = 10              # persons between progress saves
_CODE_CHUNK = 1000           # codes per IN list
_INDEX_TTL = 3600            # the qualifying index; refreshed by every sweep chunk start
_PENDING_TTL = 300           # a pending-job key outlives a lost job by this long
_LOCK_NAMESPACE = 2662       # first key of the person advisory lock
LOCK_WAIT_SECONDS = 10       # how long a resolve waits for another resolver of the same person
_LOCK_POLL_SECONDS = 0.2

# A change to any of these on a proposal can change what its rows should be.
TRIGGER_FIELDS = frozenset({
    'target_concept', 'target_concept_id', 'suggestion_confidence', 'status', 'omop_table',
    'source_code', 'source_vocabulary_id', 'organization', 'organization_id',
})


class PersonBusy(Exception):
    """Another resolver holds this person; the job is retried, the sweep leaves them for next time."""


def _enabled() -> bool:
    return bool(settings.PROVISIONAL_DATA_ENABLED)


def _celery() -> bool:
    return bool(getattr(settings, 'CELERY_BROKER_URL', ''))


_warned: set[str] = set()


def _once(key, level, message, *args):
    if key not in _warned:
        _warned.add(key)
        logger.log(level, message, *args)


# ---- the cache, best effort ------------------------------------------------------

def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001 -- a cache outage must never fail a save or an ingest
        logger.warning('Cache read failed for %s.', key, exc_info=True)
        return None


def _cache_set(key, value, ttl):
    try:
        cache.set(key, value, ttl)
    except Exception:  # noqa: BLE001
        logger.warning('Cache write failed for %s.', key, exc_info=True)


def _cache_delete(key):
    try:
        cache.delete(key)
    except Exception:  # noqa: BLE001
        logger.warning('Cache delete failed for %s.', key, exc_info=True)


def _take_pending(key, ttl=_PENDING_TTL) -> bool:
    """True when no job is pending for *key*, and marks one pending (``cache.add`` is atomic).

    A cache that cannot be reached says yes: a duplicate job costs time, a lost
    one costs data. Warns once when the cache is per-process, because then the
    coalescing is not shared between workers.
    """
    backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    if backend.endswith('LocMemCache') and _celery():
        _once('locmem', logging.WARNING,
              'The cache is per-process (no CACHE_URL), so provisional job coalescing is not '
              'shared between workers; duplicate jobs are possible and harmless.')
    try:
        return bool(cache.add(key, 1, ttl))
    except Exception:  # noqa: BLE001
        logger.warning('Cache add failed for %s; queuing without coalescing.', key, exc_info=True)
        return True


def _release_pending(key):
    _cache_delete(key)


# ---- which proposals qualify -----------------------------------------------------

_INDEX_KEY = 'provisional-sweep:index'


def build_qualifying_index() -> dict:
    """The qualifying proposals, indexed for the resolver's own decision.

    ``{'entries': {table: {UPPER_CODE: [(target_id, percent, vocabulary)]}},
    'codes': {table: [source_code]}, 'quarantine': [concept_id]}``. The
    resolver's rule: unscoped, ``proposed``, a destination that is standard, valid
    and in the table's domain, and a confidence at or above the floor. One query
    over the proposals plus one for the quarantine concepts; for a worker or the
    command, never a request.
    """
    floor = settings.PROVISIONAL_CONFIDENCE_FLOOR
    entries: dict[str, dict[str, list]] = {}
    codes: dict[str, set[str]] = {}
    rows = SourceCodeConceptMapping.objects.filter(
        status='proposed', organization__isnull=True,
        target_concept__isnull=False, suggestion_confidence__isnull=False,
    ).select_related('target_concept').only(
        'omop_table', 'source_code', 'source_vocabulary_id', 'suggestion_confidence', 'target_concept')
    for mapping in rows.iterator():
        table = normalize_omop_table(mapping.omop_table)
        if not (table in CLINICAL_TABLES and mapping.source_code
                and _confidence_percent(mapping.suggestion_confidence) >= floor
                and _usable_destination(mapping.target_concept, table)):
            continue
        entries.setdefault(table, {}).setdefault(mapping.source_code.upper(), []).append(
            (mapping.target_concept_id, _confidence_percent(mapping.suggestion_confidence),
             canonical_source_vocabulary(mapping.source_vocabulary_id)))
        codes.setdefault(table, set()).add(mapping.source_code)
    return {'entries': entries, 'codes': {t: sorted(c) for t, c in codes.items()},
            'quarantine': sorted(_quarantine_concept_ids())}


def qualifying_index(*, refresh=False):
    """The cached index, built (and cached) here when asked or when nothing is cached.

    Only for a worker or the command. A request uses :func:`cached_index`.
    """
    if not refresh:
        cached = _cache_get(_INDEX_KEY)
        if cached is not None:
            return cached
    index = build_qualifying_index()
    _cache_set(_INDEX_KEY, index, _INDEX_TTL)
    return index


def cached_index():
    """The cached index or None. Never builds one: a request must not load every proposal."""
    return _cache_get(_INDEX_KEY)


def qualifying_codes(index=None) -> dict[str, set[str]]:
    """``{table: {source_code spellings}}``, each code as stored, upper and lower case.

    The clinical tables match a source code exactly while the resolver matches
    case-insensitively, so selection looks for the spellings an ETL is likely to
    have written and the resolver then settles the rest.
    """
    index = build_qualifying_index() if index is None else index
    out: dict[str, set[str]] = {}
    for table, codes in index['codes'].items():
        spellings = out.setdefault(table, set())
        for code in codes:
            spellings.update({code, code.upper(), code.lower()})
    return out


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
                person_id=OuterRef('person_id'), **{f'{source_col}__iexact': OuterRef('source_value')})
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
    code = mapping.source_code
    return {table: {code, code.upper(), code.lower()}}


# ---- the per-person resolve ------------------------------------------------------

def _try_lock(person_id) -> bool:
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [_LOCK_NAMESPACE, person_id])
        return bool(cursor.fetchone()[0])


def _unlock(person_id):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [_LOCK_NAMESPACE, person_id])
        if not cursor.fetchone()[0]:
            # A session lock belongs to one connection. Releasing nothing means the
            # unlock ran on another: this design assumes a session-pooled or direct
            # connection, never a transaction-pooling pgbouncer.
            logger.warning('The provisional person lock for %s was not held by this connection; '
                           'is the database behind a transaction-pooling proxy?', person_id)


def locked_resolve(person, **kwargs):
    """``resolve_person_source_codes`` with one resolver per person at a time.

    The lock is a Postgres *session* advisory lock, taken before and released in
    ``finally`` after the resolver, which keeps its own short per-group
    transactions: an outer transaction here accumulated row locks across code
    groups and deadlocked with the ETL's keyed bulk write (which locks rows in
    pk order), and ran a derivation inside the lock's transaction, where a
    failure rolled the whole resolve back. Each group's rows are committed before
    the next is touched, and the derivation runs after them. The wait is
    bounded; a person still held raises :class:`PersonBusy`.
    """
    if connection.vendor != 'postgresql':
        return resolve_person_source_codes(person, **kwargs)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while not _try_lock(person.person_id):
        if time.monotonic() >= deadline:
            raise PersonBusy(f'Person {person.person_id} is being resolved by another worker.')
        time.sleep(_LOCK_POLL_SECONDS)
    try:
        return resolve_person_source_codes(person, **kwargs)
    finally:
        _unlock(person.person_id)


def person_scope(person_id, codes) -> tuple[list[str], list[str]]:
    """``(source_values, omop_tables)`` a full sweep resolves for one person.

    The codes the person holds that a sweep can change: a clinical row or an
    inventory code whose spelling is in *codes* (the qualifying proposals), or a
    row that already carries a provisional mark (a proposal that stopped
    qualifying gets its rows taken back). Restricting the resolver to these is
    the whole point: unrestricted, it walks the person's entire inventory, which
    on a loaded patient is thousands of already-resolved codes and minutes of
    queries for a handful of rows that can change. Empty lists mean there is
    nothing to resolve, and the caller must not call the resolver: it reads an
    empty ``source_values`` as no restriction. Bounded by the person's own codes
    whatever the size of *codes*; the qualifying set is looked up in
    ``_CODE_CHUNK`` pieces.
    """
    values: set[str] = set()
    tables: set[str] = set()

    def found(table, spellings):
        spellings = set(spellings)
        if spellings:
            values.update(spellings)
            tables.add(table)

    for table, (model, _concept_col, source_col) in CLINICAL_TABLES.items():
        rows = model.objects.filter(person_id=person_id)
        found(table, rows.filter(confidence__isnull=False).values_list(source_col, flat=True).distinct())
        for chunk in _chunks(codes.get(table, ())):
            found(table, rows.filter(**{f'{source_col}__in': chunk})
                  .values_list(source_col, flat=True).distinct())
            found(table, PatientSourceCode.objects.filter(
                person_id=person_id, omop_table=table, source_value__in=chunk,
            ).values_list('source_value', flat=True).distinct())
    return sorted(v for v in values if v), sorted(tables)


_NOTHING_RESOLVED = {
    'resolved': 0, 'created': 0, 'provisional': 0, 'reverted': 0,
    'skipped': 0, 'already_resolved': 0, 'code_details': [],
}


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


def fail_run(run_id, message):
    """Mark a run FAILED (the task gave up, or could not be queued)."""
    run = SourceCodeResolveRun.objects.filter(pk=run_id).first()
    if run is None or run.state in (SourceCodeResolveRun.COMPLETED, SourceCodeResolveRun.FAILED):
        return
    run.error_detail = (run.error_detail + [{'error': message[:300]}])[:100]
    _finish(run, state=SourceCodeResolveRun.FAILED)


def run_sweep(run_id, *, after=0, mapping_id=None, limit=SWEEP_CHUNK,
              budget_seconds=CHUNK_BUDGET_SECONDS):
    """Resolve one chunk of candidate persons into *run*; the next cursor, or None when done.

    A person that fails is recorded and skipped, not fatal, as in the bulk
    resolve; one another worker holds is left for the next sweep. Progress is
    saved every few persons, the chunk stops on its time budget (or a soft time
    limit) and returns the cursor it reached, so a task killed or paused loses at
    most a few persons. With the switch off nothing is done and the run ends at
    once. Each chunk starts by refreshing the qualifying index, which is how the
    request-side check learns of a new proposal.
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

    # A full sweep always refreshes the index. A mapping's sweep rebuilds it at
    # most once a minute, so a burst of saves does not rebuild it for each; when
    # it skips the rebuild the cached index no longer knows this save's proposal,
    # so it is dropped, and the next ingest check queues a warm-up instead of
    # answering from a stale one.
    index = None
    if mapping is None:
        index = qualifying_index(refresh=True)
    elif _take_pending('provisional-sweep:index-fresh', 60):
        qualifying_index(refresh=True)
    else:
        _cache_delete(_INDEX_KEY)
    codes = mapping_codes(mapping) if mapping else qualifying_codes(index)
    ids = candidate_person_ids(
        after=after, limit=limit, codes=codes, include_provisional_holders=mapping is None,
    )
    started = time.monotonic()
    last_done, errors, processed = after, [], 0
    try:
        for pid in ids:
            if processed and time.monotonic() - started > budget_seconds:
                break
            try:
                person = Person.objects.get(person_id=pid)
                if mapping is None:
                    # Only this person's qualifying and provisional codes, never
                    # the whole inventory (see person_scope).
                    source_values, omop_tables = person_scope(pid, codes)
                    kwargs = {'source_values': source_values, 'omop_tables': omop_tables}
                else:
                    source_values = [mapping.source_code]
                    kwargs = {'source_values': source_values,
                              'omop_tables': [normalize_omop_table(mapping.omop_table)]}
                result = (locked_resolve(person, **kwargs) if source_values
                          else dict(_NOTHING_RESOLVED))
                _bump(run, resolved=result['resolved'], created=result.get('created', 0),
                      provisional=result.get('provisional', 0), reverted=result.get('reverted', 0),
                      skipped=result['skipped'], already_resolved=result['already_resolved'])
            except SoftTimeLimitExceeded:
                raise
            except PersonBusy:
                _bump(run, errors=1)
                errors.append({'person_id': pid, 'error': 'busy: left for the next sweep'})
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
                fail_run(run.id, f'Could not queue the sweep: {exc}')
        transaction.on_commit(enqueue, robust=True)
        return run
    after = 0
    while after is not None:
        after = run_sweep(run.id, after=after, mapping_id=mapping_id, limit=limit)
    return run


# ---- the mapping trigger ---------------------------------------------------------

def _code_key(mapping) -> str:
    return f'provisional-sweep:code:{normalize_omop_table(mapping.omop_table)}:{(mapping.source_code or "").upper()}'


def _state(mapping) -> tuple | None:
    """What a mapping says about its code, for deciding whether a save changed the answer.

    Includes where the mapping is (code, code system, organization), so
    re-pointing a qualifying mapping at another code queues a sweep for it.
    """
    if mapping is None:
        return None
    percent = None if mapping.suggestion_confidence is None else _confidence_percent(mapping.suggestion_confidence)
    qualifies = (
        mapping.status == 'proposed' and mapping.organization_id is None
        and mapping.target_concept_id is not None and percent is not None
        and percent >= settings.PROVISIONAL_CONFIDENCE_FLOOR
        and normalize_omop_table(mapping.omop_table) in CLINICAL_TABLES
    )
    return (qualifies, mapping.target_concept_id, percent, normalize_omop_table(mapping.omop_table),
            mapping.source_code, mapping.source_vocabulary_id, mapping.organization_id)


def mapping_pre_save(sender, instance, update_fields=None, **kwargs):
    """Remember what the mapping said before this save (one query, only when it can matter)."""
    instance._provisional_before = None
    if not _enabled() or instance.pk is None:
        return
    if update_fields is not None and not (set(update_fields) & TRIGGER_FIELDS):
        return
    instance._provisional_before = _state(
        SourceCodeConceptMapping.objects.filter(pk=instance.pk).first())


def mapping_saved(sender, instance, created=False, update_fields=None, **kwargs):
    """post_save receiver: queue a sweep of the code when the proposal's answer changed.

    Queued when the mapping qualifies now or did before and the answer (qualifies,
    destination, confidence, table, where it is) differs, so a new or changed
    proposal reaches patients and a withdrawn one (rejected, destination cleared,
    below the floor) is taken back, while a save that only changed notes queues
    nothing.
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
    # A re-point to another code or table sweeps the new one; rows left under the
    # old one are taken back by the next full sweep (it visits every holder of a
    # provisional row).
    queue_mapping_sweep(instance)


def queue_mapping_sweep(mapping) -> None:
    """Queue a sweep of the persons holding this mapping's code, after commit.

    Only with a broker (never inline in a request), and coalesced: one pending
    sweep per code, so a batch of saves for one code queues one. The pending key
    is taken inside the commit callback, so a save that rolls back leaves none.
    """
    if not _enabled():
        return
    if not _celery():
        _once('no-broker', logging.INFO,
              'Provisional triggers are not queued: there is no CELERY_BROKER_URL. '
              'Run manage.py sweep_provisional_rows to make provisional rows.')
        return
    mapping_id = mapping.pk
    key = _code_key(mapping)

    def queue():
        if _take_pending(key):
            dispatch_mapping_sweep(mapping_id)
    transaction.on_commit(queue, robust=True)


def dispatch_mapping_sweep(mapping_id):
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None:
        return None
    return start_sweep(mapping=mapping)


# ---- after ingest ----------------------------------------------------------------

def person_needs_resolve(person_id) -> bool | None:
    """Whether resolving this person would change anything; None when it cannot say yet.

    One query per clinical table for the person's own distinct
    ``(code, concept, confidence, erroneous)``, one for their inventory, compared
    in Python against the cached qualifying index with the resolver's own choice
    rule (:func:`choose_proposal`), per code system and case-insensitively: yes
    for an unresolved row, or an inventory code with no row, that a proposal
    covers, and for a provisional row that no longer matches its code's proposal.
    Never builds the index (that loads every proposal): with none cached it
    returns None and the caller queues a warm-up.
    """
    index = cached_index()
    if index is None:
        return None
    entries = index['entries']
    quarantine = frozenset(index['quarantine'])
    vocabularies = defaultdict(set)
    for table, value, vocabulary in PatientSourceCode.objects.filter(person_id=person_id).values_list(
            'omop_table', 'source_value', 'source_vocabulary_id'):
        vocabularies[(normalize_omop_table(table), (value or '').upper())].add(vocabulary)
    for table, (model, concept_col, source_col) in CLINICAL_TABLES.items():
        table_entries = entries.get(table, {})
        with_rows = set()
        for code, concept_id, confidence, erroneous in model.objects.filter(person_id=person_id).values_list(
                source_col, concept_col, 'confidence', 'is_erroneous').distinct():
            if not code:
                continue
            upper = code.upper()
            with_rows.add(upper)
            proposal = choose_proposal(table_entries.get(upper, []), vocabularies.get((table, upper)))
            if confidence is None:
                if (not erroneous and (concept_id == NO_MATCHING_CONCEPT_ID or concept_id in quarantine)
                        and proposal is not None):
                    return True
            elif proposal is None or proposal != (concept_id, confidence):
                return True
        for (inventory_table, upper), held in vocabularies.items():
            if (inventory_table == table and upper not in with_rows
                    and choose_proposal(table_entries.get(upper, []), held) is not None):
                return True
    return False


def _person_key(person_id) -> str:
    return f'provisional-sweep:person:{person_id}'


def queue_person_resolve(person_id) -> None:
    """After a person's rows are written: resolve them if a proposal covers any.

    A no-op when the switch is off or there is no broker, and when nothing the
    person holds would change, so the common ingest costs a handful of queries.
    Coalesced per person: twenty POSTs for one patient queue one job. Queued
    after commit; a failed check, or an unreachable cache, never fails an ingest.
    """
    if not _enabled():
        return
    if not _celery():
        _once('no-broker', logging.INFO,
              'Provisional triggers are not queued: there is no CELERY_BROKER_URL. '
              'Run manage.py sweep_provisional_rows to make provisional rows.')
        return
    try:
        needed = person_needs_resolve(person_id)
    except Exception:  # noqa: BLE001
        logger.exception('Provisional check failed for person %s.', person_id)
        return
    if needed is None:
        _queue_index_warmup()
        return
    if not needed:
        return

    def queue():
        if _take_pending(_person_key(person_id)):
            dispatch_person_resolve(person_id)
    transaction.on_commit(queue, robust=True)


def _queue_index_warmup():
    """The index is not cached: have a worker build it, once, instead of the request."""
    def queue():
        if _take_pending('provisional-sweep:warm'):
            try:
                from omop_core.tasks import provisional_warm_index_task
                provisional_warm_index_task.delay()
            except Exception:  # noqa: BLE001
                logger.warning('Could not queue the provisional index warm-up.', exc_info=True)
                _release_pending('provisional-sweep:warm')
    transaction.on_commit(queue, robust=True)


def warm_index():
    """Worker body: build and cache the qualifying index."""
    _release_pending('provisional-sweep:warm')
    if _enabled():
        qualifying_index(refresh=True)


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
