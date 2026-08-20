from django.db import migrations


# The orphan column is exposed by the `patient_info` compatibility view, so it
# cannot be dropped without rebuilding that view. The view's column list is NOT
# the same in every environment: `0104_rename_patientinfo_to_patientrecord`
# created it as `SELECT * FROM patient_record`, and PostgreSQL expands that star
# into a fixed column list at creation time. Databases built purely from the
# migration chain froze 296 columns; staging and production froze 297, because
# `death_date` had been added to the table out-of-band before 0104 ran there.
#
# Hardcoding either list would silently corrupt the other, so the view is
# rebuilt from its own current columns: every column is carried over in its
# original ordinal position, with `status` replaced by a NULL literal of the
# same type. External consumers of `patient_info` see an identical view --
# including `SELECT status`, which only ever returned NULL anyway.
#
# `DROP VIEW` also discards the view's owner and grants, and this view exists
# solely for consumers outside this codebase -- any GRANT on it is by
# definition out-of-band, the same drift class that produced the 296/297 column
# split. Both are captured before the drop and reapplied after, so a Render
# deploy cannot silently revoke an external consumer's access. Failure to
# restore them warns rather than aborts the deploy: the fallback state (view
# owned by the migrating role) is exactly what an unguarded rebuild would have
# produced anyway.
#
# Every identifier is schema-qualified to match the `table_schema = 'public'`
# filter the column list is discovered with; unqualified DDL would resolve
# through `search_path` and could recreate the view in a different schema.

DROP_STATUS_SQL = """
DO $$
DECLARE
    col_list    text;
    status_type text;
    view_acl    aclitem[];
    view_owner  text;
    stmt        text;
BEGIN
    IF to_regclass('public.patient_info') IS NULL THEN
        RAISE NOTICE 'patient_info view not found; dropping column only';
        EXECUTE 'ALTER TABLE public.patient_record DROP COLUMN IF EXISTS status';
        RETURN;
    END IF;

    -- Exact declared type of the view's `status` column, so the NULL literal
    -- that replaces it keeps the type external clients already see.
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO status_type
      FROM pg_attribute a
     WHERE a.attrelid = to_regclass('public.patient_info')
       AND a.attname = 'status'
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF status_type IS NULL THEN
        -- The view does not expose `status`; rebuilding it would be a no-op
        -- that needlessly churns its ACL. Just drop the column.
        RAISE NOTICE 'patient_info does not expose status; dropping column only';
        EXECUTE 'ALTER TABLE public.patient_record DROP COLUMN IF EXISTS status';
        RETURN;
    END IF;

    SELECT string_agg(
               CASE WHEN column_name = 'status'
                    THEN format('NULL::%s AS status', status_type)
                    ELSE quote_ident(column_name)
               END,
               ', ' ORDER BY ordinal_position)
      INTO col_list
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'patient_info';

    SELECT c.relacl, pg_get_userbyid(c.relowner)
      INTO view_acl, view_owner
      FROM pg_class c
     WHERE c.oid = to_regclass('public.patient_info');

    -- Dropping the view also drops its INSTEAD OF trigger; the
    -- patient_info_readonly() function it calls is left in place.
    EXECUTE 'DROP VIEW public.patient_info';
    EXECUTE 'ALTER TABLE public.patient_record DROP COLUMN IF EXISTS status';
    EXECUTE format(
        'CREATE VIEW public.patient_info AS SELECT %s FROM public.patient_record',
        col_list);
    EXECUTE '
        CREATE TRIGGER patient_info_readonly_trigger
        INSTEAD OF INSERT OR UPDATE OR DELETE ON public.patient_info
        FOR EACH ROW EXECUTE FUNCTION patient_info_readonly()';

    BEGIN
        IF view_owner IS NOT NULL AND view_owner <> current_user THEN
            EXECUTE format('ALTER VIEW public.patient_info OWNER TO %I', view_owner);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'patient_info: could not restore owner %: %', view_owner, SQLERRM;
    END;

    IF view_acl IS NOT NULL THEN
        FOR stmt IN
            SELECT format('GRANT %s ON public.patient_info TO %s%s',
                          a.privilege_type,
                          CASE WHEN a.grantee = 0
                               THEN 'PUBLIC'
                               ELSE quote_ident(pg_get_userbyid(a.grantee))
                          END,
                          CASE WHEN a.is_grantable
                               THEN ' WITH GRANT OPTION'
                               ELSE ''
                          END)
              FROM aclexplode(view_acl) a
        LOOP
            BEGIN
                EXECUTE stmt;
            EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'patient_info: could not restore grant (%): %', stmt, SQLERRM;
            END;
        END LOOP;
    END IF;
END
$$;
"""


