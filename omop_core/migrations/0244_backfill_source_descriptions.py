"""Backfill unnamed source codes on the code-mapping queue (#1464).

Every tab except ICD-10 showed bare codes: on staging 45k SNOMED, 32k MedDRA,
6k CPT4, 4k LOINC and 4k RxNorm rows had no ``source_code_description``, so a
curator was comparing a destination name against a code. The names exist in
the Athena concept table or, for the licensed vocabularies Athena does not
carry there, in the loaded UMLS atoms.

Self-contained (D7, see 0118): this is a frozen copy of
``omop_core.services.source_descriptions.backfill_source_descriptions`` as of
#1478, with the vocabulary tables inlined, so a later change to the service
cannot alter what this migration did or break ``migrate`` on a fresh database.
Keep the service as the live routine; do not edit this copy.

Only blank descriptions, or ones that merely repeat the code, are written; a
curator's text is never touched. Not reversible: a backfilled description is
indistinguishable from a typed one afterwards, and clearing them all would
discard curator work, so the reverse is a no-op.

``atomic = False``: each pass is idempotent and safe to commit alone, and
``start.sh`` runs ``migrate`` while the previous instance and the worker are
still ingesting into the same rows — one long transaction holding row locks on
93k rows is the shape a deadlock needs.
"""
from django.db import migrations

DESCRIPTION_MAX = 255

ATHENA_ALIASES = [
    ('urn:oid:2.16.840.1.113883.6.96', 'SNOMED'),
    ('ICD10', 'ICD10CM'),
]

UMLS_ROOTS = [
    ('SNOMED', 'SNOMEDCT_US'), ('ICD10CM', 'ICD10CM'), ('ICD10', 'ICD10CM'),
    ('ICD10PCS', 'ICD10PCS'), ('LOINC', 'LNC'), ('RxNorm', 'RXNORM'),
    ('CPT4', 'CPT'), ('HCPCS', 'HCPCS'), ('NDC', 'NDC'), ('CVX', 'CVX'),
    ('ICD9CM', 'ICD9CM'), ('MeSH', 'MSH'), ('NDFRT', 'MED-RT'), ('MedDRA', 'MDR'),
    ('urn:oid:2.16.840.1.113883.6.96', 'SNOMEDCT_US'),
]

_UNNAMED = "(m.source_code_description = '' OR m.source_code_description = m.source_code)"


def _values(pairs):
    return ', '.join(['(%s, %s)'] * len(pairs)), [v for pair in pairs for v in pair]


def backfill(apps, schema_editor):
    mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')._meta.db_table
    concept = apps.get_model('omop_core', 'Concept')._meta.db_table
    umls = apps.get_model('omop_core', 'UmlsSourceCode')._meta.db_table
    from django.db import connections
    alias = schema_editor.connection.alias if schema_editor is not None else 'default'
    with connections[alias].cursor() as cursor:
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(c.concept_name, %s)
            FROM "{concept}" AS c
            WHERE {_UNNAMED}
              AND c.vocabulary_id = m.source_vocabulary_id
              AND c.concept_code = m.source_code
              AND c.concept_name <> ''
        ''', [DESCRIPTION_MAX])
        values, params = _values(ATHENA_ALIASES)
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(c.concept_name, %s)
            FROM (VALUES {values}) AS alias(source_vocabulary_id, athena_vocabulary_id)
            JOIN "{concept}" AS c ON c.vocabulary_id = alias.athena_vocabulary_id
            WHERE {_UNNAMED}
              AND m.source_vocabulary_id = alias.source_vocabulary_id
              AND c.concept_code = m.source_code
              AND c.concept_name <> ''
        ''', [DESCRIPTION_MAX, *params])
        values, params = _values(UMLS_ROOTS)
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(best.name, %s),
                umls_source_name = CASE WHEN m.umls_source_name = '' THEN best.name
                                        ELSE m.umls_source_name END
            FROM (
                SELECT DISTINCT ON (gap.id) gap.id, u.name
                FROM "{mapping}" AS gap
                JOIN (VALUES {values}) AS sab(source_vocabulary_id, root_source)
                  ON sab.source_vocabulary_id = gap.source_vocabulary_id
                JOIN "{umls}" AS u
                  ON u.root_source = sab.root_source AND u.code = gap.source_code
                WHERE (gap.source_code_description = ''
                       OR gap.source_code_description = gap.source_code)
                  AND u.name <> ''
                ORDER BY gap.id, (u.term_type = 'PT') DESC, u.is_preferred DESC,
                         length(u.name) DESC, u.name
            ) AS best
            WHERE m.id = best.id
        ''', [DESCRIPTION_MAX, *params])


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('omop_core', '0243_concept_ix_concept_code_upper'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
