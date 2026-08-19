from django.db import migrations, models


# The patient_info compatibility view selects three of these columns, and
# Postgres refuses to alter the type of a column a view depends on. So the view
# is dropped before the ALTERs and rebuilt with the exact column list, owner and
# grants it had, following 0139. Restoration failures warn instead of aborting
# the deploy, since the view exists only for consumers outside this codebase.
#
# free_light_chain_ratio is not added to the view on purpose. Widening an
# outward facing compatibility surface is a separate decision.
#
# The two RunSQL steps hand state over through a temp table, which is safe
# because a Django migration runs in one transaction on Postgres.

_VIEW_COLUMN_EXPR = """
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
"""

# Not every view column is backed by a table column, 0138 replaced `status` with
# a typed NULL literal. Reproduce those as literals, not bare identifiers.
DROP_VIEW_SQL = f"""
DO $$
DECLARE
    v_col_list text;
BEGIN
    IF to_regclass('public.patient_info') IS NULL THEN
        RAISE NOTICE 'patient_info view not found; nothing to drop';
        RETURN;
    END IF;

    CREATE TEMP TABLE _patient_info_rebuild (
        col_list   text,
        view_owner text,
        view_acl   aclitem[]
    ) ON COMMIT DROP;

    SELECT string_agg(v.expr, ', ' ORDER BY v.attnum)
      INTO v_col_list
      FROM ({_VIEW_COLUMN_EXPR}) AS v;

    INSERT INTO _patient_info_rebuild (col_list, view_owner, view_acl)
    SELECT v_col_list, pg_get_userbyid(c.relowner), c.relacl
      FROM pg_class c
     WHERE c.oid = to_regclass('public.patient_info');

    EXECUTE 'DROP VIEW public.patient_info';
END
$$;
"""

CREATE_VIEW_SQL = """
DO $$
DECLARE
    col_list   text;
    view_acl   aclitem[];
    view_owner text;
    stmt       text;
BEGIN
    IF to_regclass('pg_temp._patient_info_rebuild') IS NULL THEN
        RAISE NOTICE 'no captured patient_info definition; nothing to rebuild';
        RETURN;
    END IF;

    SELECT r.col_list, r.view_owner, r.view_acl
      INTO col_list, view_owner, view_acl
      FROM _patient_info_rebuild r
     LIMIT 1;

    IF col_list IS NULL THEN
        RAISE NOTICE 'captured patient_info column list is empty; nothing to rebuild';
        RETURN;
    END IF;

    EXECUTE format(
        'CREATE VIEW public.patient_info AS SELECT %s FROM public.patient_record',
        col_list);

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'patient_info_readonly') THEN
        EXECUTE '
            CREATE TRIGGER patient_info_readonly_trigger
            INSTEAD OF INSERT OR UPDATE OR DELETE ON public.patient_info
            FOR EACH ROW EXECUTE FUNCTION patient_info_readonly()';
    ELSE
        RAISE WARNING 'patient_info: patient_info_readonly() missing; view left without its read-only trigger';
    END IF;

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

    dependencies = [
        ('omop_core', '0150_nullable_patientrecord_dated_assertions'),
    ]

    operations = [
        migrations.RunSQL(sql=DROP_VIEW_SQL, reverse_sql=CREATE_VIEW_SQL),
        migrations.AddField(
            model_name='patientrecord',
            name='free_light_chain_ratio',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Serum free light chain ratio (kappa/lambda)', max_digits=12, null=True),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='clonal_plasma_cells',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Clonal plasma cells in bone marrow (%)', max_digits=6, null=True),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='kappa_flc',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Serum free kappa light chains', max_digits=10, null=True),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='lambda_flc',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Serum free lambda light chains', max_digits=10, null=True),
        ),
        migrations.RunSQL(sql=CREATE_VIEW_SQL, reverse_sql=DROP_VIEW_SQL),
    ]
