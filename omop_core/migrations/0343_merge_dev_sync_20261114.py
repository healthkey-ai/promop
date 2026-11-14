"""Rejoin the two leaves the 2026-11-14 dev sync left on cb-v2.

`dev` added `0340_condition_source_status` -> `0341_chronic_condition_override`
-> `0342_seed_chronic_condition_override` off `0339_clinical_row_confidence`,
while `cb-v2` already ended at `0342_merge_20261113_1812` (which joined
`0339_clinical_row_confidence` with `0341_authored_answer_formula_check`).
Each side was green on its own, so the fork appears only in the merged tree:
`migrate` refuses and every migration-graph test fails. Empty by design - it
only reconnects the graph, and nothing is renamed.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0342_merge_20261113_1812'),
        ('omop_core', '0342_seed_chronic_condition_override'),
    ]

    operations = [
    ]