# Restores the physical column and re-points the view at it, preserving the
# view's owner and grants exactly as the forward migration does. The column was
# empty in every environment when it was dropped, so no data is lost.
RESTORE_STATUS_SQL = """
DO $$
DECLARE
    col_list   text;
    view_acl   aclitem[];
    view_owner text;
    stmt       text;
BEGIN
    EXECUTE 'ALTER TABLE public.patient_record ADD COLUMN IF NOT EXISTS status TEXT';

    IF to_regclass('public.patient_info') IS NULL THEN
        RAISE NOTICE 'patient_info view not found; restored column only';
        RETURN;
    END IF;

    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
      INTO col_list
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'patient_info';

    SELECT c.relacl, pg_get_userbyid(c.relowner)
      INTO view_acl, view_owner
      FROM pg_class c
     WHERE c.oid = to_regclass('public.patient_info');

    EXECUTE 'DROP VIEW public.patient_info';
    EXECUTE format(
        'CREATE VIEW public.patient_info AS SELECT %s FROM public.patient_record',
        col_list);
    EXECUTE '
        CREATE TRIGGER patient_info_readonly_trigger
        INSTEAD OF INSERT OR UPDATE OR DELETE ON public.patient_info
        FOR EACH ROW EXECUTE FUNCTION patient_info_readonly()';

    BEGIN
        IF view_owner IS NOT NULL AND view_owner <> current_user THEN
            EXECUTE format('ALTER VIEW public.patient_info OWNER TO %I', view_owner);
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'patient_info: could not restore owner %: %', view_owner, SQLERRM;
    END;

    IF view_acl IS NOT NULL THEN
        FOR stmt IN
            SELECT format('GRANT %s ON public.patient_info TO %s%s',
                          a.privilege_type,
                          CASE WHEN a.grantee = 0
                               THEN 'PUBLIC'
                               ELSE quote_ident(pg_get_userbyid(a.grantee))
                          END,
                          CASE WHEN a.is_grantable
                               THEN ' WITH GRANT OPTION'
                               ELSE ''
                          END)
              FROM aclexplode(view_acl) a
        LOOP
            BEGIN
                EXECUTE stmt;
            EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'patient_info: could not restore grant (%): %', stmt, SQLERRM;
            END;
        END LOOP;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    """
    Drop the orphan ``status`` column from ``patient_record``.

    ``0036_add_cll_and_missing_fields`` created the column with raw SQL
    (``ALTER TABLE patient_info ADD COLUMN IF NOT EXISTS status TEXT``).
    ``0040_remove_status_field`` then removed the field from Django's state
    only, on the mistaken assumption that the column had never been created in
    the database. The column therefore outlived the field -- and survived the
    ``patient_info`` -> ``patient_record`` table rename -- leaving every promop
    database reporting ``EXTRA in DB: ['status']`` in the sync check.

    The column was verified empty (zero non-null values) in local, staging, and
    production before this migration was written.

    This is hand-written rather than generated by ``makemigrations``: the field
    is already absent from Django's state, so there is nothing for the
    autodetector to notice. Only a database operation is required.
    """

    dependencies = [
        ("omop_core", "0137_add_apple_watch_health_metrics"),
    ]

    operations = [
        migrations.RunSQL(sql=DROP_STATUS_SQL, reverse_sql=RESTORE_STATUS_SQL),
    ]
