"""Reconcile hand-mapped ICD-10 SCCM rows against Athena's live STCM table.

For each approved, non-Athena ICD-10 SourceCodeConceptMapping row, check if
the source_to_concept_map now has a valid standard Athena mapping.  If so,
update the destination (if different) and set origin_system='athena'.  Rows
move from the Mapped section to the Athena Mapped section in the UI.

Reads the live STCM table (no frozen snapshot). Successfully reconciled rows
are excluded on reruns; unmatched and skipped rows remain eligible.

This updates mapping definitions for subsequent resolution. It does not
rewrite existing clinical facts or rederive patient records.
"""
from django.db import migrations


def load_reconciliation(apps, schema_editor):
    from omop_core.data_migrations.athena_icd10_stcm_reconcile import (
        reconcile, summarize,
    )
    receipts = reconcile(apps, schema_editor.connection)
    summary = summarize(receipts)
    reattributed = summary.get('destination_changed', 0) + summary.get('reattributed', 0)
    print(f'ICD-10 STCM reconciliation: {reattributed} provenances changed to athena '
          f'({summary.get("destination_changed", 0)} destination changed, '
          f'{summary.get("reattributed", 0)} confirmed). '
          f'{summary.get("no_stcm_match", 0)} no STCM match, '
          f'{summary.get("multiple_targets", 0)} multiple targets skipped.')


class Migration(migrations.Migration):
    atomic = False  # batch transactions handled internally
    dependencies = [
        ('omop_core', '0255_merge_0254_patientrecord_ix_pr_email_upper_and_schema_drift_cleanup'),
    ]
    operations = [migrations.RunPython(load_reconciliation, migrations.RunPython.noop)]
