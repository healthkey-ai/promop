"""Relabel source_vocabulary_id='ICD10' → 'ICD10CM' in the code mapping table.

No ICD-10 (WHO) vocabulary is loaded from Athena — only ICD10CM (100,273
concepts).  All SCCM rows with source_vocabulary_id='ICD10' are actually
ICD-10-CM codes (verified: every code exists in the Concept table under
ICD10CM).  HT-One sends ICD-10-CM codes labeled as 'ICD10', and migration
0253 loaded Athena mappings under 'ICD10'.

After the tab split (PR #1632), the ICD-10-CM tab shows only the 9,316
Athena rows while the 12,994 HT-One rows are stranded on an ICD-10 (WHO)
tab for a vocabulary that doesn't exist.

This migration relabels them so the ICD-10-CM tab shows all ~22K rows.
"""
from django.db import migrations


def relabel_icd10_forward(apps, schema_editor):
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id='ICD10',
    ).update(source_vocabulary_id='ICD10CM')


def relabel_icd10_reverse(apps, schema_editor):
    # The original ICD10/ICD10CM split cannot be reconstructed: all rows are
    # now ICD10CM and there is no marker distinguishing which were originally
    # ICD10.  Rolling back this migration is a no-op; manual correction is
    # required if the split needs to be restored.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0266_loincrelease'),
    ]

    operations = [
        migrations.RunPython(relabel_icd10_forward, relabel_icd10_reverse),
    ]
