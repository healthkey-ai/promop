"""What "Delete" does to a code mapping (#2744).

A mapping row is two things: a destination, and the queue entry for a source
code. Removing the row removes both, and a code that patient data carries
then has rows in the clinical tables and nothing in the queue to map them
with. So Delete depends on the code:

* **A code patient data carries** keeps its row. The destination is cleared,
  the row goes back to the unmapped queue, and the rows stored at the old
  destination are moved back to "no matching concept" off the request
  (``repoint_jobs.queue_unpoint``): on a common code that move is minutes.
* **A code no patient data carries** -- a mapping made for a mistyped code, a
  proposal for a catalog code nobody has -- is removed, as #1888 asked.

An unmapped row is never deleted here; ``code_mapping_detail`` refuses it.
"""
from django.utils import timezone

from omop_core.models import PatientSourceCode
from omop_core.services.source_vocabularies import VOCABULARY_OID_ALIASES, canonical_source_vocabulary

UNMAP = 'unmap'
DELETE = 'delete'


def in_patient_data(mapping):
    """Whether any patient's data is known to carry this source code.

    The Seen count is what ingest and the lookup endpoint keep; the
    per-patient inventory covers a code whose count has not been rebuilt. The
    clinical tables are not scanned: their source value is not indexed.
    """
    if mapping.occurrence_count:
        return True
    canonical = canonical_source_vocabulary(mapping.source_vocabulary_id)
    # The ETL posts the system it received; the mapping stores the canonical id.
    vocabularies = {mapping.source_vocabulary_id, canonical, *(
        alias for alias, target in VOCABULARY_OID_ALIASES.items() if target == canonical)}
    return PatientSourceCode.objects.filter(
        source_value=mapping.source_code, source_vocabulary_id__in=vocabularies).exists()


def delete_effect(mapping):
    """What Delete would do to *mapping*: ``'unmap'``, ``'delete'``, or None
    for a row with no destination, which is not deleted."""
    if mapping.target_concept_id is None:
        return None
    return UNMAP if in_patient_data(mapping) else DELETE


def clear_destination(mapping, user):
    """Send *mapping* back to the unmapped queue; return the destination it had.

    The source side, the curator's note, the axis variants and what Suggest
    once proposed (``suggested_target_concept``, ``suggestion_outcome``,
    ``suggestion_auto_approved``, ``auto_approved_confidence``) are kept: they
    describe the code and its history, not the destination. The one thing it
    records is a first disposition for an approved, flagged row nobody has
    reviewed: removing the destination is a person overriding it. The old destination joins
    ``pending_repoint_concept_ids``, so the next approval moves any stored
    row the background move did not reach.
    """
    previous = mapping.target_concept_id
    pending = list(mapping.pending_repoint_concept_ids or [])
    if previous is not None and previous not in pending:
        pending.append(previous)
    # A person is removing the destination of an auto-approval nobody has reviewed:
    # that is their first disposition of it, an override (#2856).
    if (mapping.suggestion_auto_approved and not mapping.suggestion_outcome
            and mapping.status == 'approved'):
        mapping.suggestion_outcome = 'overridden'
    cleared = {
        'target_concept': None, 'destination_vocabulary_id': '', 'destination_unit_concept': None,
        'status': 'proposed', 'reviewer': None, 'reviewed_at': None,
        # Nobody's pick, and no machine's: the row is open for a new proposal.
        'proposed_by': None, 'origin_system': '', 'suggested_action': '',
        'suggestion_confidence': None, 'decision_confidence': None,
        'pending_repoint_concept_ids': pending,
        'locked_by': None, 'locked_at': None,
        'updated_by': user, 'updated_at': timezone.now(),
        'suggestion_outcome': mapping.suggestion_outcome,
    }
    for field, value in cleared.items():
        setattr(mapping, field, value)
    mapping.save(update_fields=list(cleared))
    return previous
