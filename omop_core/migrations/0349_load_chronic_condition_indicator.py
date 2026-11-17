"""Load AHRQ's CCIR release bundled with the code (#2512).

The loader is idempotent and ``load_chronic_condition_indicators --csv`` loads a
later release. A checkout without the file loads nothing and the PHR then skips
the chronic filter rather than hiding every condition."""
from django.db import migrations

from omop_core.services.chronic_conditions import CCIR_CSV, load_indicators


def load(apps, schema_editor):
    if CCIR_CSV.exists():
        load_indicators(apps.get_model('omop_core', 'ChronicConditionIndicator'))


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0348_chronic_condition_indicator')]
    operations = [migrations.RunPython(load, migrations.RunPython.noop)]
