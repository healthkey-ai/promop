"""Seed local source concepts for clinically distinct, unmapped fields (#785).

These meanings have no suitable standard OMOP question concept.  They must not
be minted in a licensed vocabulary merely to make them selectable: local source
concepts live in an HK-* vocabulary, have no standard flag, and use OHDSI's
reserved local concept-id range.
"""
from datetime import date

from django.db import migrations


_CONCEPTS = (
    (2_100_007_850, 'hko:ecog-assessment-date', 'ECOG performance status assessment date'),
    (2_100_007_851, 'hko:bcl2-inhibitor-refractory', 'BCL-2 inhibitor refractory disease status'),
    (2_100_007_852, 'hko:btk-inhibitor-refractory', 'BTK inhibitor refractory disease status'),
)


def seed_source_concepts(apps, schema_editor):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')
    Concept = apps.get_model('omop_core', 'Concept')

    Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Observation',
        defaults={
            'vocabulary_name': 'HealthKey local observation source concepts',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )
    Domain.objects.get_or_create(
        domain_id='Observation',
        defaults={'domain_name': 'Observation', 'domain_concept_id': 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Clinical Observation',
        defaults={
            'concept_class_name': 'Clinical Observation',
            'concept_class_concept_id': 0,
        },
    )

    for concept_id, concept_code, concept_name in _CONCEPTS:
        Concept.objects.get_or_create(
            vocabulary_id='HK-Observation',
            concept_code=concept_code,
            defaults={
                'concept_id': concept_id,
                'concept_name': concept_name,
                'domain_id': 'Observation',
                'concept_class_id': 'Clinical Observation',
                'standard_concept': None,
                'source': 'HealthKey',
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
                'invalid_reason': None,
            },
        )


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0179_merge_20260828_0945')]

    operations = [migrations.RunPython(seed_source_concepts, migrations.RunPython.noop)]
