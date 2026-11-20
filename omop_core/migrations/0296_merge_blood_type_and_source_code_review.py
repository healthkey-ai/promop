"""Rejoin the two leaves #1831 left on dev.

#1831 added 0290_patientrecord_blood_type off 0289_merge_20261013_1559, while
dev had moved on to 0295_merge_source_code_upload_and_facility_review. Each was
green on its own; once both had merged the conflict check and the
migration-graph tests failed, which skipped the staging deploy.
Empty by design - it only reconnects the graph.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0290_patientrecord_blood_type'),
        ('omop_core', '0295_merge_source_code_upload_and_facility_review'),
    ]

    operations = [
    ]
