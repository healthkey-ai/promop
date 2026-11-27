"""Remove the auto-created HK-Labs (``hkl:``) concepts (#2922).

HealthKey created ``hkl:<slug>`` concepts (vocabulary HK-Labs, non-standard)
for lab names it had no LOINC match for. They sat ahead of the standard concept
in concept search and invited wrong mappings, and a concept with no standard
equivalent is minted by a curator through ``code-mappings/mint-destination/``,
not created by an importer. So none is kept. For each one present, this
migration:

1. repoints every reference to the standard LOINC concept when it duplicates one
   of eighteen serum/plasma tests (``hkl:psa`` and LOINC 2857-1, ...), or is a
   LOINC code filed under HK-Labs whose standard concept is loaded. The
   standard concept is found by (vocabulary_id, concept_code), never by id;
2. otherwise clears the reference and guesses nothing: a measurement's source
   concept becomes 0, a mapping loses its destination and returns to the
   curators' queue, and an approved mapping is demoted to proposed with a note;
3. deletes the concept and the rows that exist only for it (synonyms,
   relationships, ancestry, embeddings, search terms, curator priority, done,
   lock and decline rows, candidate destinations; a curator's note moves to the
   standard concept when there is one and it has none). The HK-Labs vocabulary
   row stays, so the mint flow can still use it.

References are found by looking at the models, in every table that has one.
Every cleared reference, and every approved mapping demoted, is logged at
WARNING. A referencing row of a listed duplicate that says it is urine, a 24 hour
collection, saliva or a per-mass figure such as ng/mg is not serum or plasma, so
a serum code would be wrong for it: that row is cleared, not repointed, and the
serum rows are repointed. A concept is kept, and logged why, only when a listed
duplicate's standard concept is missing, inactive or not standard here, or when
something refuses to be cleared (a field answer mapped to it). Each concept is handled in a savepoint of its own, so a failure
on one leaves that concept as it was.

Idempotent: a second run finds nothing. On a database with no hkl: concepts (a
fresh install, CI) it does nothing. Read what it will do first with
``manage.py audit_hkl_duplicates`` (read-only, exits 1 when a concept would be
kept).

The reverse is a no-op: deleted concepts, their synonyms and ancestry cannot be
restored, and repointed or cleared references are not put back. Patient records
whose rows changed are marked for re-derivation (``backfill_patient_records``).

The cleanup lives in ``omop_core/services/hkl_duplicates.py`` so the audit and
this migration cannot disagree; it takes this migration's own ``apps`` and
imports no model. It depends on omop_oncology because that app's tables have
foreign keys to Concept too, and the migration's ``apps`` must contain them.
"""
import logging

from django.db import migrations

from omop_core.services import hkl_duplicates

logger = logging.getLogger(__name__)


def remove_hkl_concepts(apps, schema_editor):
    using = schema_editor.connection.alias
    plans = hkl_duplicates.build_plan(apps, using=using)
    if not plans:
        return
    summary = hkl_duplicates.apply_plan(apps, plans, using=using)
    logger.warning(
        'HK-Labs cleanup (#2922): removed %d of %d concept(s), kept %d; %d reference(s) repointed '
        'to a standard concept, %d cleared, %d approved mapping(s) demoted to proposed, '
        '%d dependent row(s) deleted, %d note(s) moved. Run backfill_patient_records for the '
        'patients whose rows changed.',
        summary.deleted, len(plans), summary.skipped, summary.rows_repointed,
        summary.rows_cleared, summary.demoted, summary.rows_deleted, summary.notes_moved,
    )


def reverse_noop(apps, schema_editor):
    # Deleted concepts (and their synonyms, relationships and ancestry) cannot be
    # restored, and repointed or cleared references are not put back. Rolling
    # back leaves the data as it is.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0379_custom_field_retired_at'),
        ('omop_oncology', '0012_therapy_line_response_assessed'),
    ]

    operations = [
        migrations.RunPython(remove_hkl_concepts, reverse_noop),
    ]
