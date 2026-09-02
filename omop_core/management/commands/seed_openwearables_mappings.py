"""Seed SourceCodeConceptMapping rows for OpenWearables metrics.

Creates one proposed mapping per OpenWearables SeriesType metric code.
Where a well-known LOINC equivalent exists the target_concept is resolved
from the concept table; otherwise the row is left unmapped for curator review.

Idempotent: uses get_or_create on (source_vocabulary_id, source_code).
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

logger = logging.getLogger(__name__)

# (source_code, display_name, unit, domain, target_vocab, target_code)
# target_vocab/target_code are None when no standard equivalent is known.
OPENWEARABLES_METRICS = [
    # ── Heart & Cardiovascular ──────────────────────────────────────────
    ('heart_rate', 'Heart rate', 'bpm', 'Measurement', 'LOINC', '8867-4'),
    ('resting_heart_rate', 'Resting heart rate', 'bpm', 'Measurement', 'LOINC', '40443-4'),
    ('heart_rate_variability_sdnn', 'Heart rate variability (SDNN)', 'ms', 'Measurement', 'LOINC', '80404-7'),
    ('heart_rate_recovery_one_minute', 'Heart rate recovery (1 min)', 'bpm', 'Measurement', None, None),
    ('walking_heart_rate_average', 'Walking heart rate average', 'bpm', 'Measurement', None, None),
    ('heart_rate_variability_rmssd', 'Heart rate variability (RMSSD)', 'ms', 'Measurement', None, None),

    # ── Blood & Respiratory ─────────────────────────────────────────────
    ('oxygen_saturation', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5'),
    ('blood_glucose', 'Blood glucose', 'mg/dL', 'Measurement', 'LOINC', '2339-0'),
    ('blood_pressure_systolic', 'Systolic blood pressure', 'mmHg', 'Measurement', 'LOINC', '8480-6'),
    ('blood_pressure_diastolic', 'Diastolic blood pressure', 'mmHg', 'Measurement', 'LOINC', '8462-4'),
    ('respiratory_rate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1'),
    ('sleeping_breathing_disturbances', 'Sleeping breathing disturbances', 'count', 'Observation', None, None),
    ('blood_alcohol_content', 'Blood alcohol content', 'mg/dL', 'Measurement', None, None),  # no wearable-appropriate LOINC
    ('peripheral_perfusion_index', 'Peripheral perfusion index', 'score', 'Measurement', None, None),
    ('forced_vital_capacity', 'Forced vital capacity (FVC)', 'L', 'Measurement', 'LOINC', '19868-9'),
    ('forced_expiratory_volume_1', 'Forced expiratory volume in 1s (FEV1)', 'L', 'Measurement', 'LOINC', '20150-9'),
    ('peak_expiratory_flow_rate', 'Peak expiratory flow rate (PEFR)', 'L/min', 'Measurement', 'LOINC', '19935-6'),
    ('breathing_disturbance_index', 'Breathing disturbance index', 'score', 'Measurement', None, None),

    # ── Body Composition ────────────────────────────────────────────────
    ('height', 'Body height', 'cm', 'Measurement', 'LOINC', '8302-2'),
    ('weight', 'Body weight', 'kg', 'Measurement', 'LOINC', '29463-7'),
    ('body_fat_percentage', 'Body fat percentage', '%', 'Measurement', 'LOINC', '41982-0'),
    ('body_mass_index', 'Body mass index (BMI)', 'kg/m2', 'Measurement', 'LOINC', '39156-5'),
    ('lean_body_mass', 'Lean body mass', 'kg', 'Measurement', 'LOINC', '88334-8'),
    ('body_temperature', 'Body temperature', '\u00b0C', 'Measurement', 'LOINC', '8310-5'),
    ('skin_temperature', 'Skin temperature', '\u00b0C', 'Measurement', 'LOINC', '39106-0'),
    ('waist_circumference', 'Waist circumference', 'cm', 'Measurement', 'LOINC', '56086-2'),
    ('body_fat_mass', 'Body fat mass', 'kg', 'Measurement', None, None),
    ('skeletal_muscle_mass', 'Skeletal muscle mass', 'kg', 'Measurement', None, None),
    ('skin_temperature_deviation', 'Skin temperature deviation from baseline', '\u00b0C', 'Measurement', None, None),
    ('skin_temperature_trend_deviation', 'Skin temperature trend deviation', '\u00b0C', 'Measurement', None, None),

    # ── Fitness Metrics ─────────────────────────────────────────────────
    ('vo2_max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9'),
    ('six_minute_walk_test_distance', '6-minute walk test distance', 'm', 'Measurement', 'LOINC', '64098-7'),
    ('cardiovascular_age', 'Cardiovascular age estimate', 'years', 'Measurement', None, None),

    # ── Activity — Basic ────────────────────────────────────────────────
    ('steps', 'Steps', 'count', 'Observation', 'LOINC', '55423-8'),
    ('energy', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1'),
    ('basal_energy', 'Basal energy expenditure', 'kcal', 'Measurement', None, None),
    ('stand_time', 'Stand time', 'min', 'Observation', None, None),
    ('exercise_time', 'Exercise time', 'min', 'Observation', 'LOINC', '55411-3'),
    ('physical_effort', 'Physical effort score', 'score', 'Observation', None, None),
    ('flights_climbed', 'Flights of stairs climbed', 'count', 'Observation', None, None),
    ('average_met', 'Average metabolic equivalent (MET)', 'MET', 'Observation', None, None),
    ('active_time', 'Active time', 'min', 'Observation', None, None),

    # ── Activity — Distance ─────────────────────────────────────────────
    ('distance_walking_running', 'Walking + running distance', 'm', 'Measurement', 'LOINC', '41953-1'),
    ('distance_cycling', 'Cycling distance', 'm', 'Measurement', None, None),
    ('distance_swimming', 'Swimming distance', 'm', 'Measurement', None, None),
    ('distance_downhill_snow_sports', 'Downhill snow sports distance', 'm', 'Measurement', None, None),
    ('distance_other', 'Other activity distance', 'm', 'Measurement', None, None),

    # ── Activity — Walking Metrics ──────────────────────────────────────
    ('walking_step_length', 'Walking step length', 'cm', 'Measurement', None, None),
    ('walking_speed', 'Walking speed', 'm/s', 'Measurement', 'LOINC', '41957-2'),
    ('walking_double_support_percentage', 'Walking double support percentage', '%', 'Measurement', None, None),
    ('walking_asymmetry_percentage', 'Walking asymmetry percentage', '%', 'Measurement', None, None),
    ('walking_steadiness', 'Walking steadiness', '%', 'Measurement', None, None),
    ('stair_descent_speed', 'Stair descent speed', 'm/s', 'Measurement', None, None),
    ('stair_ascent_speed', 'Stair ascent speed', 'm/s', 'Measurement', None, None),

    # ── Activity — Running Metrics ──────────────────────────────────────
    ('running_power', 'Running power', 'W', 'Measurement', None, None),
    ('running_speed', 'Running speed', 'm/s', 'Measurement', None, None),
    ('running_vertical_oscillation', 'Running vertical oscillation', 'cm', 'Measurement', None, None),
    ('running_ground_contact_time', 'Running ground contact time', 'ms', 'Measurement', None, None),
    ('running_stride_length', 'Running stride length', 'cm', 'Measurement', None, None),
    ('running_vertical_ratio', 'Running vertical ratio', '%', 'Measurement', None, None),
    ('running_stance_time_balance', 'Running stance time balance', '%', 'Measurement', None, None),

    # ── Activity — Swimming Metrics ─────────────────────────────────────
    ('swimming_stroke_count', 'Swimming stroke count', 'count', 'Measurement', None, None),
    ('underwater_depth', 'Underwater depth', 'm', 'Measurement', None, None),

    # ── Activity — Generic ──────────────────────────────────────────────
    ('cadence', 'Cadence', 'rpm', 'Measurement', None, None),
    ('power', 'Power output', 'W', 'Measurement', None, None),
    ('speed', 'Speed', 'm/s', 'Measurement', None, None),
    ('workout_effort_score', 'Workout effort score', 'score', 'Observation', None, None),
    ('estimated_workout_effort_score', 'Estimated workout effort score', 'score', 'Observation', None, None),

    # ── Environmental ───────────────────────────────────────────────────
    ('environmental_audio_exposure', 'Environmental audio exposure', 'dB', 'Observation', None, None),
    ('headphone_audio_exposure', 'Headphone audio exposure', 'dB', 'Observation', None, None),
    ('environmental_sound_reduction', 'Environmental sound reduction', 'dB', 'Observation', None, None),
    ('time_in_daylight', 'Time in daylight', 'min', 'Observation', None, None),
    ('water_temperature', 'Water temperature', '\u00b0C', 'Measurement', None, None),
    ('uv_exposure', 'UV exposure', 'count', 'Observation', None, None),
    ('inhaler_usage', 'Inhaler usage', 'count', 'Observation', None, None),
    ('weather_temperature', 'Weather temperature', '\u00b0C', 'Observation', None, None),
    ('weather_humidity', 'Weather humidity', '%', 'Observation', None, None),
    ('elevation', 'Elevation', 'm', 'Measurement', None, None),
    ('latitude', 'Latitude', '\u00b0', 'Observation', None, None),
    ('longitude', 'Longitude', '\u00b0', 'Observation', None, None),
    ('air_temperature', 'Air temperature', '\u00b0C', 'Observation', None, None),

    # ── Provider-Specific — Garmin ──────────────────────────────────────
    ('garmin_stress_level', 'Stress level (Garmin)', 'score', 'Observation', None, None),
    ('garmin_skin_temperature', 'Skin temperature deviation (Garmin)', '\u00b0C', 'Measurement', None, None),
    ('garmin_fitness_age', 'Fitness age (Garmin)', 'years', 'Measurement', None, None),
    ('garmin_body_battery', 'Body battery (Garmin)', '%', 'Observation', None, None),

    # ── Other ───────────────────────────────────────────────────────────
    ('electrodermal_activity', 'Electrodermal activity', '\u03bcS', 'Measurement', None, None),
    ('push_count', 'Push count (wheelchair)', 'count', 'Observation', None, None),
    ('atrial_fibrillation_burden', 'Atrial fibrillation burden', '%', 'Measurement', None, None),
    ('insulin_delivery', 'Insulin delivery', 'U', 'Measurement', None, None),
    ('number_of_times_fallen', 'Number of times fallen', 'count', 'Observation', None, None),
    ('number_of_alcoholic_beverages', 'Number of alcoholic beverages', 'count', 'Observation', None, None),
    ('nike_fuel', 'Nike Fuel points', 'count', 'Observation', None, None),
    ('hydration', 'Hydration intake', 'mL', 'Observation', None, None),
]

VOCABULARY_ID = 'OpenWearables'


class Command(BaseCommand):
    help = 'Seed SourceCodeConceptMapping rows for OpenWearables metrics.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created without writing.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']

        # Collect all target codes that need concept lookup.
        loinc_codes = set()
        for _code, _name, _unit, _domain, t_vocab, t_code in OPENWEARABLES_METRICS:
            if t_vocab == 'LOINC' and t_code:
                loinc_codes.add(t_code)

        # Batch-resolve OMOP concepts from the concept table.
        concepts = {}
        if loinc_codes:
            for c in Concept.objects.filter(
                vocabulary_id='LOINC',
                concept_code__in=loinc_codes,
            ).only('concept_id', 'concept_code', 'vocabulary_id'):
                concepts[('LOINC', c.concept_code)] = c

        created = existed = unresolved = 0
        with transaction.atomic():
            for ow_code, display, unit, domain, t_vocab, t_code in OPENWEARABLES_METRICS:
                target = concepts.get((t_vocab, t_code)) if t_vocab else None
                desc = f'{display} ({unit})' if unit else display
                omop_table = DOMAIN_TO_TABLE.get(domain, '')

                if t_vocab and t_code and not target:
                    logger.warning(
                        'Concept not found: %s %s for metric %s',
                        t_vocab, t_code, ow_code,
                    )

                if dry_run:
                    status = 'MAPPED' if target else 'UNMAPPED'
                    self.stdout.write(f'  [{status}] {ow_code} -> {t_vocab or "-"}:{t_code or "-"}')
                    if not target:
                        unresolved += 1
                    continue

                _, was_created = SourceCodeConceptMapping.objects.get_or_create(
                    source_vocabulary_id=VOCABULARY_ID,
                    source_code=ow_code,
                    defaults={
                        'domain_id': domain,
                        'source_code_description': desc[:255],
                        'target_concept': target,
                        'destination_vocabulary_id': target.vocabulary_id if target else '',
                        'omop_table': omop_table,
                        'status': 'proposed',
                        'origin': 'import',
                        'origin_system': 'open-wearables-seed',
                        'source': 'HealthKey',
                        'occurrence_count': 0,
                    },
                )
                if was_created:
                    created += 1
                else:
                    existed += 1
                if not target:
                    unresolved += 1

        total = len(OPENWEARABLES_METRICS)
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: {total} metrics, {total - unresolved} mapped, '
                f'{unresolved} unmapped.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Done: {created} created, {existed} already existed, '
                f'{unresolved} without target concept (of {total} total).'
            ))
