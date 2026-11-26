"""Rejoin the two leaves #2905 and #2902 left on dev.

Both added a 0376 migration on 0375_recordrevision_source:
0376_hematologic_spread_concept_and_scope (#2905, merged first) and
0376_oura_connection (#2902). Each PR was green against the dev it was tested
on, so the fork appeared only after both merged: the post-merge conflict check
failed on dev, and both backend suites fail on PRs tested against it. The two
touch different tables (a data repair of field mappings and disease
memberships, and a new model), so their order does not matter.
Empty by design - it only reconnects the graph.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0376_hematologic_spread_concept_and_scope'),
        ('omop_core', '0376_oura_connection'),
    ]

    operations = []
