"""Fix stale FK constraint on patient_user.identity_id.

Migration 0004 renamed the column user_id → identity_id but did not update
the foreign key constraint.  On databases that ran 0002 before the
AUTH_USER_MODEL switch, the constraint still references auth_user instead of
the identity table.  This migration drops the stale constraint (if present)
and adds the correct one.

Fresh databases (created after the model change) already have the correct
constraint; the operations are conditional and safe to run either way.
"""
from django.db import migrations


def fix_fk_constraint(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        # Check if the stale constraint exists
        cursor.execute("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_schema = current_schema()
              AND table_name   = 'patient_user'
              AND constraint_type = 'FOREIGN KEY'
              AND constraint_name LIKE '%%fk_auth_user%%'
        """)
        stale = cursor.fetchall()
        for (name,) in stale:
            cursor.execute(f'ALTER TABLE patient_user DROP CONSTRAINT "{name}"')

        # Check if a correct FK to the identity table already exists
        cursor.execute("""
            SELECT 1
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.table_schema = current_schema()
              AND tc.table_name   = 'patient_user'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND ccu.table_name = 'identity'
              AND ccu.column_name = 'id'
        """)
        if not cursor.fetchone():
            cursor.execute("""
                ALTER TABLE patient_user
                ADD CONSTRAINT patient_user_identity_id_fk_identity
                FOREIGN KEY (identity_id) REFERENCES identity (id)
                DEFERRABLE INITIALLY DEFERRED
            """)


def reverse_noop(apps, schema_editor):
    # Cannot safely restore the stale constraint; leave the correct one in place.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("patient_portal", "0014_auditevent_chain_hash"),
    ]

    operations = [
        migrations.RunPython(fix_fk_constraint, reverse_noop),
    ]
