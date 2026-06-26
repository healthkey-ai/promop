"""Add uid field as USERNAME_FIELD, remove unique=True from sub.

Three steps:
1. Add uid as nullable (no unique yet), remove unique from sub
2. Backfill uid = issuer:sub for all existing rows
3. Make uid non-nullable + unique

PRODUCTION SAFETY
-----------------
On production the identity table was created by 0002_patient_models's bridge SQL,
which does not include a standalone unique index on sub (only the composite
uq_identity_issuer_sub constraint added to the 0002 bridge).  Consequently
Django's AlterField(sub, unique=False) schema editor helper
(_delete_composed_index) cannot find a single-column unique constraint and
raises ValueError: "Found wrong number (0) of constraints for identity(sub)".

To handle all three DB states idempotently:
  - Production (bridge path): sub has no standalone unique index → DROP is skipped.
  - Fresh DB (0001_initial path): sub has a field-level unique index → DROP runs.
  - Dev (already applied): Django skips the whole migration.

The AddField(uid) and the final AlterField(uid, unique=True) are straightforward
because uid did not exist before 0003 and are unaffected by the bridge path.
"""
from django.db import migrations, models


def backfill_uid(apps, schema_editor):
    Identity = apps.get_model("patient_portal", "Identity")
    for identity in Identity.objects.all():
        identity.uid = f"{identity.issuer}:{identity.sub}"
        identity.save(update_fields=["uid"])


class Migration(migrations.Migration):

    dependencies = [
        ("patient_portal", "0002_patient_models"),
    ]

    operations = [
        # Step 1a: add uid as nullable (straightforward — column is new on every path)
        migrations.AddField(
            model_name="identity",
            name="uid",
            field=models.CharField(max_length=512, null=True, editable=False),
        ),

        # Step 1b: drop the standalone unique index on sub — but only if it exists.
        #
        # Fresh-DB path   : 0001_initial CreateModel created identity_sub_<hash>_uniq
        #                   (or identity_sub_key, depending on Django version).
        #                   The DO block finds it and drops it.
        # Production path : 0002 bridge SQL never added a standalone unique on sub.
        #                   The DO block finds nothing and exits silently.
        # Dev path        : migration already applied; this block never runs.
        #
        # We use SeparateDatabaseAndState so Django's ORM state is updated via the
        # normal AlterField while the DB-side uses a safe conditional DROP.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="identity",
                    name="sub",
                    field=models.CharField(max_length=255),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
DO $$
DECLARE
    _conname text;
BEGIN
    -- Find any single-column unique constraint (or unique index) on identity.sub.
    -- This covers both the implicit unique index Django creates for unique=True fields
    -- and any manually created UNIQUE constraint on that column alone.
    SELECT c.conname INTO _conname
    FROM pg_constraint c
    JOIN pg_class     t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE t.relname  = 'identity'
      AND n.nspname  = current_schema()
      AND c.contype  = 'u'
      AND array_length(c.conkey, 1) = 1
      AND c.conkey[1] = (
          SELECT a.attnum
          FROM pg_attribute a
          WHERE a.attrelid = t.oid
            AND a.attname  = 'sub'
      )
    LIMIT 1;

    IF _conname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE identity DROP CONSTRAINT %I', _conname);
    END IF;
END $$;
""",
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),

        # Step 2: backfill uid for any pre-existing rows
        migrations.RunPython(backfill_uid, migrations.RunPython.noop),

        # Step 3: make uid non-nullable + unique
        migrations.AlterField(
            model_name="identity",
            name="uid",
            field=models.CharField(max_length=512, unique=True, editable=False),
        ),
    ]
