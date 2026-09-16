"""Originally widened Measurement/Observation value_as_string to TextField.

Withdrawn: text exceeding the CDM column width (60 chars) is now stored in
a linked NOTE row. The columns stay at CharField(max_length=60).
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0221_seed_treatment_editor_catalogs')]
    operations = []  # No-op: column widening withdrawn per genomics_architecture_v2 §5.
