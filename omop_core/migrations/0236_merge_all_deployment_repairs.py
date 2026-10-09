"""Join both 0235 repairs; preserve every already-deployed migration history."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("omop_core", "0235_merge_concurrent_0234_branches"),
        ("omop_core", "0235_merge_concurrent_deployment_repairs"),
    ]

    operations = []
