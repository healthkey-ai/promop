"""Mark the suggestions Suggest approved by itself (#2856): the backfill.

``suggest_auto_approve.approve`` stamped ``origin_system`` with
"suggest v0.4 auto-approved <T>%" before the column existed, and that stamp is
the only record of which rows were auto-approved. This marks those rows, and
copies each one's current ``suggestion_confidence`` to ``auto_approved_confidence``
(the score the analytics bands read). It is best-effort, not exact:

* A row a curator re-approved or edited since had its ``origin_system``
  replaced by theirs. The note Suggest appends ("Auto-approved by Suggest: ...")
  survives for most of them, so a second branch marks a row whose note carries
  it, but only a row still approved at the destination Suggest suggested
  (``target_concept_id = suggested_target_concept_id``) and still holding a model
  version: a row whose auto-approved suggestion was replaced since is not the one
  the note describes. A row whose note was rewritten as well is not found.
* ``auto_approved_confidence`` is the live ``suggestion_confidence``, which a
  curator's save may have cleared or replaced since the approval, so it can be
  null (the row then counts in the Overall row of the analytics and in no band)
  or a later score.
* A review already archived to ``MappingSuggestionReview`` holds neither field
  and is not marked.

A separate migration from the columns', so the table's exclusive lock for the
ALTER is not held across the scan. One UPDATE per branch, with the predicate in
the statement and no row loaded; it leaves ``updated_at`` alone, because nothing
a reader of the mapping list is sent has changed. The log carries counts only.
"""
import logging

from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)

# Frozen here: the migration must keep meaning what it meant when it ran,
# whatever the live constants say later.
SUGGEST_PROVENANCE_PREFIX = 'suggest'
AUTO_APPROVED_MARK = 'auto-approved'
AUTO_APPROVED_NOTE = 'Auto-approved by Suggest: '


def mark_auto_approved(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    unmarked = Mapping.objects.filter(suggestion_auto_approved=False, suggestion_fallback=False)
    by_stamp = unmarked.filter(
        origin_system__istartswith=SUGGEST_PROVENANCE_PREFIX,
        origin_system__contains=AUTO_APPROVED_MARK,
    ).update(suggestion_auto_approved=True, auto_approved_confidence=F('suggestion_confidence'))
    by_note = unmarked.filter(
        notes__contains=AUTO_APPROVED_NOTE, status='approved',
        suggested_target_concept_id__isnull=False,
        target_concept_id=F('suggested_target_concept_id'),
        suggestion_model_version__gt='',
    ).update(suggestion_auto_approved=True, auto_approved_confidence=F('suggestion_confidence'))
    logger.warning(
        'Marked %d stored suggestions as auto-approved by Suggest (%d by the origin stamp, %d by the note).',
        by_stamp + by_note, by_stamp, by_note,
    )


def unmark_auto_approved(apps, schema_editor):
    # Rolling back 0372 drops the columns; rolling back only this leaves the
    # marks, which describe real rows and are harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0372_suggestion_auto_approved"),
    ]

    operations = [
        migrations.RunPython(mark_auto_approved, unmark_auto_approved),
    ]
