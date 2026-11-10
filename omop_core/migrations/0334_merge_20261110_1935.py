from django.db import migrations


class Migration(migrations.Migration):
    """Join the two integration lanes after promoting `cb-v2` into `dev`.

    `cb-v2` carries the clinical-input/descriptor merge leaf
    (`0332_merge_20261109_2143`); `dev` carries the concept-note and
    field-placement leaves (`0332_concept_mapping_note` -> `0333_field_mapping_tab`).
    No operations: both branches' schema changes are independent.
    """

    dependencies = [
        ('omop_core', '0332_merge_20261109_2143'),
        ('omop_core', '0333_field_mapping_tab'),
    ]

    operations = []
