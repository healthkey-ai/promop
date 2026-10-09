"""Seed per-choice standard Observation mappings for cytogenetic markers (#1049).

Concept IDs, codes, names and domains verified against Athena CONCEPT.csv.
These are broader SNOMED categories, not marker-specific LOINC test concepts.
"""
from datetime import date

from django.db import migrations

FIELD = 'cytogenetic_markers'
CONCEPTS = (
    (4030018, '107675007', 'Chromosomal morphology'),
    (4284835, '67285006', 'Deletion of short arm'),
    (4275261, '64329008', 'Deletion of long arm'),
    (4049480, '15897004', 'Chromosomal translocation'),
    (4215516, '41669009', 'Alteration of chromosome structure'),
    (4208087, '55597007', 'Hyperploidy'),
)
CHOICES = (
    ('del17p', '67285006'),
    ('t(4;14)', '15897004'),
    ('t(11;14)', '15897004'),
    ('t(14;16)', '15897004'),
    ('1q_gain', '41669009'),
    ('1q_amp', '41669009'),
    ('hyperdiploidy', '55597007'),
    ('del13q', '64329008'),
    ('MYC rearrangement', '41669009'),
)


def seed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Choice = apps.get_model('omop_core', 'FieldChoice')
    Code = apps.get_model('omop_core', 'FieldChoiceCode')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for model, key, name in (
        ('Vocabulary', 'vocabulary', 'SNOMED'),
        ('Domain', 'domain', 'Observation'),
        ('ConceptClass', 'concept_class', 'Morph Abnormality'),
    ):
        defaults = {key + '_name': name, key + '_concept_id': 0}
        if model == 'Vocabulary':
            defaults.update(vocabulary_reference='https://www.snomed.org/', vocabulary_version='')
        apps.get_model('omop_core', model).objects.using(alias).get_or_create(
            **{key + '_id': name}, defaults=defaults)
    resolved = {}
    for concept_id, code, name in CONCEPTS:
        concept, _ = Concept.objects.using(alias).get_or_create(
            vocabulary_id='SNOMED', concept_code=code,
            defaults=dict(concept_id=concept_id, concept_name=name, domain_id='Observation',
                          concept_class_id='Morph Abnormality', standard_concept='S',
                          valid_start_date=date(2002, 1, 31), valid_end_date=date(2099, 12, 31)),
        )
        resolved[code] = concept
    for order, (display, code) in enumerate(CHOICES):
        choice, _ = Choice.objects.using(alias).get_or_create(
            field_name=FIELD, display=display, defaults={'sort_order': order})
        Code.objects.using(alias).filter(choice=choice).update(is_primary=False)
        Code.objects.using(alias).update_or_create(
            choice=choice, vocabulary_id='SNOMED', code=code,
            defaults={'display': resolved[code].concept_name, 'is_primary': True})
    Mapping.objects.using(alias).update_or_create(
        field_name=FIELD,
        defaults=dict(concept=resolved['107675007'], vocabulary_id='SNOMED',
                      concept_code='107675007', omop_table='Observation',
                      source_value='mm-cytogenetic-markers', value_kind='string',
                      multiple=True, value_vocabulary='', type_concept_id=32817,
                      status='approved', provenance='system_generated',
                      notes='Per-choice standard SNOMED Observation categories (#1049). '
                            'Exact markers are retained in distinct source keys and values; '
                            'FieldChoiceCode resolves each selected value.'),
    )


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0229_merge_cytogenetics_and_domain_audit'),
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
