"""Merge the LOINC receptor-mapping and concept-relationship migration heads."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0197_seed_pr_her2_loinc_mappings'),
        ('omop_core', '0199_drop_orphaned_cr_provenance_columns'),
    ]

    operations = []
