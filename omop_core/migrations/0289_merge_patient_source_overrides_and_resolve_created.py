"""Join independent 0288 migrations merged into dev from #1822 and #1823."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0288_patient_source_context_overrides'),
        ('omop_core', '0288_sourcecoderesolverun_created'),
    ]

    operations = []
