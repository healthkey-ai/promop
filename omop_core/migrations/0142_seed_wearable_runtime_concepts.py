import logging

from django.db import migrations

logger = logging.getLogger(__name__)


# Concepts the wearable write path hard-depends on at runtime.
#
# Why a migration rather than seed_omop_concepts: start.sh runs only
# `manage.py migrate`. seed_omop_concepts is a manual command, so nothing in
# the deploy path installs these. Two consequences motivated this:
#
#   1. upload_wearable now refuses to write when the provenance type concept is
#      missing (#441) rather than silently substituting 'Lab'. Without this
#      migration that guard turns every upload into a 503 after deploy.
#   2. HK-WEAR-HRV-RMSSD is a local mint, so Athena can never supply it — not
#      even on a fully-loaded deployment. Without it, Garmin HRV samples resolve
#      to None and are discarded with only a WARNING, which combined with the
#      parser change in #438 would drop Garmin HRV entirely.
#
# The other four HK-Wearable mints are included for the same reason: they are
# locally authored, so no vocabulary load produces them, and a deployment that
# never ran the seed command silently drops those metrics too. That is a
# pre-existing gap, not one #438 introduced, but it has the same single cause
# and the same one-line fix.
#
# Definitions are duplicated from seed_omop_concepts rather than imported:
# migrations must stay frozen against the code at the time they were written.
# omop_core/tests.py asserts the two stay consistent.

TYPE_CONCEPT_ID = 32865

_TYPE_CONCEPT = dict(
    concept_id=TYPE_CONCEPT_ID,
    concept_name='Patient self-report',
    domain_id='Type Concept',
    vocabulary_id='Type Concept',
    concept_class_id='Type Concept',
    standard_concept='S',
    concept_code='OMOP4976938',
    source=None,
)

_HK_WEARABLE_CONCEPTS = [
    dict(concept_id=2029606350, concept_name='Walking step length',
         concept_code='HK-WEAR-STEP-LENGTH'),
    dict(concept_id=2029606351, concept_name='Walking double support time percentage',
         concept_code='HK-WEAR-DBL-SUPPORT'),
    dict(concept_id=2029606352, concept_name='Heart rate during walking',
         concept_code='HK-WEAR-WALK-HR'),
    dict(concept_id=2029606353, concept_name='Basal energy expenditure',
         concept_code='HK-WEAR-BASAL-ENERGY'),
    dict(concept_id=2029606354, concept_name='R-R interval RMSSD (heart rate variability)',
         concept_code='HK-WEAR-HRV-RMSSD'),
]


def _ensure_supporting_rows(apps):
    """Domain / Vocabulary / ConceptClass rows the concepts below reference."""
    Domain = apps.get_model('omop_core', 'Domain')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')

    for domain_id in ('Type Concept', 'Measurement'):
        Domain.objects.get_or_create(
            domain_id=domain_id,
            defaults={'domain_name': domain_id, 'domain_concept_id': 0},
        )
    for vocabulary_id, name in (('Type Concept', 'OMOP Type Concept'),
                                ('HK-Wearable', 'HealthKey Wearable Metrics')):
        Vocabulary.objects.get_or_create(
            vocabulary_id=vocabulary_id,
            defaults={'vocabulary_name': name, 'vocabulary_reference': '',
                      'vocabulary_version': '', 'vocabulary_concept_id': 0},
        )
    for concept_class_id in ('Type Concept', 'Clinical Observation'):
        ConceptClass.objects.get_or_create(
            concept_class_id=concept_class_id,
            defaults={'concept_class_name': concept_class_id,
                      'concept_class_concept_id': 0},
        )


def seed_wearable_runtime_concepts(apps, schema_editor):
    import datetime

    Concept = apps.get_model('omop_core', 'Concept')
    _ensure_supporting_rows(apps)

    valid_start = datetime.date(1970, 1, 1)
    valid_end = datetime.date(2099, 12, 31)

    # --- Provenance type concept -------------------------------------------
    #
    # 32865 is a genuine Athena concept_id, so on an Athena-loaded database it
    # already exists and is left alone. On a database without that load it may
    # instead hold a *shadow* row: patient_portal.api.lab_results.sync mints
    # concept_id 32865 into the HK-Labs vocabulary as a fallback when Athena is
    # absent. That row occupies a real Athena id with locally-authored content —
    # the defect class #415 exists to eliminate — and upload_wearable would
    # otherwise accept it and type every wearable row with it.
    #
    # Repairing rather than skipping is safe: any row already pointing at 32865
    # meant 'Patient self-report', which is exactly what the corrected row says.
    existing = Concept.objects.filter(concept_id=TYPE_CONCEPT_ID).first()
    if existing is None:
        Concept.objects.create(
            valid_start_date=valid_start, valid_end_date=valid_end, **_TYPE_CONCEPT)
    elif existing.vocabulary_id != 'Type Concept':
        logger.warning(
            'Repairing shadow concept %s: vocabulary_id=%r source=%r concept_code=%r '
            '-> genuine Athena Type Concept row.',
            TYPE_CONCEPT_ID, existing.vocabulary_id, existing.source,
            existing.concept_code,
        )
        for field, value in _TYPE_CONCEPT.items():
            setattr(existing, field, value)
        existing.save()

    # --- Locally-minted wearable concepts ----------------------------------
    #
    # Keyed on concept_id, matching seed_omop_concepts, so a database that has
    # already run the seed command is untouched.
    for row in _HK_WEARABLE_CONCEPTS:
        Concept.objects.get_or_create(
            concept_id=row['concept_id'],
            defaults=dict(
                concept_name=row['concept_name'],
                domain_id='Measurement',
                vocabulary_id='HK-Wearable',
                concept_class_id='Clinical Observation',
                standard_concept=None,
                concept_code=row['concept_code'],
                source='HealthKey',
                valid_start_date=valid_start,
                valid_end_date=valid_end,
            ),
        )


def reverse_noop(apps, schema_editor):
    """Deliberately does nothing.

    These concepts are referenced by measurement/observation rows. Deleting
    them on rollback would either cascade into clinical data or fail on the FK,
    and leaving them costs nothing — they are valid rows either way.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0141_patientrecord_hrv_rmssd_avg_30d_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_wearable_runtime_concepts, reverse_noop),
    ]
