"""Write provisional rows for patients whose codes have a proposal (#2662).

The ETL does not propose concepts: Code to Concept and Concept to Code do, and
store the ranker's confidence on the mapping (``suggestion_confidence``). Rows an
ETL loads for such a code sit at concept 0 until something resolves them, and
the per-person resolver (:func:`resolve_person_source_codes`) already knows how
to make them provisional at the proposal's concept. This module decides *which
persons* to run it for, and when:

* a **sweep** over every person who holds an unresolved row, a provisional row,
  or an unresolved inventory code under a proposal that qualifies (unscoped,
  ``proposed``, a destination, a confidence at or above the floor), in
  person-id chunks;
* a **trigger** when a mapping is saved as a proposal (any writer: Suggest,
  suggest-one accept, Concept to Code, the dialog and Calc), limited to that
  mapping's code;
* a **person job** after ingest (the bulk OMOP write, the FHIR sync) so a new
  patient does not wait for a sweep.

Everything is off with ``PROVISIONAL_DATA_ENABLED``: nothing is queried,
queued or written. A job is idempotent (a person already provisional at the
current proposal is "already resolved"), so a duplicate or retried job costs
time, not correctness, and a chunk is resumed by its cursor (the last person id
it finished), carried in the task arguments rather than stored: the selection
shrinks as persons are resolved, and the cursor covers the ones that stay
candidates (a provisional row is rewritten, never "done").

Like the other mapping jobs it runs on Celery when ``CELERY_BROKER_URL`` is set
and inline otherwise; inline, a mapping trigger does one bounded chunk and
leaves the rest to ``manage.py sweep_provisional_rows``.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID, _QUARANTINE_VOCABULARIES,
    normalize_omop_table, resolve_person_source_codes,
)
from omop_core.models import (
    Concept, PatientSourceCode, Person, SourceCodeConceptMapping, SourceCodeResolveRun,
)

logger = logging.getLogger(__name__)

SWEEP_CHUNK = 200            # persons per task; a task must finish inside CELERY_TASK_TIME_LIMIT
INLINE_CHUNK = 50            # persons a trigger resolves in the request when there is no broker
_CODE_CHUNK = 1000           # codes per IN list

# A change to any of these on a proposal can change what its rows should be.
TRIGGER_FIELDS = frozenset({
    'target_concept', 'target_concept_id', 'suggestion_confidence', 'status', 'omop_table',
})


def _enabled() -> bool:
    return bool(settings.PROVISIONAL_DATA_ENABLED)


def qualifying_codes() -> dict[str, set[str]]:
    """``{table: {source_code}}`` for proposals a provisional row may be written from.

    Unscoped, ``proposed``, with a destination and a confidence at or above the
    floor, in a clinical table. The resolver re-checks the destination and the
    code systems per person (``provisional_proposal``); this only decides whom to
    look at, so it is allowed to be a little generous.
    """
    floor = settings.PROVISIONAL_CONFIDENCE_FLOOR / 100 - 0.005
    codes: dict[str, set[str]] = {}
    rows = SourceCodeConceptMapping.objects.filter(
        status='proposed', organization__isnull=True,
        target_concept__isnull=False, suggestion_confidence__gte=floor,
    ).values_list('omop_table', 'source_code').distinct()
    for table, code in rows:
        table = normalize_omop_table(table)
        if table in CLINICAL_TABLES and code:
            codes.setdefault(table, set()).add(code)
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


def candidate_person_ids(*, after=0, limit=SWEEP_CHUNK, codes=None, person_id=None,
                         include_provisional_holders=True) -> list[int]:
    """The next *limit* person ids above *after* that a resolve could change.

    A person is a candidate when, for a code in *codes* (default: the qualifying
    proposals), they hold an unresolved row (concept 0 or a quarantine
    placeholder), a provisional row (a proposal may have moved or fallen below
    the floor), or an inventory code with no clinical row yet. With
    *include_provisional_holders* every person holding any provisional row is a
    candidate too, which is how a proposal that stopped qualifying, with no hook
    to say so, gets its rows taken back. One bounded query per table and code
    chunk, never one per person.
    """
    codes = qualifying_codes() if codes is None else codes
    quarantine = _quarantine_concept_ids()
    found: set[int] = set()

    def collect(queryset):
        queryset = queryset.filter(person_id__gt=after)
        if person_id is not None:
            queryset = queryset.filter(person_id=person_id)
        found.update(queryset.order_by('person_id').values_list('person_id', flat=True)
                     .distinct()[:limit])

    for table, code_set in codes.items():
        model, concept_col, source_col = CLINICAL_TABLES[table]
        unresolved = Q(**{concept_col: NO_MATCHING_CONCEPT_ID})
        if quarantine:
            unresolved |= Q(**{f'{concept_col}__in': quarantine})
        for chunk in _chunks(code_set):
            collect(model.objects.filter(**{f'{source_col}__in': chunk})
                    .filter(unresolved | Q(confidence__isnull=False)))
            has_row = model.objects.filter(
                person_id=OuterRef('person_id'), **{source_col: OuterRef('source_value')})
            collect(PatientSourceCode.objects.filter(omop_table=table, source_value__in=chunk)
                    .annotate(has_row=Exists(has_row)).filter(has_row=False))
    if include_provisional_holders:
        for model, _concept_col, _source_col in CLINICAL_TABLES.values():
            collect(model.objects.filter(confidence__isnull=False))
    return sorted(found)[:limit]


def mapping_codes(mapping) -> dict[str, set[str]]:
    """The one code a mapping trigger looks at, whether or not it qualifies.

    A proposal that no longer qualifies still needs its rows looked at, so they
    can be taken back.
    """
    table = normalize_omop_table(mapping.omop_table)
    if table not in CLINICAL_TABLES or not mapping.source_code:
        return {}
    return {table: {mapping.source_code}}


def _bump(run, **counts):
    for name, amount in counts.items():
        setattr(run, name, getattr(run, name) + amount)


def run_sweep(run_id, *, after=0, mapping_id=None, limit=SWEEP_CHUNK):
    """Resolve one chunk of candidate persons into *run*; the next cursor, or None when done.

    A person that fails is recorded and skipped, not fatal, as in the bulk
    resolve. With the switch off nothing is done and the run ends at once.
    """
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
    run.state = SourceCodeResolveRun.RUNNING
    run.save(update_fields=['state'])

    ids = candidate_person_ids(
        after=after, limit=limit,
        codes=mapping_codes(mapping) if mapping else None,
        include_provisional_holders=mapping is None,
    )
    errors = []
    for pid in ids:
        try:
            person = Person.objects.get(person_id=pid)
            if mapping is None:
                result = resolve_person_source_codes(person)
            else:
                result = resolve_person_source_codes(
                    person, source_values=[mapping.source_code],
                    omop_tables=[normalize_omop_table(mapping.omop_table)])
            _bump(run, resolved=result['resolved'], created=result.get('created', 0),
                  provisional=result.get('provisional', 0), reverted=result.get('reverted', 0),
                  skipped=result['skipped'], already_resolved=result['already_resolved'])
        except Exception as exc:  # noqa: BLE001 -- one person must not stop the sweep
            logger.exception('Provisional sweep failed for person %s in run %s', pid, run_id)
            _bump(run, errors=1)
            errors.append({'person_id': pid, 'error': str(exc)[:200]})
        _bump(run, done=1)
    run.total = max(run.total, run.done)
    if errors:
        run.error_detail = (run.error_detail + errors)[:100]
    run.save(update_fields=['done', 'total', 'resolved', 'created', 'provisional', 'reverted',
                            'skipped', 'already_resolved', 'errors', 'error_detail'])
    if len(ids) < limit:
        _finish(run)
        return None
    return ids[-1]


def _finish(run, notes=None):
    run.state = SourceCodeResolveRun.COMPLETED
    run.finished_at = timezone.now()
    if notes:
        run.code_details = notes
    run.save(update_fields=['state', 'finished_at', 'code_details'])


def start_run_record(user=None, total=0):
    return SourceCodeResolveRun.objects.create(total=total, created_by=user)


def _celery() -> bool:
    return bool(getattr(settings, 'CELERY_BROKER_URL', ''))


def start_sweep(*, mapping=None, user=None, max_chunks=None, limit=SWEEP_CHUNK):
    """Start a sweep (everything, or one mapping's code); the run, or None when off.

    On Celery the chunks chain from a task, each queuing the next with its
    cursor. Inline the chunks run here, up to *max_chunks* (all when None).
    """
    if not _enabled():
        return None
    run = start_run_record(user)
    mapping_id = mapping.pk if mapping is not None else None
    if _celery():
        try:
            from omop_core.tasks import provisional_sweep_task
            transaction.on_commit(
                lambda: provisional_sweep_task.delay(str(run.id), 0, mapping_id, limit), robust=True)
            return run
        except Exception:  # noqa: BLE001 -- a queue outage must not strand the work
            logger.warning('Could not queue the provisional sweep; running inline.')
    after, chunks = 0, 0
    while after is not None:
        after = run_sweep(run.id, after=after, mapping_id=mapping_id, limit=limit)
        chunks += 1
        if after is not None and max_chunks is not None and chunks >= max_chunks:
            logger.info('Provisional sweep %s paused after %s chunk(s) at person %s.',
                        run.id, chunks, after)
            break
    return run


def queue_mapping_sweep(mapping) -> None:
    """After a mapping is saved as a proposal: look at the persons holding its code.

    Queued after commit; failures never reach the saving request.
    """
    if not _enabled():
        return
    mapping_id = mapping.pk
    transaction.on_commit(lambda: dispatch_mapping_sweep(mapping_id), robust=True)


def dispatch_mapping_sweep(mapping_id):
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None or mapping.status != 'proposed' or mapping.organization_id is not None:
        return None
    # Inline: one bounded chunk in the request, the rest for the sweep command.
    return start_sweep(mapping=mapping, max_chunks=1, limit=INLINE_CHUNK if not _celery() else SWEEP_CHUNK)


def mapping_saved(sender, instance, created=False, update_fields=None, **kwargs):
    """post_save receiver: a proposal whose answer may have changed queues its sweep."""
    if not _enabled():
        return
    if instance.status != 'proposed' or instance.organization_id is not None:
        return
    if instance.target_concept_id is None or normalize_omop_table(instance.omop_table) not in CLINICAL_TABLES:
        return
    if update_fields is not None and not (set(update_fields) & TRIGGER_FIELDS):
        return
    queue_mapping_sweep(instance)


# ---- after ingest ------------------------------------------------------------

def person_needs_resolve(person_id) -> bool:
    """Whether this person holds an unresolved row or code under a qualifying proposal."""
    return bool(candidate_person_ids(person_id=person_id, limit=1, include_provisional_holders=False))


def queue_person_resolve(person_id) -> None:
    """After a person's rows are written: resolve them if a proposal covers any.

    A no-op when the switch is off or nothing the person holds is covered, so
    the common ingest costs one cheap check. Queued after commit.
    """
    if not _enabled():
        return
    try:
        if not person_needs_resolve(person_id):
            return
    except Exception:  # noqa: BLE001 -- the check must never fail an ingest
        logger.exception('Provisional check failed for person %s.', person_id)
        return
    transaction.on_commit(lambda: dispatch_person_resolve(person_id), robust=True)


def dispatch_person_resolve(person_id):
    if _celery():
        try:
            from omop_core.tasks import provisional_resolve_person_task
            provisional_resolve_person_task.delay(person_id)
            return
        except Exception:  # noqa: BLE001
            logger.warning('Could not queue the provisional resolve of person %s; running inline.', person_id)
    run_person_resolve(person_id)


def run_person_resolve(person_id):
    if not _enabled():
        return None
    person = Person.objects.filter(person_id=person_id).first()
    if person is None:
        return None
    return resolve_person_source_codes(person)
