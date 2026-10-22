"""Join independently merged blood-count units and PALB2 naming migrations."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0233_blood_count_units_640'),
        ('omop_core', '0233_genomics_palb2_naming'),
    ]
    operations = []
