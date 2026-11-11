"""Rejoin the promotion lane with `dev`'s source-record-key migrations.

`dev` gained #2455 (`0337_source_record_key`, `0338_source_record_key_unique`),
which do not descend from `cb-v2`'s `0337_merge_20261111_1944`. Merging `dev`
into `cb-v2` therefore leaves both as leaves and `migrate` refuses to run
(#2418). No migration is renamed: every one of them is already applied on some
database, and a rename would run it a second time. An empty merge migration
depending on both leaves is the repair AGENTS.md prescribes.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0337_merge_20261111_1944"),
        ("omop_core", "0338_source_record_key_unique"),
    ]

    operations = []
