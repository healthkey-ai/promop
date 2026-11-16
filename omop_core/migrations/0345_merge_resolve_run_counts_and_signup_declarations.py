"""Rejoin the two leaves #2598 left on dev beside #2597 and #2621.

#2598 added 0343_organization_requires_signup_declarations on
0342_seed_chronic_condition_override. By the time it merged, #2597
(0343_mint_openwearables_local_concepts) and #2621
(0344_resolve_run_provisional_counts) were already on dev, so its green
result described an older base and the fork appeared only at the merge:
the conflict check and the Django suite fail on every open PR.
Empty by design - it only reconnects the graph.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0343_organization_requires_signup_declarations'),
        ('omop_core', '0344_resolve_run_provisional_counts'),
    ]

    operations = [
    ]
