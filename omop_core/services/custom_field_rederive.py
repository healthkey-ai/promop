"""Fill a created field from the facts patients already have at its concept (#2941).

A created field (``CustomPatientField``) reads imported OMOP facts at its mapping's
concept (``patient_record_service.custom_field_answers``), but only when a record is
derived. Creating the field, restoring it, or moving its approved mapping to another
concept does not derive anything, so a patient who already had a PSA result at the
field's LOINC code showed the field empty until something else (Generate OMOP, a
backfill, another edit) re-derived their record. This module queues that derivation.

* **Who.** The distinct people with a non-erroneous fact at the mapping's concept in the
  mapping's table (:func:`person_ids`, keyset-paged by person id).
* **When.** A saved mapping of an active, editable created field whose status, concept,
  table or source key changed to an approved, concept-bearing state
  (:func:`mapping_saved`), and a restore (:func:`queue`; ``update()`` fires no signal).
  Built-in field mappings are not touched.
* **How.** Off the request, on Celery, in chunks of :data:`CHUNK` people: one chunk
  derives its people and queues the next with its cursor, and stops early at
  :data:`CHUNK_BUDGET_SECONDS` to hand the cursor on. A lab concept can have thousands
  of people and one derivation costs seconds, so the work is never done in a request.
  One pending job per mapping is coalesced through the cache, so a burst of saves
  queues one chain.
* **No broker.** Nothing is queued and the reason is logged once; the records are
  re-derived by Generate OMOP, by ``manage.py backfill_patient_records``, or by
  ``manage.py refresh_custom_field_records <field_name>``, which runs the same chunks
  in the foreground.

A derivation is idempotent, so a chunk redelivered after a worker died, or two chains
for one mapping, cost time and nothing else.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)

CHUNK = 25                    # people per task
CHUNK_BUDGET_SECONDS = 600    # a chunk stops here and hands on its cursor
SOFT_TIME_LIMIT_SECONDS = 840  # below CELERY_TASK_TIME_LIMIT (900)
_PENDING_TTL = 300            # a pending key outlives a lost job by this long

_warned: set[str] = set()


def _celery() -> bool:
    return bool(getattr(settings, 'CELERY_BROKER_URL', ''))


def _pending_key(mapping_id) -> str:
    return f'custom-field-rederive:{mapping_id}'


def _take_pending(mapping_id) -> bool:
    """True when no chain is pending for the mapping (``cache.add`` is atomic).

    A cache that cannot be reached says yes: a duplicate chain costs time, a lost one
    leaves a field empty.
    """
    try:
        return bool(cache.add(_pending_key(mapping_id), 1, _PENDING_TTL))
    except Exception:  # noqa: BLE001
        logger.warning('Cache add failed for custom field %s; queuing without coalescing.',
                       mapping_id, exc_info=True)
        return True


def _release_pending(mapping_id) -> None:
    try:
        cache.delete(_pending_key(mapping_id))
    except Exception:  # noqa: BLE001
        logger.warning('Cache delete failed for custom field %s.', mapping_id, exc_info=True)


# ---- which mappings qualify --------------------------------------------------

def mapping_state(mapping) -> tuple:
    """What decides which facts answer a created field: any change here changes who to derive."""
    return (mapping.status, mapping.concept_id, (mapping.omop_table or '').strip().lower(),
            mapping.source_value or '')


def _qualifies(state: tuple) -> bool:
    status, concept_id, table, _source = state
    return status == 'approved' and bool(concept_id) and bool(table)


def mapping_post_init(sender, instance, **kwargs) -> None:
    """Remember what the mapping said when it was loaded, so only a real change queues work.

    Taken from the loaded instance, with no query: mapping saves are made in bulk (e.g.
    ``resolve_field_concept_mappings``) and a read per save would show. A row loaded with
    those columns deferred, or a new one, has no snapshot and counts as changed.
    """
    fields = ('status', 'concept_id', 'omop_table', 'source_value')
    if instance.pk and all(name in instance.__dict__ for name in fields):
        instance._rederive_before = mapping_state(instance)
    else:
        instance._rederive_before = None


def mapping_saved(sender, instance, **kwargs) -> None:
    """Queue the derivation when a created field's mapping became (or changed while) approved."""
    now = mapping_state(instance)
    before = getattr(instance, '_rederive_before', None)
    instance._rederive_before = now
    if not _qualifies(now) or before == now:
        return
    queue(instance.pk)


