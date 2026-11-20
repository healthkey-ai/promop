"""Auto-approving a Suggest result that the ranker is sure of (#1532).

A Suggest run writes every proposal as ``proposed`` and a curator approves it. A
curator who trusts the ranker at a high confidence can have the run approve it as
it writes it. Three rules shape this:

* **The threshold is a whole percent from 90 to 100, default 100.** Below 90 a
  ranker's answer is not one a run should sign off on, so the floor is not a
  preference. 100 approves only a score that rounds to 100%, which is what the
  default means to say: certain matches only.
* **It is the curator's choice and it sticks.** The last value an approver set is
  kept on their identity (``Identity.suggest_auto_approve_threshold``) and is what
  their next run uses and what the Suggest controls show; a run records the
  threshold it used on ``SuggestRun.selection``.
* **Only an approver gets it.** Approval is staff and org admin only
  (``_can_approve_mappings``). An analyst may run Suggest and never causes an
  approval; the setting is ignored for them and the page does not offer it. A
  service credential is a machine and never auto-approves.

The approval itself is the one a curator's is: :func:`approve` calls
``_upsert_source_code_mapping``, so the status, the reviewer, the re-point of
stored clinical rows, the promotion of provisional rows and the relocation rules
all run once, in one place. Only what the machine decided is recorded
differently: ``origin_system`` says it was auto-approved and at what threshold,
no ``decision_*`` column is stamped (those record a curator's decision), and the
suggestion's outcome is left blank (it is the *curator's* first disposition of a
suggestion, and nobody has reviewed this one).
"""
from __future__ import annotations

import logging

from django.db import transaction
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)

MIN_THRESHOLD = 90
MAX_THRESHOLD = 100
DEFAULT_THRESHOLD = 100

THRESHOLD_MESSAGE = f'Must be a whole number from {MIN_THRESHOLD} to {MAX_THRESHOLD}.'


def parse_threshold(raw):
    """*raw* as a whole percent 90-100, or ``None`` when nothing was sent.

    ``None`` and an empty string mean "use mine"; anything else that is not a
    whole number in range raises ``ValueError`` (a 400, never a clamp).
    """
    if raw is None or raw == '':
        return None
    if isinstance(raw, bool):
        raise ValueError(THRESHOLD_MESSAGE)
    if isinstance(raw, float):
        if not raw.is_integer():
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


def saved_threshold(user) -> int:
    """The threshold *user* last set, or the default."""
    value = getattr(user, 'suggest_auto_approve_threshold', None)
    return value if value is not None else DEFAULT_THRESHOLD


def remember(user, threshold) -> None:
    """Keep *threshold* as *user*'s own, so the next run starts from it."""
    if user.suggest_auto_approve_threshold == threshold:
        return
    user.suggest_auto_approve_threshold = threshold
    user.save(update_fields=['suggest_auto_approve_threshold'])


def run_threshold(user, raw):
    """The threshold a run started by *user* uses, or ``None`` for no auto-approval.

    *raw* is what the request sent (validated: out of range raises
    ``ValueError`` for everyone, so a client bug shows up on any account). For
    an account that may not approve, the setting is ignored and nothing is
    stored. For an approver a value that was sent is remembered; none sent uses
    what they last set.
    """
    sent = parse_threshold(raw)
    if not eligible(user):
        return None
    if sent is None:
        return saved_threshold(user)
    remember(user, sent)
    return sent


def approved_origin(threshold) -> str:
    """The provenance an auto-approved row carries (begins ``suggest``, so it reads as the run's)."""
    from omop_core.mapping.suggestions import SUGGESTION_PROVENANCE

    return f'{SUGGESTION_PROVENANCE} auto-approved {threshold}%'[:50]


def qualifies(mapping, threshold, *, concept) -> bool:
    """Whether the proposal just written on *mapping* is one to approve.

    Needs a destination, a score a ranker stood behind (the first retrieval hit
    written because no ranker could be reached is not an answer, nor is a UMLS
    shortcut, which nothing ranked) and a score that rounds to the threshold or
    more, in the same whole percent a provisional row carries.
    """
    from omop_core.mapping.code_resolution import _confidence_percent

    if concept is None or mapping.suggestion_fallback or mapping.suggestion_confidence is None:
        return False
    return _confidence_percent(mapping.suggestion_confidence) >= threshold


def approve(mapping, user, threshold, *, note_written=False) -> bool:
    """Approve *mapping*'s proposal as *user*; ``True`` when it is now approved.

    Declines quietly (the row stays proposed, for a person to decide) when the
    approval is refused: another curator has the destination concept locked,
    Athena already supplies the mapping, and the like.
    """
    from omop_core.mapping.code_resolution import _confidence_percent
    from patient_portal.api.views import _upsert_source_code_mapping

    percent = _confidence_percent(mapping.suggestion_confidence)
    try:
        with transaction.atomic():
            _upsert_source_code_mapping(
                mapping.target_concept, {'status': 'approved'}, user,
                mapping=mapping, record_decision=False,
            )
            fields = ['origin_system', 'suggestion_outcome', 'updated_at']
            mapping.origin_system = approved_origin(threshold)
            # The first *curator* disposition of a suggestion: nobody has made one.
            mapping.suggestion_outcome = ''
            if note_written:
                mapping.notes = (
                    f'{mapping.notes} Auto-approved by Suggest: {percent}% confidence, '
                    f'threshold {threshold}%.'
                ).strip()
                fields.append('notes')
            mapping.save(update_fields=fields)
    except APIException as exc:
        logger.info('Auto-approval of mapping %s declined: %s', mapping.pk, exc.detail)
        mapping.refresh_from_db()
        return False
    return True
