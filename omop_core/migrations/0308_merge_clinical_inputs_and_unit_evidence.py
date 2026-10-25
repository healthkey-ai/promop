"""Join the clinical-input and unit-evidence migration leaves.

The cb-v2 clinical source-input line (#1987/#1988) ends at
``0307_merge_clinical_inputs_preexisting_attestation``; the dev line ends at
``0307_unit_evidence_rebuild`` (#2043/#2044). The back-merge brings both
together, so this empty migration rejoins the graph. No schema change.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0307_merge_clinical_inputs_preexisting_attestation'),
        ('omop_core', '0307_unit_evidence_rebuild'),
    ]

    operations = []