def _field_for(mapping_id):
    """The active, editable created field this mapping belongs to, or ``None``."""
    from omop_core.models import CustomPatientField

    return (CustomPatientField.objects.active()
            .filter(mapping_id=mapping_id, mode='editable', mapping__status='approved')
            .select_related('mapping').first())


# ---- queueing -----------------------------------------------------------------

def queue(mapping_id) -> None:
    """After commit, queue the derivation of everyone with a fact at this created field's concept.

    Never runs a derivation in the caller's request. A mapping that is not a created
    field's (the common case: a built-in field's curation) queues nothing.
    """
    def run():
        try:
            if _field_for(mapping_id) is None:
                return
            if not _celery():
                if 'no-broker' not in _warned:
                    _warned.add('no-broker')
                    logger.info(
                        'Created fields are not filled from existing facts: there is no '
                        'CELERY_BROKER_URL. Run manage.py refresh_custom_field_records <name>, '
                        'or Generate OMOP on a patient.')
                return
            if _take_pending(mapping_id):
                dispatch(mapping_id)
        except Exception:  # noqa: BLE001 -- never fail the save that triggered this
            logger.exception('Could not queue the derivation for created field mapping %s.', mapping_id)

    transaction.on_commit(run, robust=True)


def dispatch(mapping_id, after: int = 0) -> None:
    try:
        from omop_core.tasks import refresh_custom_field_records_task
        refresh_custom_field_records_task.delay(mapping_id, after)
    except Exception:  # noqa: BLE001 -- a later save, or the command, picks it up
        logger.exception('Could not queue the derivation for created field mapping %s.', mapping_id)
        _release_pending(mapping_id)


# ---- the work -----------------------------------------------------------------

def person_ids(mapping, *, after: int = 0, limit: int = CHUNK) -> list[int]:
    """Up to ``limit`` distinct people after ``after`` with a live fact at the mapping's concept."""
    from django.apps import apps

    from omop_core.services import custom_field_values
    from omop_core.services.custom_field_removal import _FACT_TABLES

    target = custom_field_values.target_for(mapping.omop_table)
    if target is None or not mapping.concept_id:
        return []
    model_name, concept_column, _source = _FACT_TABLES[target]
    model = apps.get_model('omop_core', model_name)
    return list(
        model.objects.filter(**{concept_column: mapping.concept_id}, is_erroneous=False,
                             person_id__gt=after)
        .order_by('person_id').values_list('person_id', flat=True).distinct()[:limit])


def run_chunk(mapping_id, after: int = 0, *, limit: int = CHUNK,
              budget: float = CHUNK_BUDGET_SECONDS) -> dict:
    """Derive one chunk of people. ``next_after`` is the cursor of the next chunk, ``None`` when done.

    The first chunk releases the coalescing mark, so a save made while the chain runs
    queues another chain: a mapping changed meanwhile must be read again.
    """
    from omop_core.models import Person
    from omop_core.services.patient_record_service import refresh_patient_record

    if after == 0:
        _release_pending(mapping_id)
    result = {'mapping_id': mapping_id, 'after': after, 'refreshed': 0, 'errors': 0, 'next_after': None}
    field = _field_for(mapping_id)
    if field is None:
        return result
    ids = person_ids(field.mapping, after=after, limit=limit)
    started = time.monotonic()
    last = after
    for person_id in ids:
        if time.monotonic() - started > budget:
            result['next_after'] = last
            return result
        last = person_id
        person = Person.objects.filter(person_id=person_id).first()
        if person is None:
            continue
        try:
            refresh_patient_record(person)
            result['refreshed'] += 1
        except Exception:  # noqa: BLE001 -- one record must not stop the rest
            result['errors'] += 1
            logger.exception('Deriving person %s for created field mapping %s failed.',
                             person_id, mapping_id)
    if len(ids) >= limit:
        result['next_after'] = last
    return result
