"""Rejoin the promotion lane with the new `dev` leaves.

`dev` gained two leaves after the last sync: #2417
(0335_group_access_role_additive) and #2452 (0335_sccm_updated_at_index),
rejoined on `dev` by 0336_merge_role_additive_and_sccm_updated_at_index.
`cb-v2` carries its own 0335_merge_20261110_2045, which joined the promotion
lane with `dev`'s 0334_sccm_curator_note. Merging `dev` into `cb-v2` therefore
leaves 0335_merge_20261110_2045 and
0336_merge_role_additive_and_sccm_updated_at_index as two leaves, and `migrate`
refuses to run (#2418). No migration is renamed: every one of them is already
applied on some database, and a rename would run it a second time. An empty
merge migration depending on both leaves is the repair AGENTS.md prescribes.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0335_merge_20261110_2045"),
        ("omop_core", "0336_merge_role_additive_and_sccm_updated_at_index"),
    ]

    operations = []
