"""Join the independent #1993 source-input and #1995 attestation histories.

Preserve both existing migration identifiers; this migration changes no data.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0306_patientrecord_able_to_swallow_tablets_capsules_and_more'),
        ('omop_core', '0306_preexisting_conditions_attestation'),
    ]

    operations = []
