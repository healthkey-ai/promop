"""Retire the HK-Labs concepts that duplicate a standard serum/plasma LOINC test.

HealthKey minted ``hkl:<slug>`` concepts (vocabulary HK-Labs, non-standard) for
lab names it had no LOINC match for. Eighteen of them name a test LOINC does
have (``hkl:psa`` and LOINC 2857-1, ...), and they sat ahead of the standard
concept in concept search. This migration, for each of them present:

1. finds the standard concept by (vocabulary_id, concept_code), never by id;
2. repoints every reference to it, in every table that has one (found by
   looking at the models), and the text columns that carry its vocabulary and
   code beside a foreign key;
3. deletes the concept and the rows that exist only for it (synonyms,
   relationships, ancestry, embeddings, search terms, curator priority, done,
   lock and decline rows; a curator's note moves to the standard concept when
   it has none).

It keeps a concept, and logs why at WARNING, when

* the standard concept is not loaded here, is inactive or not standard;
* a referencing row says it is urine, a 24 hour collection, saliva or a
  per-mass figure such as ng/mg (a serum code would be wrong for it): that row
  stays on the local concept, the rows that are serum or plasma are repointed,
  and the concept is kept because it is still referenced;
* a reference exists that it does not rewrite (a field answer coded as the
  hkl: code).

Each concept is handled in a savepoint of its own, so a failure on one leaves
that concept exactly as it was. The migration is idempotent: a second run finds
nothing. On a database with no hkl: concepts (a fresh install, CI) it does
nothing. Read what it will do first with
``manage.py audit_hkl_duplicates`` (read-only, exits 1 when a concept would be
kept).

The reverse is a no-op: deleted concepts, their synonyms and their ancestry
cannot be restored, and rows repointed to the standard concept are not
repointed back. Patient records whose rows were repointed are marked for
re-derivation (``backfill_patient_records``).

The cleanup lives in ``omop_core/services/hkl_duplicates.py`` so the audit and
this migration cannot disagree; it takes this migration's own ``apps`` and
imports no model.
"""
import logging

from django.db import migrations

from omop_core.services import hkl_duplicates

logger = logging.getLogger(__name__)


def retire_hkl_duplicates(apps, schema_editor):
    using = schema_editor.connection.alias
    plans = hkl_duplicates.build_plan(apps, using=using)
    if not plans:
        return
    summary = hkl_duplicates.apply_plan(apps, plans, using=using)
    logger.warning(
        'HK-Labs cleanup: removed %d of %d duplicate concept(s), kept %d; %d row(s) repointed, '
        '%d dependent row(s) deleted, %d note(s) moved. Run backfill_patient_records for the '
        'patients whose rows were repointed.',
        summary.deleted, len(plans), summary.skipped, summary.rows_repointed,
        summary.rows_deleted, summary.notes_moved,
    )


def reverse_noop(apps, schema_editor):
    # Deleted concepts (and their synonyms, relationships and ancestry) cannot be
    # restored, and repointed rows are not pointed back. Rolling back leaves the
    # data as it is.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0378_custom_field_dropdown_type'),
    ]

    operations = [
        migrations.RunPython(retire_hkl_duplicates, reverse_noop),
    ]
