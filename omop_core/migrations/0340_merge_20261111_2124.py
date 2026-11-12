"""Rejoin `cb-v2`'s two promotion leaves.

#2465 (`0338_patient_record_pending_source_pair_dates`, the pair-day
bookkeeping for #2426) and #2464 (`0339_merge_20261111_2112`, the rejoin with
`dev`'s source-record-key migrations) both descend from
`0337_merge_20261111_1944`, so merging either after the other leaves two leaf
nodes and `migrate` refuses (#2418). No migration is renamed: both are applied
on some database, and a rename would run one of them a second time. An empty
merge migration depending on both is the repair AGENTS.md prescribes.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0338_patient_record_pending_source_pair_dates"),
        ("omop_core", "0339_merge_20261111_2112"),
    ]

    operations = []
