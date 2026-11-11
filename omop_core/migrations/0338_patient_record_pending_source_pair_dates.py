"""Remember the entered day of each pending lab source pair (#2426).

Additive with a `{}` default: every existing record reads as "no date", which
keeps its pending pair in place until an explicit canonical event. A legacy
pair's day cannot be reconstructed, and guessing one from a revision timestamp
would retire a patient's answer on evidence the record never had.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0337_merge_20261111_1944"),
    ]

    operations = [
        migrations.AddField(
            model_name="patientrecord",
            name="pending_source_pair_dates",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Server-managed: the clinical day (YYYY-MM-DD) each pending source pair was entered for, so a newer imported result retires the pair (#2426). Never written by clients.",
            ),
        ),
    ]
