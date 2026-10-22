"""Use each instance's Athena SCCM mappings to reconcile approved ICD-10 rows."""
import json

from django.db import migrations


def reconcile_mappings(apps, schema_editor):
    from omop_core.data_migrations.athena_sccm_reconcile_v1 import reconcile, summarize
    receipts = reconcile(apps, schema_editor.connection)
    print('Athena SCCM reconciliation (0259): ' + json.dumps(summarize(receipts), sort_keys=True))


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('omop_core', '0258_reconcile_approved_icd10_from_athena_export')]
    # Preserve completed corrections and subsequent curator edits on rollback.
    operations = [migrations.RunPython(reconcile_mappings, migrations.RunPython.noop)]
