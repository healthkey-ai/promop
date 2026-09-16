"""Join cytogenetic persistence with the landed genomics/provenance merge."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0228_merge_cytogenetics_and_genomics'),
        ('omop_core', '0228_merge_20260912_1019'),
    ]

    operations = []
