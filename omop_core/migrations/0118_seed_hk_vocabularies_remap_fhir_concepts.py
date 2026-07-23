"""Data migration: seed HK-Regimen / HK-Drug quarantine vocabularies and
remap locally-minted concepts out of the governed HemOnc namespace.

Context (issue #236, ADR 0001): promop is the governed source of coded
vocabulary data; consumers will mirror the concept table.  Rows minted by the
FHIR upload path used to be stamped vocabulary_id='HemOnc' +
standard_concept='S' with a synthetic 'FHIR-*' concept_code, making them
indistinguishable from genuine HemOnc regimens.  This migration:

  1. Seeds the local quarantine vocabularies 'HK-Regimen' and 'HK-Drug'
     (HealthKey-authored, source='HealthKey').
  2. Moves every 'FHIR-*' concept out of HemOnc into 'HK-Regimen', clears its
     standard_concept flag, rewrites its concept_code to the 'hkr:<slug>'
     namespace form, and stamps source='HealthKey'.  concept_ids are left
     untouched so existing FK references (DrugExposure, Episode, ...) stay
     valid.
  3. Stamps source='HealthKey' on rows in the pre-existing local namespaces
     ('HK-Labs', 'FHIR') so consumers can filter local rows on one column.

Migrations must be self-contained (D7): the slug helper below is duplicated
from omop_core.services.regimen_resolution rather than imported.
"""
import re
import unicodedata

from django.db import migrations


def _hkr_slug(name):
    """Normalize a regimen name to a stable concept_code slug: 'hkr:<slug>'."""
    s = unicodedata.normalize('NFKD', (name or '').lower())
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return f'hkr:{s}'[:50]


def seed_and_remediate(apps, schema_editor):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')
    Concept = apps.get_model('omop_core', 'Concept')

    Domain.objects.get_or_create(
        domain_id='Drug',
        defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Regimen',
        defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
    )

    Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Regimen',
        defaults={
            'vocabulary_name': 'HealthKey local regimen quarantine',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )
    Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Drug',
        defaults={
            'vocabulary_name': 'HealthKey local drug quarantine',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )

    # 2. Remap fake HemOnc concepts into the HK-Regimen quarantine namespace.
    fhir_minted = Concept.objects.filter(
        vocabulary_id='HemOnc', concept_code__startswith='FHIR-',
    )
    for concept in fhir_minted.iterator():
        concept.vocabulary_id = 'HK-Regimen'
        concept.standard_concept = None
        concept.source = 'HealthKey'
        concept.concept_code = _hkr_slug(concept.concept_name)
        concept.save(update_fields=[
            'vocabulary_id', 'standard_concept', 'source', 'concept_code',
        ])

    # 3. Stamp provenance on the other local namespaces.
    Concept.objects.filter(
        vocabulary_id__in=('HK-Labs', 'HK-Regimen', 'HK-Drug', 'FHIR'),
        source__isnull=True,
    ).update(source='HealthKey')


def reverse_noop(apps, schema_editor):
    # The concept_code rewrite ('FHIR-X' -> 'hkr:x') loses the original code,
    # and re-claiming rows under HemOnc would re-introduce the namespace
    # pollution this migration removes.  Rolling back leaves rows in the
    # HK-Regimen namespace; manual correction is required to undo fully.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0117_concept_source_patientrecord_therapy_ids_provenance_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_and_remediate, reverse_noop),
    ]
