"""How a vote confirmation email gets sent (#2678).

Sending inside the request had two faults. The mail provider's call can hang
for as long as gunicorn lets a request live, and a worker killed there dies
after the submission and its email count were committed. And the time the
answer took told a submission that sent mail from one that was held back.

So the request only records the submission and hands its id here. The seam
mirrors ``derivation_jobs.py`` and ``suggest_jobs.py``: Celery when a broker
is configured, inline otherwise, and no separate setting, because two
settings that can disagree leave every job queued with nothing consuming it.
Inline is what a machine with no Redis gets; there the request still waits
for the mail backend, bounded by ``MAPPING_VOTE_EMAIL_TIMEOUT``.

The job's only argument is the submission's id. The confirmation token is
made by whoever sends the email (``mapping_votes.send_confirmation``), so no
token is ever in a task argument, a broker message, a Celery event or an
error report that quotes a task's arguments.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from django.conf import settings
from django.db import transaction


class ConfirmationDispatcher(Protocol):
    def dispatch(self, submission_id: int) -> None:
        """Arrange for the submission's confirmation email to be sent."""


class CeleryDispatcher:
    """Queues the send on a worker."""

    def dispatch(self, submission_id: int) -> None:
        from omop_core.tasks import send_vote_confirmation_task

        # On commit, so a worker never looks for a row its caller has not
        # committed yet. Outside a transaction this runs at once.
        transaction.on_commit(
            lambda: send_vote_confirmation_task.apply_async(args=[submission_id])
        )


class InlineDispatcher:
    """Sends in the calling thread. What a machine with no broker gets."""

    def dispatch(self, submission_id: int) -> None:
        from omop_core.services import mapping_votes

        mapping_votes.send_confirmation(submission_id)


class FakeDispatcher:
    """Records what it was asked to send, sends nothing. For tests."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def dispatch(self, submission_id: int) -> None:
        self.calls.append(submission_id)


_celery = CeleryDispatcher()
_inline = InlineDispatcher()
_override: ConfirmationDispatcher | None = None


def dispatcher() -> ConfirmationDispatcher:
    if _override is not None:
        return _override
    return _celery if settings.CELERY_BROKER_URL else _inline


@contextmanager
def use_dispatcher(replacement: ConfirmationDispatcher) -> Iterator[ConfirmationDispatcher]:
    """Swap the dispatcher for the duration of a test."""
    global _override
    previous = _override
    _override = replacement
    try:
        yield replacement
    finally:
        _override = previous
