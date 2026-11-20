"""Auto-approving a Suggest result that the ranker is sure of (#1532).

A Suggest run writes every proposal as ``proposed`` and a curator approves it. A
curator who trusts the ranker at a high confidence can have the run approve it as
it writes it. The rules that shape this:

* **The threshold is a whole percent from 90 to 100, default 100, or off.** Below
  90 a ranker's answer is not one a run should sign off on, so the floor is not a
  preference. A score is compared as it is, not rounded: 100 means a score of
  exactly 1.0, and 0.895 does not approve at 90. "Off" is stricter than any
  threshold, so it breaks no minimum.
* **It is the curator's choice and it sticks.** The last value an approver set (a
  number or off) is kept on their identity and is what their next run uses and what
  the Suggest controls show; a run records the state it used on
  ``SuggestRun.selection``. Someone who never chose gets the default, 100.
* **Only an approver gets it.** Approval is staff and org admin only
  (``_can_approve_mappings``), checked again for every row the run approves. An
  analyst may run Suggest and never causes an approval; the setting is ignored for
  them and the page does not offer it. A service credential is a machine and never
  auto-approves.
* **Only where a worker does the re-point.** An approval queues a re-point of every
  stored row of the code; under the inline dispatcher (no ``CELERY_BROKER_URL``) that
  would run inside the request, so auto-approval is disabled there and the run says so.

The approval itself is the one a curator's is: :func:`approve` calls
``_upsert_source_code_mapping``, so the status, the reviewer, the re-point of stored
clinical rows, the promotion of provisional rows and the relocation rules all run once,
in one place. Only what the machine decided is recorded differently: ``origin_system``
says it was auto-approved and at what threshold, no ``decision_*`` column is stamped
(those record a curator's decision), and the suggestion's outcome is left blank (it is
the *curator's* first disposition, and nobody has reviewed this one).
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)

MIN_THRESHOLD = 90
MAX_THRESHOLD = 100
DEFAULT_THRESHOLD = 100
#: What a request sends to turn auto-approval off.
OFF = 'off'

THRESHOLD_MESSAGE = f'Must be a whole number from {MIN_THRESHOLD} to {MAX_THRESHOLD}, or "{OFF}".'

#: Why a run did not auto-approve although it could have.
REASON_NOT_APPROVER = 'not_approver'
REASON_NO_WORKER = 'no_worker'
REASON_DRY_RUN = 'dry_run'
REASON_TEXT = {
    REASON_NOT_APPROVER: 'Only staff and organization admins can auto-approve.',
    REASON_NO_WORKER: (
        'Auto-approval is off because this deployment has no worker queue: an approval '
        'would re-point every stored row inside the request.'
    ),
    REASON_DRY_RUN: 'A dry run changes nothing.',
}


def parse_threshold(raw):
    """*raw* as a whole percent 90-100, ``OFF``, or ``None`` when nothing was sent.

    ``None`` and an empty string mean "use mine"; anything else that is not a whole
    number in range, or the word off, raises ``ValueError`` (a 400, never a clamp).
    """
    if raw is None or raw == '':
        return None
    if isinstance(raw, bool):
        raise ValueError(THRESHOLD_MESSAGE)
    if isinstance(raw, str) and raw.strip().lower() == OFF:
        return OFF
    if isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            raise ValueError(THRESHOLD_MESSAGE)
        raw = int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text.isdigit():
            raise ValueError(THRESHOLD_MESSAGE)
        raw = int(text)
    if not isinstance(raw, int) or not MIN_THRESHOLD <= raw <= MAX_THRESHOLD:
        raise ValueError(THRESHOLD_MESSAGE)
    return raw


def eligible(user) -> bool:
    """Whether Suggest runs started by *user* may approve what they write."""
    from omop_core.services import mapping_approval
    from patient_portal.api.views import _can_approve_mappings

    return bool(mapping_approval.is_person(user) and _can_approve_mappings(user))


def available() -> bool:
    """Whether a worker would do the re-point an approval queues (not the inline dispatcher)."""
    from omop_core.services.suggest_jobs import InlineDispatcher, get_dispatcher

    return not isinstance(get_dispatcher(), InlineDispatcher)


def saved_threshold(user) -> int:
    """The threshold *user* last set, or the default."""
    value = getattr(user, 'suggest_auto_approve_threshold', None)
    return value if value is not None else DEFAULT_THRESHOLD


def is_off(user) -> bool:
    return bool(getattr(user, 'suggest_auto_approve_off', False))


def reference_for(user) -> dict:
    """What the Suggest controls need: whether to offer them, and their prefilled state."""
    can_approve = eligible(user)
    return {
        'allowed': can_approve,
        'available': available(),
        'unavailable_reason': REASON_TEXT[REASON_NO_WORKER] if not available() else '',
        'threshold': saved_threshold(user),
        'off': is_off(user),
        'min': MIN_THRESHOLD,
        'max': MAX_THRESHOLD,
    }


def remember(user, value) -> None:
    """Keep *value* (a threshold, or ``OFF``) as *user*'s own, so the next run starts from it."""
    if value == OFF:
        user.suggest_auto_approve_off = True
        fields = ['suggest_auto_approve_off']
    else:
        user.suggest_auto_approve_off = False
        user.suggest_auto_approve_threshold = value
        fields = ['suggest_auto_approve_off', 'suggest_auto_approve_threshold']
    user.save(update_fields=fields)


