"""Join duplicate graph repairs merged concurrently in #1291 and #1294."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0234_merge_blood_count_and_genomics'),
        ('omop_core', '0234_merge_blood_units_and_genomics'),
    ]
    operations = []
