"""CDISC Biomedical Concepts become source codes on the Wearables tab.

0354 linked OMOP concepts to BCs in a table of its own. A mapping here always
runs from a source code to an OMOP concept, so a BC is now a source code too:
vocabulary ``CDISC-BC``, code the BC id (an NCIt C-code), one OMOP concept as
its destination. It is proposed, voted on, approved and copied like any other
row, and a wearable code reaches a BC by sharing its destination. 0356 drops
the link table.

One destination per BC, so where 0354 linked a BC to several concepts
(Heart Rate to general, resting and walking heart rate) the general concept is
kept. Rows are ``proposed``. A BC row takes the category and metric key of the
wearable rows already mapped to its concept, so it sits in their group. A row
whose concept is not loaded on this instance is skipped and logged; re-running
creates it once the vocabulary is there. An existing row is left as it is.
Reverse is a no-op: the rows may have been curated or voted on.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

VOCABULARY_ID = 'CDISC-BC'
ORIGIN_SYSTEM = 'cdisc-bc-seed v1'
TABLES = {'Measurement': 'measurement', 'Observation': 'observation'}
WEARABLE_VOCABULARIES = ('OpenWearables', 'Apple', 'Garmin', 'HL7-PHR', 'HealthConnect')

# (BC id, destination vocabulary, destination code)
BC_DESTINATIONS = [
    ('C49677', 'LOINC', '8867-4'),      # Heart Rate
    ('C60832', 'LOINC', '59408-5'),     # Oxygen Saturation Measurement
    ('C49678', 'LOINC', '9279-1'),      # Respiratory Rate
    ('C81328', 'LOINC', '29463-7'),     # Body Weight
    ('C164634', 'LOINC', '8302-2'),     # Body Height
    ('C16358', 'LOINC', '39156-5'),     # Body Mass Index
    ('C174446', 'LOINC', '8310-5'),     # Body Temperature
    ('C174371', 'LOINC', '39106-0'),    # Peripheral Body Temperature -> skin temperature
    ('C25298', 'LOINC', '8480-6'),      # Systolic Blood Pressure
    ('C25299', 'LOINC', '8462-4'),      # Diastolic Blood Pressure
    ('C100948', 'LOINC', '8280-0'),     # Waist Circumference
    ('C122232', 'LOINC', '41982-0'),    # Body Fat Measurement -> percentage
    ('C71258', 'LOINC', '91557-9'),     # Lean Body Mass
    ('C199682', 'LOINC', '80404-7'),    # Heart Rate Variability, SDNN
    ('C174363', 'LOINC', '94122-9'),    # Maximal Oxygen Consumption, Estimated
    ('C147491', 'LOINC', '93819-1'),    # Energy Expenditure -> active energy
    ('C126083', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY'),  # Basal Metabolic Rate
    ('C105585', 'LOINC', '2339-0'),     # Glucose Measurement
    ('C38084', 'LOINC', '20150-9'),     # Forced Expiratory Volume in 1 Second
    ('C111361', 'LOINC', '19870-5'),    # Forced Vital Capacity
    ('C41372', 'LOINC', '33452-4'),     # Peak Expiratory Flow
]


def seed_bc_source_codes(apps, schema_editor):
    alias = schema_editor.connection.alias
    BC = apps.get_model('omop_core', 'BiomedicalConcept')
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    mappings = Mapping.objects.using(alias)
    names = dict(BC.objects.using(alias).filter(
        bc_id__in=[bc for bc, _, _ in BC_DESTINATIONS]).values_list('bc_id', 'short_name'))
    created, skipped = 0, []
    for bc_id, vocabulary_id, code in BC_DESTINATIONS:
        concept = Concept.objects.using(alias).filter(
            vocabulary_id=vocabulary_id, concept_code=code).first()
        if concept is None or concept.domain_id not in TABLES or bc_id not in names:
            skipped.append(bc_id)
            continue
        sibling = mappings.filter(
            organization__isnull=True, source_vocabulary_id__in=WEARABLE_VOCABULARIES,
            target_concept_id=concept.pk,
        ).exclude(status='rejected').exclude(metric_key='').order_by(
            'source_vocabulary_id', 'source_code').first()
        _, was_created = mappings.get_or_create(
            organization=None, source_vocabulary_id=VOCABULARY_ID, source_code=bc_id,
            defaults={
                'source_code_description': names[bc_id][:255],
                'domain_id': concept.domain_id, 'omop_table': TABLES[concept.domain_id],
                'target_concept_id': concept.pk, 'destination_vocabulary_id': vocabulary_id,
                'status': 'proposed', 'origin': 'import', 'origin_system': ORIGIN_SYSTEM,
                'category': sibling.category if sibling else '',
                'metric_key': sibling.metric_key if sibling else '',
            },
        )
        created += was_created
    if skipped:
        logger.warning('CDISC BC source codes skipped (concept or BC not loaded here): %s',
                       ', '.join(skipped))
    logger.info('Created %d CDISC BC source code mappings.', created)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0354_load_cdisc_biomedical_concepts'),
    ]

    operations = [
        migrations.RunPython(seed_bc_source_codes, migrations.RunPython.noop),
    ]
