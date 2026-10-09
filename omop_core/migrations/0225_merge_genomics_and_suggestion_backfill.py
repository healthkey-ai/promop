from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0224_seed_genomics_mappings'),
        ('omop_core', '0223_backfill_approved_suggestion_outcomes'),
    ]
    operations = []
