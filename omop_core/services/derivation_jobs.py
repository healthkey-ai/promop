"""How a PatientRecord derivation gets run.

Derivation cost grows with the rows a person already holds, so on a bulk
loaded patient the refresh endpoint spent 15-25s inside the request and hit
its statement timeout as a 500. It now hands the work to a dispatcher and the
caller polls for the outcome.

The seam sits here rather than in the view so a test can swap the dispatcher
out without a broker and without reaching into Celery.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings
from django.db import connection, transaction

from omop_core.models import Person

# Celery's own vocabulary, so a client written against the queue reads the
# inline path with no special cases.
PENDING = 'PENDING'
STARTED = 'STARTED'
SUCCESS = 'SUCCESS'
FAILURE = 'FAILURE'


@dataclass(frozen=True)
class DerivationStatus:
    task_id: str
    state: str
    error: str | None = None


class DerivationDispatcher(Protocol):
    def dispatch(self, person: Person) -> str:
        """Arrange for the derivation to run, return the task id."""

    def status(self, task_id: str) -> DerivationStatus:
        """Where that derivation got to."""


class CeleryDispatcher:
    """Queues the derivation on a worker."""

    def dispatch(self, person: Person) -> str:
        from omop_core.tasks import refresh_patient_record_task

        # The id is minted here rather than taken from the enqueue call, because
        # the caller needs it in the 202 and the enqueue is deferred to commit.
        task_id = str(uuid.uuid4())
        person_id = person.person_id
        # Deferred on purpose: a worker that starts inside the caller's open
        # transaction reads the pre-write state and derives from it.
        transaction.on_commit(
            lambda: refresh_patient_record_task.apply_async(
                args=[person_id], task_id=task_id,
            )
        )
        return task_id

    def status(self, task_id: str) -> DerivationStatus:
        from ctomop.celery import app as celery_app

        result = celery_app.AsyncResult(task_id)
        state = result.state
        return DerivationStatus(
            task_id=task_id,
            state=state,
            error=str(result.result) if state == FAILURE else None,
        )


class InlineDispatcher:
    """Derives in the calling thread. What a machine with no broker gets.

    A failure propagates instead of being recorded, so the caller sees it on
    the refresh response rather than having to poll for it.
    """

    # An id is only ever handed out after the derivation returned, so the
    # prefix is the completion record and nothing has to be remembered. A
    # registry would be process-local, and under several gunicorn workers the
    # poll lands on a process that never saw the POST and answers PENDING for
    # ever.
    _PREFIX = 'inline-'

    # The derivation holds the request and a database connection for as long
    # as it runs, so it needs the bound the queued path gets from
    # CELERY_TASK_TIME_LIMIT. SET LOCAL reverts on commit.
    _STATEMENT_TIMEOUT = '25s'

    def dispatch(self, person: Person) -> str:
        from omop_core.services.patient_record_service import refresh_patient_record

        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cur:
                    cur.execute(f"SET LOCAL statement_timeout = '{self._STATEMENT_TIMEOUT}'")
            refresh_patient_record(person)
        return f'{self._PREFIX}{uuid.uuid4()}'

    def status(self, task_id: str) -> DerivationStatus:
        # An id from some other deployment mode reads PENDING, which is what
        # Celery answers for an id it has never seen.
        return DerivationStatus(
            task_id=task_id,
            state=SUCCESS if task_id.startswith(self._PREFIX) else PENDING,
        )


class FakeDispatcher:
    """Records what it was asked to derive, derives nothing. For tests."""

    def __init__(self, state: str = SUCCESS, error: str | None = None) -> None:
        self.calls: list[int] = []
        self.state = state
        self.error = error

    def dispatch(self, person: Person) -> str:
        self.calls.append(person.person_id)
        return f'fake-task-{len(self.calls)}'

    def status(self, task_id: str) -> DerivationStatus:
        return DerivationStatus(task_id=task_id, state=self.state, error=self.error)


_celery = CeleryDispatcher()
_inline = InlineDispatcher()
_override: DerivationDispatcher | None = None


def get_dispatcher() -> DerivationDispatcher:
    """Celery when a broker is configured, inline otherwise.

    Read off the broker URL rather than given its own setting: two settings
    that can disagree buy nothing and one of the combinations leaves every job
    queued with nothing consuming it.
    """
    if _override is not None:
        return _override
    return _celery if getattr(settings, 'CELERY_BROKER_URL', '') else _inline


@contextmanager
def use_dispatcher(dispatcher: DerivationDispatcher) -> Iterator[DerivationDispatcher]:
    """Swap the dispatcher for the duration of a test."""
    global _override
    previous = _override
    _override = dispatcher
    try:
        yield dispatcher
    finally:
        _override = previous
