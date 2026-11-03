"""FieldConceptMapping + FieldChoice rows for the three B-symptom fields.

Each symptom gets an approved observation mapping (SNOMED concept) and
Yes / No / Unknown choices, matching the existing bulky_disease pattern.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# (field_name, snomed_code, athena_concept_id, display_name, notes)
_B_SYMPTOM_MAPPINGS = [
    ('b_symptom_fever', '386661006', 4158517, 'Fever',
     'B symptom: Fever (SNOMED 386661006)'),
    ('b_symptom_night_sweats', '42984000', 4196900, 'Night sweats',
     'B symptom: Night sweats (SNOMED 42984000)'),
    ('b_symptom_weight_loss', '262285001', 4177808, 'Loss of weight',
     'B symptom: Weight loss (SNOMED 262285001)'),
]

_CHOICES = ['Yes', 'No', 'Unknown']


def create_mappings(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')

    for field_name, snomed_code, concept_id, display, notes in _B_SYMPTOM_MAPPINGS:
        concept = Concept.objects.filter(
            concept_id=concept_id, standard_concept='S',
        ).first()
        if concept is None:
            concept = Concept.objects.filter(
                vocabulary_id='SNOMED', concept_code=snomed_code,
                standard_concept='S',
            ).first()
        if concept is None:
            logger.warning(
                'Concept %s (%s) not loaded — skipping FieldConceptMapping for %s',
                snomed_code, display, field_name,
            )
            continue

        FieldConceptMapping.objects.get_or_create(
            field_name=field_name,
            defaults=dict(
                concept=concept,
                vocabulary_id='SNOMED',
                concept_code=snomed_code,
                omop_table='observation',
                source_value=snomed_code,
                value_kind='boolean',
                type_concept_id=32817,
                value_vocabulary='',
                multiple=False,
                status='approved',
                notes=notes,
            ),
        )

        for sort_order, label in enumerate(_CHOICES):
            FieldChoice.objects.get_or_create(
                field_name=field_name,
                display=label,
                defaults=dict(sort_order=sort_order),
            )


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0313_add_b_symptom_fields'),
    ]

    operations = [
        migrations.RunPython(create_mappings, migrations.RunPython.noop),
    ]
