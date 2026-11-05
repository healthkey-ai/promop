"""Mint the local HK-Genomics BCMA marker concept (#5674).

`bcmaExpression` is a trial marker with no exact standard concept in the loaded
vocabulary: the only BCMA rows are an IL2::TNFRSF17 fusion and anti-BCMA
therapy classes, both asserting something different. Aliasing the expression
marker onto either would be the same class of defect as the old origin/
interpretation code mix (#5671).

The marker is minted under ``HK-Genomics`` with the quarantine rules every
local concept follows: ``source='HealthKey'``, ``standard_concept=None`` and a
``concept_id`` in OHDSI's reserved local range. The trial envelope still carries
the canonical ``bcmaExpression`` code until it is recompiled to emit this
feature; the patient-side mapping can use the id now, and the string fallback
stays.

Duplicated in ``omop_core/concept_fixtures.py`` so a test database has it
without running migrations; the drift test in ``omop_core/tests.py`` asserts the
two definitions stay identical.
"""
from datetime import date

from django.db import migrations


_CONCEPTS = (
    (2_100_007_900, 'HK-MARKER:BCMA-EXPRESSION', 'BCMA protein expression'),
)


def seed_bcma_marker(apps, schema_editor):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')
    Concept = apps.get_model('omop_core', 'Concept')

    Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Genomics',
        defaults={
            'vocabulary_name': 'HealthKey genomic marker concepts',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )
    Domain.objects.get_or_create(
        domain_id='Measurement',
        defaults={'domain_name': 'Measurement', 'domain_concept_id': 21},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Lab Test',
        defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0},
    )

    for concept_id, concept_code, concept_name in _CONCEPTS:
        Concept.objects.get_or_create(
            vocabulary_id='HK-Genomics',
            concept_code=concept_code,
            defaults={
                'concept_id': concept_id,
                'concept_name': concept_name,
                'domain_id': 'Measurement',
                'concept_class_id': 'Lab Test',
                'standard_concept': None,
                'source': 'HealthKey',
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
                'invalid_reason': None,
            },
        )


def unseed_bcma_marker(apps, schema_editor):
    """Remove the mint, and the vocabulary if nothing else uses it.

    A Measurement that references the concept protects the delete; that is the
    correct outcome while the mint is in use.
    """
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')

    Concept.objects.filter(
        vocabulary_id='HK-Genomics',
        concept_code__in=[code for _cid, code, _name in _CONCEPTS],
    ).delete()
    if not Concept.objects.filter(vocabulary_id='HK-Genomics').exists():
        Vocabulary.objects.filter(vocabulary_id='HK-Genomics').delete()


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0326_explicit_negative_answers')]

    operations = [
        migrations.RunPython(seed_bcma_marker, unseed_bcma_marker),
    ]
