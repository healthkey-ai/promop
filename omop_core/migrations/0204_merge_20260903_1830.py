"""Two leaves, both descended from 0200, merged.

`0201_retire_legacy_surveys` (the PROlog work) and `0203_raw_umls_tables`
(the UMLS work on dev) were written in parallel against the same parent, so
the graph has two ends and Django refuses to migrate until one is named.

Empty on purpose: neither branch's schema conflicts with the other's, and
renumbering either would strand the deployments that have already applied it —
the staging survey deployment has 0201_retire_legacy_surveys in
django_migrations, and a rename would make Django try to drop those tables a
second time.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("omop_core", "0201_retire_legacy_surveys"),
        ("omop_core", "0203_raw_umls_tables"),
    ]

    operations = []
