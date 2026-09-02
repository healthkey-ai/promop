"""Seed SourceCodeConceptMapping rows for Apple HealthKit and Garmin FIT metrics.

Creates one *approved* mapping per device-specific source code, derived from
the existing hard-coded mapping tables in wearable_parsers.py and mappings.py.
These mappings represent the code paths that are already proven in production.

Idempotent: uses get_or_create on (source_vocabulary_id, source_code).
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

logger = logging.getLogger(__name__)

# (source_code, display_name, unit, domain, dest_vocab, dest_code, metric_key)
# dest_vocab/dest_code resolve the target concept via (vocabulary_id, concept_code).
# metric_key is the internal key used by WEARABLE_CONCEPT_CODE.

APPLE_METRICS = [
    ('HKQuantityTypeIdentifierStepCount', 'Step count', 'count', 'Observation', 'LOINC', '55423-8', 'steps'),
    ('HKQuantityTypeIdentifierAppleExerciseTime', 'Exercise time (active minutes)', 'min', 'Observation', 'LOINC', '55411-3', 'active_minutes'),
    ('HKQuantityTypeIdentifierRestingHeartRate', 'Resting heart rate', 'bpm', 'Measurement', 'LOINC', '40443-4', 'resting_hr'),
    ('HKQuantityTypeIdentifierHeartRateVariabilitySDNN', 'Heart rate variability (SDNN)', 'ms', 'Measurement', 'LOINC', '80404-7', 'hrv_sdnn'),
    ('HKQuantityTypeIdentifierOxygenSaturation', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5', 'spo2'),
    ('HKQuantityTypeIdentifierRespiratoryRate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1', 'respiratory_rate'),
    ('HKQuantityTypeIdentifierVO2Max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9', 'vo2_max'),
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 'Walking + running distance', 'km', 'Measurement', 'LOINC', '41953-1', 'distance'),
    ('HKQuantityTypeIdentifierWalkingSpeed', 'Walking speed', 'km/hr', 'Measurement', 'LOINC', '41957-2', 'walking_speed'),
    ('HKQuantityTypeIdentifierWalkingStepLength', 'Walking step length', 'cm', 'Measurement', 'HK-Wearable', 'HK-WEAR-STEP-LENGTH', 'walking_step_length'),
    ('HKQuantityTypeIdentifierWalkingDoubleSupportPercentage', 'Walking double support percentage', '%', 'Measurement', 'HK-Wearable', 'HK-WEAR-DBL-SUPPORT', 'walking_double_support_pct'),
    ('HKQuantityTypeIdentifierWalkingHeartRateAverage', 'Walking heart rate average', 'bpm', 'Measurement', 'HK-Wearable', 'HK-WEAR-WALK-HR', 'walking_hr_avg'),
    ('HKQuantityTypeIdentifierFlightsClimbed', 'Flights of stairs climbed', 'count', 'Observation', 'LOINC', '100304-5', 'flights_climbed'),
    ('HKQuantityTypeIdentifierActiveEnergyBurned', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1', 'active_energy'),
    ('HKQuantityTypeIdentifierBasalEnergyBurned', 'Basal energy expenditure', 'kcal', 'Measurement', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY', 'basal_energy'),
    ('HKQuantityTypeIdentifierBodyMass', 'Body weight', 'kg', 'Measurement', 'LOINC', '29463-7', 'body_mass'),
    ('HKCategoryTypeIdentifierSleepAnalysis', 'Sleep duration', 'h', 'Observation', 'LOINC', '93832-4', 'sleep_duration'),
]

GARMIN_METRICS = [
    ('steps', 'Step count', 'count', 'Observation', 'LOINC', '55423-8', 'steps'),
    ('active_minutes', 'Active minutes', 'min', 'Observation', 'LOINC', '55411-3', 'active_minutes'),
    ('resting_hr', 'Resting heart rate', 'bpm', 'Measurement', 'LOINC', '40443-4', 'resting_hr'),
    ('hrv_rmssd', 'Heart rate variability (RMSSD)', 'ms', 'Measurement', 'HK-Wearable', 'HK-WEAR-HRV-RMSSD', 'hrv_rmssd'),
    ('spo2', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5', 'spo2'),
    ('respiratory_rate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1', 'respiratory_rate'),
    ('sleep_duration', 'Sleep duration', 'h', 'Observation', 'LOINC', '93832-4', 'sleep_duration'),
    ('vo2_max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9', 'vo2_max'),
    ('distance', 'Walking + running distance', 'km', 'Measurement', 'LOINC', '41953-1', 'distance'),
    ('active_energy', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1', 'active_energy'),
    ('basal_energy', 'Basal energy expenditure', 'kcal', 'Measurement', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY', 'basal_energy'),
]


class Command(BaseCommand):
    help = 'Seed SourceCodeConceptMapping rows for Apple HealthKit and Garmin FIT metrics.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created without writing.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']

        # Collect all target codes that need concept lookup.
        lookup_keys = set()
        for metrics in (APPLE_METRICS, GARMIN_METRICS):
            for _code, _name, _unit, _domain, d_vocab, d_code, _mkey in metrics:
                lookup_keys.add((d_vocab, d_code))

        # Batch-resolve OMOP concepts from the concept table.
        concepts = {}
        vocab_codes = {}
        for vocab, code in lookup_keys:
            vocab_codes.setdefault(vocab, set()).add(code)

        for vocab, codes in vocab_codes.items():
            for c in Concept.objects.filter(
                vocabulary_id=vocab,
                concept_code__in=codes,
            ).only('concept_id', 'concept_code', 'vocabulary_id'):
                concepts[(vocab, c.concept_code)] = c

        device_sets = [
            ('Apple', APPLE_METRICS, 'hk-wearables-apple'),
            ('Garmin', GARMIN_METRICS, 'hk-wearables-garmin'),
        ]

        total_created = total_existed = total_unresolved = 0

        with transaction.atomic():
            for source_vocab, metrics, origin_system in device_sets:
                created = existed = unresolved = 0
                for src_code, display, unit, domain, d_vocab, d_code, _mkey in metrics:
                    target = concepts.get((d_vocab, d_code))
                    desc = f'{display} ({unit})' if unit else display
                    omop_table = DOMAIN_TO_TABLE.get(domain, '')

                    if not target:
                        logger.warning(
                            'Concept not found: %s %s for %s metric %s — '
                            'skipping (run load_athena_vocabularies first)',
                            d_vocab, d_code, source_vocab, src_code,
                        )
                        unresolved += 1
                        if dry_run:
                            self.stdout.write(
                                f'  [UNMAPPED] {source_vocab}:{src_code} -> '
                                f'{d_vocab}:{d_code}'
                            )
                        continue

                    if dry_run:
                        self.stdout.write(
                            f'  [MAPPED] {source_vocab}:{src_code} -> '
                            f'{d_vocab}:{d_code}'
                        )
                        continue

                    _, was_created = SourceCodeConceptMapping.objects.get_or_create(
                        source_vocabulary_id=source_vocab,
                        source_code=src_code,
                        defaults={
                            'domain_id': domain,
                            'source_code_description': desc[:255],
                            'target_concept': target,
                            'destination_vocabulary_id': d_vocab,
                            'omop_table': omop_table,
                            'status': 'approved',
                            'origin': 'import',
                            'origin_system': origin_system,
                            'source': 'HealthKey',
                            'occurrence_count': 0,
                        },
                    )
                    if was_created:
                        created += 1
                    else:
                        existed += 1

                total_created += created
                total_existed += existed
                total_unresolved += unresolved

                count = len(metrics)
                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f'DRY RUN [{source_vocab}]: {count} metrics, '
                        f'{count - unresolved} mapped, {unresolved} unmapped.'
                    ))
                else:
                    self.stdout.write(self.style.SUCCESS(
                        f'{source_vocab}: {created} created, {existed} already existed, '
                        f'{unresolved} without target concept (of {count} total).'
                    ))

        total = len(APPLE_METRICS) + len(GARMIN_METRICS)
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'\nDRY RUN total: {total} metrics, '
                f'{total - total_unresolved} mapped, {total_unresolved} unmapped.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone: {total_created} created, {total_existed} already existed, '
                f'{total_unresolved} without target concept (of {total} total).'
            ))
