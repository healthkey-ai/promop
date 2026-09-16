"""Rename and enable the PatientRecord-first cytogenetic marker field."""

import logging

from django.db import migrations


logger = logging.getLogger(__name__)
LEGACY_FIELD = 'cytogenic_markers'
FIELD = 'cytogenetic_markers'

# These are canonical local result values, not LOINC test identifiers. No
# standard equivalent has been verified for this multi-marker summary.
CHOICES = (
    'del17p', 't(4;14)', 't(11;14)', 't(14;16)', '1q_gain', '1q_amp',
    'hyperdiploidy', 'del13q', 'MYC rearrangement',
)
# Incorrect associations shipped in the unmerged version of this migration.
# Remove only those exact associations if a development DB already has them.
INVALID_CHOICE_CODES = (
    ('del17p', '72838-3'), ('t(4;14)', '72842-5'),
    ('t(14;16)', '81250-3'), ('1q_gain', '81249-5'), ('1q_amp', '81249-5'),
    ('hyperdiploidy', '81248-7'), ('del13q', '72840-9'),
)


def rename_pending_edits(apps, schema_editor, old_name=LEGACY_FIELD, new_name=FIELD):
    using = schema_editor.connection.alias if schema_editor else 'default'
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    records = PatientRecord.objects.using(using).filter(
        user_edited_fields__contains=[old_name],
    ).values_list('pk', 'user_edited_fields')
    for pk, fields in records.iterator():
        renamed = list(dict.fromkeys(new_name if name == old_name else name for name in fields))
        PatientRecord.objects.using(using).filter(pk=pk).update(user_edited_fields=renamed)


def migrate_and_seed(apps, schema_editor):
    rename_pending_edits(apps, schema_editor)
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    FieldSynonym = apps.get_model('omop_core', 'FieldSynonym')
    Concept = apps.get_model('omop_core', 'Concept')

    using = schema_editor.connection.alias if schema_editor else 'default'

    for model in (FieldChoice, FieldFormula, FieldSynonym):
        model.objects.using(using).filter(field_name=LEGACY_FIELD).update(field_name=FIELD)

    FieldConceptMapping.objects.using(using).filter(
        field_name=LEGACY_FIELD,
        vocabulary_id='MeSH',
        concept_code='D002869',
    ).delete()
    FieldConceptMapping.objects.using(using).filter(field_name=LEGACY_FIELD).update(field_name=FIELD)

    for sort_order, display in enumerate(CHOICES):
        FieldChoice.objects.using(using).get_or_create(
            field_name=FIELD, display=display, defaults={'sort_order': sort_order},
        )
    for display, code in INVALID_CHOICE_CODES:
        FieldChoiceCode.objects.using(using).filter(
            choice__field_name=FIELD, choice__display=display,
            vocabulary_id='LOINC', code=code,
        ).delete()

    # 69548-6 is a present/absent genetic variant assessment, not a list of
    # abnormalities. Keep this legacy summary explicitly unmapped and separate
    # from genetic_mutations.status, whose domain is reconciled by #1204.
    zero = Concept.objects.using(using).filter(pk=0).first()
    mapping_defaults = {
        'concept': zero,
        'vocabulary_id': '',
        'concept_code': '',
        'omop_table': 'observation',
        'source_value': 'mm-cytogenetic-markers',
        'value_kind': 'string',
        'multiple': True,
        'status': 'approved' if zero else 'proposed',
        'notes': (
            'Canonical cytogenetic marker summary; no verified standard equivalent. '
            'OMOP concept 0 with local source mm-cytogenetic-markers. '
            'Do not map to LOINC 69548-6 (finding status; #1204).'
        ),
    }
    mapping, created = FieldConceptMapping.objects.using(using).get_or_create(
        field_name=FIELD, defaults=mapping_defaults,
    )
    if not created and (
        mapping.vocabulary_id == 'LOINC' and mapping.concept_code == '69548-6'
        and mapping.source_value == 'mm-cytogenetic-markers'
    ):
        FieldConceptMapping.objects.using(using).filter(pk=mapping.pk).update(**mapping_defaults)
    if zero is None:
        logger.warning('OMOP concept 0 missing; cytogenetic summary remains direct-only.')

    # Repair facts written by the old development seed without touching the
    # separate, correctly coded finding-status rows introduced by #1204.
    if zero is not None:
        Observation = apps.get_model('omop_core', 'Observation')
        Observation.objects.using(using).filter(
            observation_source_value='mm-cytogenetic-markers',
            observation_concept__vocabulary_id='LOINC',
            observation_concept__concept_code='69548-6',
        ).update(observation_concept=zero)


def reverse_metadata(apps, schema_editor):
    rename_pending_edits(apps, schema_editor, FIELD, LEGACY_FIELD)
    using = schema_editor.connection.alias
    for model_name in ('FieldChoice', 'FieldFormula', 'FieldSynonym', 'FieldConceptMapping'):
        model = apps.get_model('omop_core', model_name)
        model.objects.using(using).filter(field_name=FIELD).update(field_name=LEGACY_FIELD)


def _rebuild_view(old_output, new_output, table_column):
    return f"""
DO $$
DECLARE
    col_list text;
    view_acl aclitem[];
    view_owner text;
    stmt text;
BEGIN
    IF to_regclass('public.patient_info') IS NULL THEN
        RETURN;
    END IF;

    SELECT string_agg(
               CASE
                   WHEN v.attname = '{old_output}'
                       THEN format('%I AS %I', '{table_column}', '{new_output}')
                   WHEN EXISTS (
                       SELECT 1 FROM pg_attribute t
                        WHERE t.attrelid = to_regclass('public.patient_record')
                          AND t.attname = v.attname
                          AND t.attnum > 0 AND NOT t.attisdropped
                   ) THEN quote_ident(v.attname)
                   ELSE format('NULL::%s AS %I',
                               format_type(v.atttypid, v.atttypmod), v.attname)
               END,
               ', ' ORDER BY v.attnum)
      INTO col_list
      FROM pg_attribute v
     WHERE v.attrelid = to_regclass('public.patient_info')
       AND v.attnum > 0 AND NOT v.attisdropped;

    SELECT c.relacl, pg_get_userbyid(c.relowner)
      INTO view_acl, view_owner
      FROM pg_class c WHERE c.oid = to_regclass('public.patient_info');

    EXECUTE 'DROP VIEW public.patient_info';
    EXECUTE format(
        'CREATE VIEW public.patient_info AS SELECT %s FROM public.patient_record',
        col_list);
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'patient_info_readonly') THEN
        EXECUTE 'CREATE TRIGGER patient_info_readonly_trigger '
                'INSTEAD OF INSERT OR UPDATE OR DELETE ON public.patient_info '
                'FOR EACH ROW EXECUTE FUNCTION patient_info_readonly()';
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
                          CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                               ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
                          CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END)
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


FORWARD_VIEW_SQL = _rebuild_view(LEGACY_FIELD, FIELD, FIELD)
# Reverse SQL runs before Django reverses RenameField, so the table column is
# still correctly spelled while the compatibility view is changed back.
REVERSE_VIEW_SQL = _rebuild_view(FIELD, LEGACY_FIELD, FIELD)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0221_seed_treatment_editor_catalogs')]

    operations = [
        migrations.RenameField(
            model_name='patientrecord', old_name=LEGACY_FIELD, new_name=FIELD,
        ),
        migrations.RunPython(migrate_and_seed, reverse_metadata),
        migrations.RunSQL(sql=FORWARD_VIEW_SQL, reverse_sql=REVERSE_VIEW_SQL),
    ]
