"""Mark the suggestions Suggest approved by itself (#2856): the backfill.

``suggest_auto_approve.approve`` stamped ``origin_system`` with
"suggest v0.4 auto-approved <T>%" before the column existed, and that stamp is
the only record of which rows were auto-approved. This marks those rows. It is
best-effort, not exact:

* A row a curator re-approved or edited since had its ``origin_system``
  replaced by theirs. The note Suggest appends ("Auto-approved by Suggest: ...")
  survives for most of them, so a note that carries it also marks the row, when
  the row holds a stored suggestion and is not a fallback. A row whose note was
  rewritten as well is not found, and its review is counted as any other.
* A review already archived to ``MappingSuggestionReview`` holds neither field
  and is not marked.

A separate migration from the column's, so the table's exclusive lock for the
ALTER is not held across the scan. One UPDATE, with the predicate in the
statement and no row loaded; it leaves ``updated_at`` alone, because nothing a
reader of the mapping list is sent has changed.
"""
import logging

from django.db import migrations
from django.db.models import Q

logger = logging.getLogger(__name__)

# Frozen here: the migration must keep meaning what it meant when it ran,
# whatever the live constants say later.
SUGGEST_PROVENANCE_PREFIX = 'suggest'
AUTO_APPROVED_MARK = 'auto-approved'
AUTO_APPROVED_NOTE = 'Auto-approved by Suggest: '


def mark_auto_approved(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    marked = Mapping.objects.filter(
        suggestion_auto_approved=False, suggestion_fallback=False,
    ).filter(
        Q(origin_system__istartswith=SUGGEST_PROVENANCE_PREFIX,
          origin_system__contains=AUTO_APPROVED_MARK)
        | Q(notes__contains=AUTO_APPROVED_NOTE, suggested_target_concept_id__isnull=False)
    ).update(suggestion_auto_approved=True)
    logger.warning('Marked %d stored suggestions as auto-approved by Suggest.', marked)


def unmark_auto_approved(apps, schema_editor):
    # Rolling back 0372 drops the column; rolling back only this leaves the
    # marks, which describe real rows and are harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0372_suggestion_auto_approved"),
    ]

    operations = [
        migrations.RunPython(mark_auto_approved, unmark_auto_approved),
    ]