def plan(user, raw, *, dry_run=False) -> dict:
    """What a run started by *user* does about auto-approval.

    Returns ``{'state': 'on'|'off'|'disabled', 'threshold': int|None, 'reason': str}``.
    *raw* is what the request sent (already validated by :func:`parse_threshold`, so a bad
    value fails on any account). For an account that may not approve, the setting is
    ignored and nothing is stored. For an approver, a value that was sent is remembered;
    none sent uses what they last set (the default, 100, if they never did).
    """
    sent = parse_threshold(raw)
    if not eligible(user):
        return {'state': 'disabled', 'threshold': None, 'reason': REASON_NOT_APPROVER}
    if sent is not None:
        remember(user, sent)
    if dry_run:
        return {'state': 'disabled', 'threshold': None, 'reason': REASON_DRY_RUN}
    if (is_off(user) if sent is None else sent == OFF):
        return {'state': 'off', 'threshold': None, 'reason': ''}
    if not available():
        return {'state': 'disabled', 'threshold': None, 'reason': REASON_NO_WORKER}
    return {'state': 'on', 'threshold': sent if isinstance(sent, int) else saved_threshold(user), 'reason': ''}


def approved_origin(threshold) -> str:
    """The provenance an auto-approved row carries (begins ``suggest``, so it reads as the run's)."""
    from omop_core.mapping.suggestions import SUGGESTION_PROVENANCE

    return f'{SUGGESTION_PROVENANCE} {AUTO_APPROVED_MARK} {threshold}%'[:50]


#: What ``approved_origin`` carries and the list filter looks for.
AUTO_APPROVED_MARK = 'auto-approved'


def _score_percent(score):
    """*score* (0-1) as an exact ``Decimal`` percent, or ``None`` when it is not one.

    Read from the float's shortest repr, not its binary value, so 0.95 is exactly 95 and
    0.895 is exactly 89.5. Anything that is not a finite number from 0 to 1 (a missing
    score, NaN, infinity, a bool, a probability outside the range) is not a score.
    """
    if score is None or isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if not math.isfinite(score) or not 0 <= score <= 1:
        return None
    return Decimal(repr(float(score))) * 100


def percent_text(score) -> str:
    """The score as the note shows it: ``95``, ``99.5``."""
    percent = _score_percent(score)
    return '?' if percent is None else format(percent.normalize(), 'f')


def qualifies(mapping, threshold, *, concept) -> bool:
    """Whether the proposal just written on *mapping* is one to approve.

    Needs a destination, a score a ranker stood behind (the first retrieval hit written
    because no ranker could be reached is not an answer, nor is a UMLS shortcut, which
    nothing ranked) and a score whose percent is at or above the threshold as it is: no
    rounding, so 100 means exactly 1.0. A score that is not a finite number from 0 to 1
    does not qualify; it never fails the run.
    """
    if concept is None or mapping.suggestion_fallback:
        return False
    percent = _score_percent(mapping.suggestion_confidence)
    return percent is not None and percent >= threshold


def approve(mapping, user, threshold, *, note_written=False) -> str:
    """Approve *mapping*'s proposal as *user*: ``'approved'``, ``'declined'`` or ``'error'``.

    ``'declined'``: the row stays proposed, for a person to decide, because the approval
    was refused (another curator has the destination concept locked, Athena already
    supplies the mapping, the proposer is the approver) or *user* may no longer approve.
    ``'error'``: something unexpected failed; the row's approval was rolled back and it
    stays proposed, and the run carries on.
    """
    from patient_portal.api.views import _upsert_source_code_mapping

    # Re-checked for every row: a run is long, and an account that lost the right, or was
    # deactivated, since it started must not approve the rest.
    fresh = get_user_model().objects.filter(pk=getattr(user, 'pk', None), is_active=True).first()
    if fresh is None or not eligible(fresh):
        return 'declined'
    try:
        with transaction.atomic():
            _upsert_source_code_mapping(
                mapping.target_concept, {'status': 'approved'}, fresh,
                mapping=mapping, record_decision=False,
            )
            fields = ['origin_system', 'suggestion_outcome', 'updated_at']
            mapping.origin_system = approved_origin(threshold)
            # The first *curator* disposition of a suggestion: nobody has made one.
            mapping.suggestion_outcome = ''
            if note_written:
                mapping.notes = (
                    f'{mapping.notes} Auto-approved by Suggest: {percent_text(mapping.suggestion_confidence)}% '
                    f'confidence, threshold {threshold}%.'
                ).strip()
                fields.append('notes')
            mapping.save(update_fields=fields)
    except APIException as exc:
        logger.info('Auto-approval of mapping %s declined: %s', mapping.pk, exc.detail)
        mapping.refresh_from_db()
        return 'declined'
    except Exception as exc:  # noqa: BLE001 - one row must not fail the run
        # The atomic block rolled the row back. Log the kind, never the message: it can
        # carry a source value.
        logger.error('Auto-approval of mapping %s failed (%s); left proposed.', mapping.pk, type(exc).__name__)
        mapping.refresh_from_db()
        return 'error'
    return 'approved'
