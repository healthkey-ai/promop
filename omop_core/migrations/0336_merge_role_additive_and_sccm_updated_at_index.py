"""Rejoin the two 0335 leaves on dev.

#2417 (0335_group_access_role_additive, roles additive) and #2452
(0335_sccm_updated_at_index) were each green on their own base and merged
into dev on the same day, leaving two leaf migrations: migrate refuses to
run and CI fails on every pull request. Neither is renamed, because
0335_sccm_updated_at_index is already applied on staging and a renamed
migration would run a second time. An empty merge migration depending on
both is the repair AGENTS.md prescribes.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0335_group_access_role_additive"),
        ("omop_core", "0335_sccm_updated_at_index"),
    ]

    operations = []
