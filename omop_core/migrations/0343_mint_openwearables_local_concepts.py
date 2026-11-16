"""Mint the two HK-Wearable concepts the OpenWearables seed names (#2595).

``seed_openwearables_mappings`` points ``estimated_workout_effort_score`` and
``cardiovascular_age`` at HK-WEAR-OW-WORKOUT-EFFORT and HK-WEAR-OW-CARDIO-AGE.
Both are vendor scores with no LOINC or SNOMED equivalent, so no vocabulary
load supplies them, and until now nothing installed them either: they existed
only where somebody had inserted them by hand (Render staging, 2 Nov 2026, at
the ids below and with no ``source``). Everywhere else the two seed rows
resolved to nothing.

Same shape as 0143: a migration is the one step that runs on every deployment,
and the definitions are duplicated from ``omop_core/concept_fixtures.py``
rather than imported, so the migration stays frozen. ``omop_core/tests.py``
asserts the two copies agree.

Matched on (vocabulary, code), not on the id: HealthKey-minted ids do not agree
across instances. A concept that is already here is kept at its id and name; it
is only given ``source='HealthKey'`` when it has none, which is what marks it
as a local mint for ``copy_reference_data``.
"""
import datetime
import logging

from django.db import migrations
from django.db.models import Max

logger = logging.getLogger(__name__)

LOCAL_CONCEPT_ID_MIN = 2_000_000_000
VOCABULARY_ID = 'HK-Wearable'
CONCEPT_CLASS_ID = 'Clinical Observation'

# The ids continue the HK-Wearable block 0143 opened at 2029606350.
_HK_WEARABLE_CONCEPTS = [
    dict(concept_id=2029606355, concept_name='Workout effort score (vendor-specific)',
         domain_id='Observation', concept_code='HK-WEAR-OW-WORKOUT-EFFORT'),
    dict(concept_id=2029606356, concept_name='Cardiovascular age estimate (vendor-specific)',
         domain_id='Measurement', concept_code='HK-WEAR-OW-CARDIO-AGE'),
]


def _ensure_supporting_rows(apps):
    """Domain / Vocabulary / ConceptClass rows the concepts below reference."""
    Domain = apps.get_model('omop_core', 'Domain')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')

    for domain_id in sorted({row['domain_id'] for row in _HK_WEARABLE_CONCEPTS}):
        Domain.objects.get_or_create(
            domain_id=domain_id,
            defaults={'domain_name': domain_id, 'domain_concept_id': 0},
        )
    Vocabulary.objects.get_or_create(
        vocabulary_id=VOCABULARY_ID,
        defaults={'vocabulary_name': 'HealthKey Wearable Metrics',
                  'vocabulary_reference': '', 'vocabulary_version': '',
                  'vocabulary_concept_id': 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id=CONCEPT_CLASS_ID,
        defaults={'concept_class_name': CONCEPT_CLASS_ID,
                  'concept_class_concept_id': 0},
    )


def mint_openwearables_local_concepts(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    _ensure_supporting_rows(apps)

    for row in _HK_WEARABLE_CONCEPTS:
        held = (
            Concept.objects
            .filter(vocabulary_id=VOCABULARY_ID, concept_code=row['concept_code'])
            .order_by('concept_id')
            .first()
        )
        if held is not None:
            if not held.source:
                Concept.objects.filter(pk=held.pk).update(source='HealthKey')
            continue

        concept_id = row['concept_id']
        if Concept.objects.filter(concept_id=concept_id).exists():
            # Another concept sits at the id. Take the next free local one
            # rather than fail the deploy; the code is what callers resolve by.
            highest = Concept.objects.aggregate(top=Max('concept_id'))['top'] or 0
            concept_id = max(highest, LOCAL_CONCEPT_ID_MIN) + 1
            logger.warning(
                'concept_id %s is taken; minting %s at %s instead.',
                row['concept_id'], row['concept_code'], concept_id,
            )
        Concept.objects.create(
            concept_id=concept_id,
            concept_name=row['concept_name'],
            domain_id=row['domain_id'],
            vocabulary_id=VOCABULARY_ID,
            concept_class_id=CONCEPT_CLASS_ID,
            standard_concept=None,
            concept_code=row['concept_code'],
            source='HealthKey',
            valid_start_date=datetime.date(1970, 1, 1),
            valid_end_date=datetime.date(2099, 12, 31),
        )


def reverse_noop(apps, schema_editor):
    """Deliberately does nothing.

    Proposed and approved mappings, and any measurement or observation rows an
    approved one wrote, point at these concepts. Deleting them on rollback
    would cascade into that data or fail on the foreign key, and a concept left
    behind is a valid row either way. A ``source`` this migration filled in is
    left filled in: it was the correct value.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0342_seed_chronic_condition_override"),
    ]

    operations = [
        migrations.RunPython(mint_openwearables_local_concepts, reverse_noop),
    ]
