"""Backfill empty source descriptions on the code-mapping queue (#1464).

Every tab except ICD-10 showed bare codes: on staging 45k SNOMED, 32k MedDRA,
6k CPT4, 4k LOINC and 4k RxNorm rows had no ``source_code_description``, so a
curator was comparing a destination name against a code. The names exist in
the Athena concept table or, for the licensed vocabularies Athena does not
carry, in the loaded UMLS atoms.

Runs the shared service on historical models. Only empty descriptions are
written; a curator's text is never touched. Not reversible: a backfilled
description is indistinguishable from a typed one afterwards, and clearing
them all would discard curator work, so the reverse is a no-op.
"""
from django.db import migrations

from omop_core.services.source_descriptions import backfill_source_descriptions


def backfill(apps, schema_editor):
    backfill_source_descriptions(
        apps.get_model('omop_core', 'SourceCodeConceptMapping'),
        apps.get_model('omop_core', 'Concept'),
        apps.get_model('omop_core', 'UmlsSourceCode'),
        using=schema_editor.connection.alias if schema_editor is not None else 'default',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0243_concept_ix_concept_code_upper'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
