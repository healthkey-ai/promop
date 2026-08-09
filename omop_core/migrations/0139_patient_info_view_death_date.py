from django.db import migrations


# `PatientRecord.death_date` is fully covered by the migration chain (0110), so
# the *column* exists everywhere. The `patient_info` compatibility view is the
# gap: 0104 created it as `SELECT * FROM patient_record` and PostgreSQL freezes
# that star into a fixed column list at creation time. 0110 runs after 0104, so
# a chain-built view can never contain `death_date`. Staging has it only because
# the view was rebuilt out-of-band at some point after 0110 -- which is why the
# view is 297 columns there and 296 in CI.
#
# This converges them. `death_date` is inserted immediately after `race`, which
# is where it sits in staging (ordinal 281, before `mrd_status`); the rest of
# the ordering already matches, so this reproduces staging's layout exactly
# rather than appending and handing `SELECT *` consumers a different order.
#
# Idempotent: databases whose view already exposes `death_date` are left
# untouched, so their ACL is not churned for a no-op rebuild. As in 0138, the
# view's owner and grants are captured before the drop and reapplied after --
# `DROP VIEW` discards both, and this view exists solely for consumers outside
# this codebase. Restoration failures warn rather than abort the deploy.

ADD_DEATH_DATE_SQL = """
DO $$
DECLARE
    col_list   text;
    has_anchor boolean;
    view_acl   aclitem[];
    view_owner text;
    stmt       text;
BEGIN
    IF to_regclass('public.patient_info') IS NULL THEN
        RAISE NOTICE 'patient_info view not found; nothing to do';
        RETURN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'patient_record'
                      AND column_name = 'death_date') THEN
        RAISE NOTICE 'patient_record.death_date missing; nothing to do';
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'patient_info'
                  AND column_name = 'death_date') THEN
        RAISE NOTICE 'patient_info already exposes death_date; leaving view untouched';
        RETURN;
    END IF;

    SELECT bool_or(name = 'race')
      INTO has_anchor
      FROM (
        SELECT a.attname AS name,
               a.attnum  AS attnum,
               CASE WHEN EXISTS (
                        SELECT 1 FROM pg_attribute t
                         WHERE t.attrelid = to_regclass('public.patient_record')
                           AND t.attname = a.attname
                           AND t.attnum > 0
                           AND NOT t.attisdropped)
                    THEN quote_ident(a.attname)
                    ELSE format('NULL::%s AS %I',
                                format_type(a.atttypid, a.atttypmod), a.attname)
               END AS expr
          FROM pg_attribute a
         WHERE a.attrelid = to_regclass('public.patient_info')
           AND a.attnum > 0
           AND NOT a.attisdropped
     ) AS v;

    -- Not every view column is backed by a table column: 0138 replaced
    -- `status` with a typed NULL literal. Reproduce any such column as a
    -- literal rather than emitting a bare identifier that does not resolve.
    SELECT string_agg(
               CASE WHEN v.name = 'race'
                    THEN v.expr || ', death_date'
                    ELSE v.expr
               END,
               ', ' ORDER BY v.attnum)
      INTO col_list
      FROM (
        SELECT a.attname AS name,
               a.attnum  AS attnum,
               CASE WHEN EXISTS (
                        SELECT 1 FROM pg_attribute t
                         WHERE t.attrelid = to_regclass('public.patient_record')
                           AND t.attname = a.attname
                           AND t.attnum > 0
                           AND NOT t.attisdropped)
                    THEN quote_ident(a.attname)
                    ELSE format('NULL::%s AS %I',
                                format_type(a.atttypid, a.atttypmod), a.attname)
               END AS expr
          FROM pg_attribute a
         WHERE a.attrelid = to_regclass('public.patient_info')
           AND a.attnum > 0
           AND NOT a.attisdropped
     ) AS v;

    IF NOT has_anchor THEN
        -- No `race` column to anchor against; append rather than skip.
        col_list := col_list || ', death_date';
    END IF;

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


# Rebuilds the view without `death_date`, restoring the pre-migration shape.
# Databases that never had it in the view are left untouched.
REMOVE_DEATH_DATE_SQL = """
DO $$
DECLARE
    col_list   text;
    view_acl   aclitem[];
    view_owner text;
    stmt       text;
BEGIN
    IF to_regclass('public.patient_info') IS NULL THEN
        RAISE NOTICE 'patient_info view not found; nothing to do';
        RETURN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'patient_info'
                      AND column_name = 'death_date') THEN
        RAISE NOTICE 'patient_info does not expose death_date; leaving view untouched';
        RETURN;
    END IF;

    SELECT string_agg(v.expr, ', ' ORDER BY v.attnum)
      INTO col_list
      FROM (
        SELECT a.attname AS name,
               a.attnum  AS attnum,
               CASE WHEN EXISTS (
                        SELECT 1 FROM pg_attribute t
                         WHERE t.attrelid = to_regclass('public.patient_record')
                           AND t.attname = a.attname
                           AND t.attnum > 0
                           AND NOT t.attisdropped)
                    THEN quote_ident(a.attname)
                    ELSE format('NULL::%s AS %I',
                                format_type(a.atttypid, a.atttypmod), a.attname)
               END AS expr
          FROM pg_attribute a
         WHERE a.attrelid = to_regclass('public.patient_info')
           AND a.attnum > 0
           AND NOT a.attisdropped
     ) AS v
     WHERE v.name <> 'death_date';

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
    Expose ``death_date`` through the ``patient_info`` compatibility view.

    The column itself has been present since ``0110_patientrecord_death_date``;
    only the view was missing it, because ``0104`` froze the view's column list
    before ``0110`` existed. Staging picked the column up through an
    out-of-band view rebuild, leaving it at 297 columns against CI's 296. This
    closes that gap so ``SELECT death_date FROM patient_info`` behaves the same
    everywhere.

    No Django state changes -- the view is not modelled -- so this is a
    database-only operation.
    """

    dependencies = [
        ("omop_core", "0138_drop_orphan_patient_record_status"),
    ]

    operations = [
        migrations.RunSQL(sql=ADD_DEATH_DATE_SQL, reverse_sql=REMOVE_DEATH_DATE_SQL),
    ]
