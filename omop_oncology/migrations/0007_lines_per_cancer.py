"""Lines of therapy per cancer (#1739).

1. ``episode_parent_id`` becomes a BigInteger, like ``episode_id``: the ids it
   holds no longer fit in 32 bits.
2. One Disease Episode (32528) per person and cancer.

Existing lines are left unparented: a line no writer filed under a cancer is
the primary cancer's, whichever that is when it is read. There is nothing to
backfill.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_oncology', '0006_add_episode_pk_sequence'),
        ('omop_core', '0281_merge_corrected_units_and_source_code_resolve'),
    ]

    operations = [
        migrations.AlterField(
            model_name='episode',
            name='episode_parent_id',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='episode',
            constraint=models.UniqueConstraint(
                condition=models.Q(episode_concept_id=32528),
                fields=('person', 'episode_source_value'),
                name='uniq_disease_episode_per_cancer',
            ),
        ),
    ]
