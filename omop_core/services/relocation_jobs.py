"""Move a code's stored rows to the table its mapping now names, off the request.

A save from a mapping tab re-files a code every patient may carry, and a
common code has thousands of rows, so the move runs after commit: on Celery
when ``CELERY_BROKER_URL`` is set, inline otherwise -- the rule
``repoint_jobs`` follows. The patient page's save is scoped to one patient
and moves that patient's rows in the request, so the tab reflects it at once.

Unlike a re-point there is no queue column: the mapping no longer remembers
the table it left, so the task carries it. A task that is lost leaves the
rows where they were, which is the state before the fix; the next save of
the mapping from either table moves them.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from omop_core.mapping.table_relocation import relocate_clinical_rows
from omop_core.models import SourceCodeConceptMapping

logger = logging.getLogger(__name__)


def queue_relocation(mapping, from_table, to_table):
    """Dispatch the move after the save's transaction commits."""
    mapping_id = mapping.pk
    _after_commit(lambda: dispatch_relocation(mapping_id, from_table, to_table))
    return {'queued': True, 'from_table': from_table, 'to_table': to_table}


def _after_commit(func):
    # robust: a failed dispatch must not turn the committed save into a 500.
    transaction.on_commit(func, robust=True)


def dispatch_relocation(mapping_id, from_table, to_table):
    if getattr(settings, 'CELERY_BROKER_URL', ''):
        try:
            from omop_core.tasks import relocate_mapping_rows_task
            relocate_mapping_rows_task.delay(mapping_id, from_table, to_table)
            return
        except Exception:  # noqa: BLE001 - a queue outage must not strand the rows
            logger.warning('Could not queue the relocation of mapping %s; running inline.', mapping_id)
    run_relocation(mapping_id, from_table, to_table)


def run_relocation(mapping_id, from_table, to_table):
    """Move the rows, unless the mapping has since left *to_table* again."""
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None or mapping.omop_table != to_table:
        logger.info('Relocation of mapping %s skipped: it no longer names %s.', mapping_id, to_table)
        return {'rows_relocated': 0, 'persons_marked_stale': 0, 'source_codes_refiled': 0}
    outcome = relocate_clinical_rows(mapping=mapping, from_table=from_table, to_table=to_table)
    outcome.pop('person_ids', None)
    return outcome
