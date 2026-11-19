"""Mark the stored suggestions that were only a fallback (#2743).

When no ranker can be reached, Suggest writes the first retrieval candidate as
the proposal and leaves a note beginning "Best-match fallback". Those rows
carry a model version and a stored suggestion like any other, so their
outcomes were scored as the model's. This marks the rows already written,
from that note, which is the only record of it. It is a best-effort backfill,
not an exact one:

* A fallback written onto a row that already had a note never carried the
  fallback text (both writers keep an existing note), and is not found.
* A note left by a fallback and kept when a later real pick replaced the
  destination (the writer keeps the note of a row a curator has edited) marks
  that row, which is then left out of the scores.
* A review archived before this migration is not marked: it holds no note.

Matching on the start of the note, not anywhere in it, keeps a curator's own
text that merely quotes the phrase out. A separate migration from the column's,
so the table's exclusive lock for the ALTER is not held across the scan.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# The opening of the note suggestions.rank_candidates leaves when it falls
# back to a candidate. Frozen here: the migration must keep meaning what it
# meant when it ran, whatever the note says later.
FALLBACK_NOTE = 'Best-match fallback'


def mark_fallbacks(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    # Only a row that holds a stored suggestion has one to mark. One UPDATE,
    # and deliberately not a touch of updated_at: nothing a reader of the
    # mapping list is sent has changed.
    marked = Mapping.objects.filter(
        suggested_target_concept_id__isnull=False, notes__startswith=FALLBACK_NOTE,
        suggestion_fallback=False,
    ).update(suggestion_fallback=True)
    logger.warning('Marked %d stored suggestions as ranker-unavailable fallbacks.', marked)


def unmark_fallbacks(apps, schema_editor):
    # Rolling back 0359 drops the column; rolling back only this leaves the
    # marks, which describe real rows and are harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0359_suggestion_fallback"),
    ]

    operations = [
        migrations.RunPython(mark_fallbacks, unmark_fallbacks),
    ]
