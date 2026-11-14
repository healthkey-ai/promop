from django.db import migrations


class Migration(migrations.Migration):
    """Join the promotion lanes after the latest `dev` sync.

    `cb-v2` carries `0334_merge_20261110_1935` (which already joins the
    clinical-input and field-placement leaves); `dev` added
    `0334_sccm_curator_note`. No operations: the schema changes are independent.
    """

    dependencies = [
        ('omop_core', '0334_merge_20261110_1935'),
        ('omop_core', '0334_sccm_curator_note'),
    ]

    operations = []
