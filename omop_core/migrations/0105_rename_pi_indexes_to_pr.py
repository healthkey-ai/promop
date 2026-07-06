# Rename indexes that were created with the old 'pi' (patient_info) prefix to
# the new 'pr' (patient_record) prefix after the table rename in migration 0104.
# ALTER INDEX is a metadata-only operation in PostgreSQL — no table locking.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0104_rename_patientinfo_to_patientrecord"),
    ]

    operations = [
        # Rename the raw-SQL index created in migration 0102
        migrations.RunSQL(
            sql="ALTER INDEX IF EXISTS ix_pi_organization_id RENAME TO ix_pr_organization_id;",
            reverse_sql="ALTER INDEX IF EXISTS ix_pr_organization_id RENAME TO ix_pi_organization_id;",
        ),
        # Rename the auto-generated indexes whose names include the old table prefix
        migrations.RenameIndex(
            model_name="patientrecord",
            new_name="patient_rec_person__721331_idx",
            old_name="patient_inf_person__ea83ef_idx",
        ),
        migrations.RenameIndex(
            model_name="patientrecord",
            new_name="patient_rec_patient_b9bd93_idx",
            old_name="patient_inf_patient_a5f152_idx",
        ),
        migrations.RenameIndex(
            model_name="patientrecord",
            new_name="patient_rec_disease_5f6f3e_idx",
            old_name="patient_inf_disease_15b4ff_idx",
        ),
        migrations.RenameIndex(
            model_name="patientrecord",
            new_name="patient_rec_stage_26d6a8_idx",
            old_name="patient_inf_stage_1f985e_idx",
        ),
        # Rename the two Meta-declared named indexes
        migrations.RenameIndex(
            model_name="patientrecord",
            new_name="ix_pr_updated_at",
            old_name="ix_pi_updated_at",
        ),
        migrations.RenameIndex(
            model_name="patientrecord",
            new_name="ix_pr_org_updated_at",
            old_name="ix_pi_org_updated_at",
        ),
    ]
