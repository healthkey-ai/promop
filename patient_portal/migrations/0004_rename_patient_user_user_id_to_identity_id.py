"""Rename patient_user.user_id → identity_id.

When AUTH_USER_MODEL was switched from auth.User to Identity (Phase C),
the PatientUser.identity OneToOneField now resolves to identity_id, but
databases that ran migration 0002 while the model still pointed at auth.User
have the column named user_id.  This migration renames the column in the DB;
Django state already reflects identity_id so no state operation is needed.

The rename is conditional: fresh databases created after the AUTH_USER_MODEL
switch already have identity_id and must not be touched.
"""
from django.db import migrations


def rename_user_id_to_identity_id(apps, schema_editor):
    """Rename user_id → identity_id only if user_id still exists."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'patient_user' AND column_name = 'user_id'
        """)
        if cursor.fetchone():
            cursor.execute(
                'ALTER TABLE patient_user RENAME COLUMN user_id TO identity_id'
            )


def rename_identity_id_to_user_id(apps, schema_editor):
    """Reverse: rename identity_id → user_id only if identity_id exists."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'patient_user' AND column_name = 'identity_id'
        """)
        if cursor.fetchone():
            cursor.execute(
                'ALTER TABLE patient_user RENAME COLUMN identity_id TO user_id'
            )


class Migration(migrations.Migration):

    dependencies = [
        ("patient_portal", "0003_identity_uid_username_field"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    rename_user_id_to_identity_id,
                    rename_identity_id_to_user_id,
                ),
            ],
            state_operations=[],  # Django state already has identity_id
        ),
    ]
