# Rename the patient drug-class "type" fields to match the HemOnc/OMOP "type"
# wording used by the trial omop_therapy_types_* columns and the matcher
# (PR #370 review). RenameField (not add/remove) so any DB that already applied
# 0131 keeps its populated values across the upgrade.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0131_patientrecord_component_class_ids"),
    ]

    operations = [
        migrations.RenameField(
            model_name="patientrecord",
            old_name="first_line_component_class_ids",
            new_name="first_line_therapy_type_ids",
        ),
        migrations.RenameField(
            model_name="patientrecord",
            old_name="second_line_component_class_ids",
            new_name="second_line_therapy_type_ids",
        ),
        migrations.RenameField(
            model_name="patientrecord",
            old_name="later_component_class_ids",
            new_name="later_therapy_type_ids",
        ),
        migrations.RenameField(
            model_name="patientrecord",
            old_name="therapy_component_class_ids",
            new_name="therapy_type_ids",
        ),
    ]
