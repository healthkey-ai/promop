"""Fix disease_activity, planned_therapies and tumor_grade field-concept mappings.

disease_activity: concept 2100000002 (HealthKey-minted, SNOMED 246456005 absent
from Athena) → 1617595 (LOINC 97509-4 "Cancer disease progression", standard,
Observation). Choices replaced with FLF patient-facing set carrying standard
answer codes. value_kind set to 'concept' so projection writes
value_as_concept_id.

planned_therapies: concept 4254051 (SNOMED 410942007, non-standard, Drug domain)
→ 4226140 (SNOMED 405613005 "Planned procedure", standard, Observation).
status promoted from 'proposed' to 'approved'.

tumor_grade: concept 4160340 is correct (SNOMED 371469007, Observation domain)
but omop_table was 'Measurement' — corrected to 'Observation'.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# -- disease_activity choices: FLF patient-facing set --
# (display, vocabulary_id, code, is_primary)  — None means uncoded.
_DISEASE_ACTIVITY_CHOICES = [
    ('Watch and wait', 'SNOMED', '373818007', True),
    ('On treatment', None, None, None),
    ('Finished treatment and in remission', 'SNOMED', '103338009', True),
    ('Came back after treatment', 'SNOMED', '263855007', True),
    ('Not sure', 'LOINC', 'LA12693-0', True),
]

_OLD_DISEASE_ACTIVITY_DISPLAYS = [
    'Active', 'Inactive', 'Remission', 'Relapsed', 'Refractory',
]


def forwards(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')

    # --- 1a. Fix disease_activity mapping ---
    concept_da = Concept.objects.filter(
        vocabulary_id='LOINC', concept_code='97509-4',
    ).first()
    if concept_da is None:
        logger.warning(
            'Concept LOINC:97509-4 is missing; disease_activity mapping not updated.'
        )
    else:
        FieldConceptMapping.objects.update_or_create(
            field_name='disease_activity',
            defaults={
                'concept': concept_da,
                'vocabulary_id': 'LOINC',
                'concept_code': '97509-4',
                'value_kind': 'concept',
                'omop_table': 'Observation',
                'status': 'approved',
                'notes': (
                    'LOINC 97509-4 "Cancer disease progression" (standard, '
                    'Observation). Fixed by migration 0311: was HealthKey-minted '
                    '2100000002 / SNOMED 246456005 (absent from Athena).'
                ),
            },
        )

    # --- 1b. Replace disease_activity choices ---
    # Delete old choices (cascade deletes their FieldChoiceCode rows).
    FieldChoice.objects.filter(
        field_name='disease_activity',
        display__in=_OLD_DISEASE_ACTIVITY_DISPLAYS,
    ).delete()

    for sort_order, (display, vocab, code, is_primary) in enumerate(_DISEASE_ACTIVITY_CHOICES):
        choice, _ = FieldChoice.objects.get_or_create(
            field_name='disease_activity',
            display=display,
            defaults={'sort_order': sort_order},
        )
        if vocab is not None and code is not None:
            FieldChoiceCode.objects.get_or_create(
                choice=choice,
                vocabulary_id=vocab,
                code=code,
                defaults={
                    'display': display,
                    'is_primary': is_primary,
                },
            )

    # --- 1c. Fix planned_therapies mapping ---
    concept_pt = Concept.objects.filter(
        vocabulary_id='SNOMED', concept_code='405613005',
    ).first()
    if concept_pt is None:
        logger.warning(
            'Concept SNOMED:405613005 is missing; planned_therapies mapping not updated.'
        )
    else:
        FieldConceptMapping.objects.update_or_create(
            field_name='planned_therapies',
            defaults={
                'concept': concept_pt,
                'vocabulary_id': 'SNOMED',
                'concept_code': '405613005',
                'omop_table': 'Observation',
                'status': 'approved',
                'notes': (
                    'SNOMED 405613005 "Planned procedure" (standard, Observation). '
                    'Fixed by migration 0311: was 4254051 / SNOMED 410942007 '
                    '(non-standard, Drug domain), status was proposed.'
                ),
            },
        )

    # --- 1d. Fix tumor_grade mapping ---
    FieldConceptMapping.objects.filter(field_name='tumor_grade').update(
        omop_table='Observation',
    )


def reverse(apps, schema_editor):
    """The old concepts were broken; there is nothing safe to restore."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0311_alter_fieldconceptmapping_value_kind'),
    ]
    operations = [
        migrations.RunPython(forwards, reverse),
    ]
