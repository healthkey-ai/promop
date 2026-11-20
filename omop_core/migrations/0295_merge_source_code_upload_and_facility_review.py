"""Rejoin the two leaves #1844 and #1856 left on dev.

Both branched off 0291_source_system_facility and each was green on its
own, so the fork only appeared once both had merged: the conflict check
and every migration-graph test failed, which skipped the staging deploy.
Empty by design - it only reconnects the graph.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0293_patientsourcecodeupload'),
        ('omop_core', '0294_source_system_facility_review'),
    ]

    operations = [
    ]
