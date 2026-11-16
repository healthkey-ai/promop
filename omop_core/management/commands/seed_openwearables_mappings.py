"""Seed SourceCodeConceptMapping rows for OpenWearables metrics.

Creates one proposed mapping per OpenWearables SeriesType metric code.
Where the wearable mapping proposal names a target (LOINC, SNOMED or a local
HK-Wearable concept) the target_concept is resolved from the concept table;
otherwise the row is left unmapped for curator review. Where the proposal and
the HL7 PHR code list disagree, the HL7 code is the one seeded.

Idempotent: uses get_or_create on (source_vocabulary_id, source_code), so a
row that exists is left exactly as it is. A target that changes here reaches an
already seeded instance only with ``--update-seeded``, which re-points a row
whose destination nothing but this command has set (see ``_not_seed_owned``)
and never clears a destination.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.models import Concept, ConceptMappingLock, SourceCodeConceptMapping
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
    ('walking_heart_rate_average', 'Walking heart rate average', 'bpm', 'Measurement', 'HK-Wearable', 'HK-WEAR-WALK-HR'),
    ('heart_rate_variability_rmssd', 'Heart rate variability (RMSSD)', 'ms', 'Measurement', 'HK-Wearable', 'HK-WEAR-HRV-RMSSD'),

    # ── Blood & Respiratory ─────────────────────────────────────────────
    ('oxygen_saturation', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5'),
    ('blood_glucose', 'Blood glucose', 'mg/dL', 'Measurement', 'LOINC', '2339-0'),
    ('cgm_glucose', 'CGM glucose (interstitial fluid)', 'mg/dL', 'Measurement', 'LOINC', '99504-3'),
    ('blood_pressure_systolic', 'Systolic blood pressure', 'mmHg', 'Measurement', 'LOINC', '8480-6'),
    ('blood_pressure_diastolic', 'Diastolic blood pressure', 'mmHg', 'Measurement', 'LOINC', '8462-4'),
    ('respiratory_rate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1'),
    ('sleeping_breathing_disturbances', 'Sleeping breathing disturbances', 'count', 'Observation', None, None),
    ('blood_alcohol_content', 'Blood alcohol content', 'mg/dL', 'Measurement', 'SNOMED', '54791000237107'),  # no wearable-appropriate LOINC
    ('peripheral_perfusion_index', 'Peripheral perfusion index', 'score', 'Measurement', None, None),
    ('forced_vital_capacity', 'Forced vital capacity (FVC)', 'L', 'Measurement', 'LOINC', '19870-5'),  # HL7 PHR; was 19868-9 (by Spirometry)
    ('forced_expiratory_volume_1', 'Forced expiratory volume in 1s (FEV1)', 'L', 'Measurement', 'LOINC', '20150-9'),
    ('peak_expiratory_flow_rate', 'Peak expiratory flow rate (PEFR)', 'L/min', 'Measurement', 'LOINC', '33452-4'),  # HL7 PHR; was 19935-6 (by Peak flow meter)
    ('breathing_disturbance_index', 'Breathing disturbance index', '/h', 'Measurement', 'LOINC', '90566-1'),

    # ── Body Composition ────────────────────────────────────────────────
    ('height', 'Body height', 'cm', 'Measurement', 'LOINC', '8302-2'),
    ('weight', 'Body weight', 'kg', 'Measurement', 'LOINC', '29463-7'),
    ('body_fat_percentage', 'Body fat percentage', '%', 'Measurement', 'LOINC', '41982-0'),
    ('body_mass_index', 'Body mass index (BMI)', 'kg/m2', 'Measurement', 'LOINC', '39156-5'),
    ('lean_body_mass', 'Lean body mass', 'kg', 'Measurement', 'LOINC', '91557-9'),  # HL7 PHR; was 88334-8 (Calculated)
    ('body_temperature', 'Body temperature', '\u00b0C', 'Measurement', 'LOINC', '8310-5'),
    ('skin_temperature', 'Skin temperature', '\u00b0C', 'Measurement', 'LOINC', '39106-0'),
    ('waist_circumference', 'Waist circumference', 'cm', 'Measurement', 'LOINC', '8280-0'),
    ('body_fat_mass', 'Body fat mass', 'kg', 'Measurement', 'LOINC', '73708-0'),
    ('skeletal_muscle_mass', 'Skeletal muscle mass', 'kg', 'Measurement', None, None),
    ('skin_temperature_deviation', 'Skin temperature deviation from baseline', '\u00b0C', 'Measurement', None, None),
    ('skin_temperature_trend_deviation', 'Skin temperature trend deviation', '\u00b0C', 'Measurement', None, None),

    # ── Fitness Metrics ─────────────────────────────────────────────────
    ('vo2_max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9'),
    ('six_minute_walk_test_distance', '6-minute walk test distance', 'm', 'Measurement', 'LOINC', '64098-7'),
    ('cardiovascular_age', 'Cardiovascular age estimate', 'years', 'Measurement', 'HK-Wearable', 'HK-WEAR-OW-CARDIO-AGE'),

    # ── Activity — Basic ────────────────────────────────────────────────
    ('steps', 'Steps', 'count', 'Observation', 'LOINC', '55423-8'),
    ('energy', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1'),
    ('basal_energy', 'Basal energy expenditure', 'kcal', 'Measurement', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY'),
    ('stand_time', 'Stand time', 'min', 'Observation', None, None),
    ('exercise_time', 'Exercise time', 'min', 'Observation', 'LOINC', '55411-3'),
    ('physical_effort', 'Physical effort score', 'score', 'Observation', None, None),
    ('flights_climbed', 'Flights of stairs climbed', 'count', 'Observation', 'LOINC', '100304-5'),
    # SNOMED 16821004 "Metabolic requirement" is unverified as an average MET over an
    # interval; it stays unmapped until an expert has reviewed it.
    ('average_met', 'Average metabolic equivalent (MET)', 'MET', 'Observation', None, None),
    ('active_time', 'Active time', 'min', 'Observation', None, None),

    # ── Activity — Distance ─────────────────────────────────────────────
    ('distance_walking_running', 'Walking + running distance', 'm', 'Measurement', 'LOINC', '112427-0'),
    ('distance_cycling', 'Cycling distance', 'm', 'Measurement', 'LOINC', '93818-3'),
    ('distance_swimming', 'Swimming distance', 'm', 'Measurement', 'LOINC', '93816-7'),
    ('distance_downhill_snow_sports', 'Downhill snow sports distance', 'm', 'Measurement', 'LOINC', '103208-5'),
    ('distance_other', 'Other activity distance', 'm', 'Measurement', 'LOINC', '103208-5'),

    # ── Activity — Walking Metrics ──────────────────────────────────────
    ('walking_step_length', 'Walking step length', 'cm', 'Measurement', 'HK-Wearable', 'HK-WEAR-STEP-LENGTH'),
    ('walking_speed', 'Walking speed', 'm/s', 'Measurement', 'LOINC', '41957-2'),
    ('walking_double_support_percentage', 'Walking double support percentage', '%', 'Measurement', 'HK-Wearable', 'HK-WEAR-DBL-SUPPORT'),
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
    ('estimated_workout_effort_score', 'Estimated workout effort score', 'score', 'Observation', 'HK-Wearable', 'HK-WEAR-OW-WORKOUT-EFFORT'),

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
    ('elevation', 'Elevation', 'm', 'Measurement', 'LOINC', '41905-1'),
    ('latitude', 'Latitude', '\u00b0', 'Observation', None, None),
    ('longitude', 'Longitude', '\u00b0', 'Observation', None, None),
    ('air_temperature', 'Air temperature', '\u00b0C', 'Observation', 'SNOMED', '250825003'),

    # ── Provider-Specific — Garmin ──────────────────────────────────────
    ('garmin_stress_level', 'Stress level (Garmin)', 'score', 'Observation', None, None),
    ('garmin_skin_temperature', 'Skin temperature deviation (Garmin)', '\u00b0C', 'Measurement', 'LOINC', '39106-0'),
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
SEED_ORIGIN_SYSTEM = 'open-wearables-seed'


def _not_seed_owned(row):
    """Why ``--update-seeded`` must leave *row* alone, or '' when it may move it.

    A row is the seed's only while nothing else has set its destination or
    curated it. Suggest stamps its own ``origin_system`` when it proposes a
    destination, and every curator save records ``decision_surface`` and
    ``updated_by``. ``copy_reference_data`` clears ``updated_by``, so what only
    a person enters (a note, a unit, LOINC axes, SNOMED attributes) is checked
    too. A run Suggest declined leaves the destination empty and the row the
    seed's; ``_repoint`` drops the note that run left.
    """
    if row.status != 'proposed':
        return f'status is {row.status}'
    if row.origin != 'import' or row.origin_system != SEED_ORIGIN_SYSTEM:
        return f'destination set by {row.origin_system or row.origin or "another writer"}'
    if row.suggested_target_concept_id or row.suggestion_confidence is not None:
        return 'carries a Suggest answer'
    if (row.decision_surface or row.updated_by_id or row.reviewer_id or row.curator_note_at
            or row.destination_unit_concept_id or row.loinc_axes or row.snomed_attributes):
        return 'saved by a curator'
    return ''


def _repoint(row, target):
    """Move a seed-owned *row* to *target*. The caller holds the row lock."""
    row.target_concept = target
    row.destination_vocabulary_id = target.vocabulary_id
    fields = ['target_concept', 'destination_vocabulary_id', 'updated_at']
    if row.notes:
        # No curator wrote it (_not_seed_owned): it is the rationale of a
        # Suggest run that found nothing, and would now read beside a
        # destination as if it were about it.
        row.notes = ''
        fields.append('notes')
    row.save(update_fields=fields)


class Command(BaseCommand):
    help = 'Seed SourceCodeConceptMapping rows for OpenWearables metrics.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created or re-pointed without writing.',
        )
        parser.add_argument(
            '--update-seeded', action='store_true',
            help='Re-point existing rows whose target differs from the seed, '
                 'but only rows nothing but this command has written: proposed, '
                 'origin import / open-wearables-seed, no curator decision. '
                 'Never clears a destination.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        update_seeded = options['update_seeded']
        detail = self.stdout.write if options['verbosity'] >= 1 else (lambda _line: None)

        # Collect all target codes that need concept lookup, grouped by vocab.
        vocab_codes: dict[str, set[str]] = {}
        for _code, _name, _unit, _domain, t_vocab, t_code in OPENWEARABLES_METRICS:
            if t_vocab and t_code:
                vocab_codes.setdefault(t_vocab, set()).add(t_code)

        # Batch-resolve OMOP concepts from the concept table.
        concepts = {}
        for vocab, codes in vocab_codes.items():
            for c in Concept.objects.filter(
                vocabulary_id=vocab,
                concept_code__in=codes,
            ).only('concept_id', 'concept_code', 'vocabulary_id', 'domain_id',
                   'invalid_reason'):
                concepts[(vocab, c.concept_code)] = c
        # A concept a curator holds refuses every write that names it (#2390).
        locked = set(
            ConceptMappingLock.objects
            .filter(concept_id__in=[c.pk for c in concepts.values()])
            .values_list('concept_id', flat=True)
        )

        created = existed = unresolved = repointed = kept = 0
        left_alone = []  # (source_code, reason): the seed's target was not applied
        with transaction.atomic():
            existing = {
                row.source_code: row
                for row in SourceCodeConceptMapping.objects.filter(
                    organization=None, source_vocabulary_id=VOCABULARY_ID,
                ).select_related('target_concept')
            }
            for ow_code, display, unit, domain, t_vocab, t_code in OPENWEARABLES_METRICS:
                target = concepts.get((t_vocab, t_code)) if t_vocab else None
                desc = f'{display} ({unit})' if unit else display
                omop_table = DOMAIN_TO_TABLE.get(domain, '')
                wanted = f'{t_vocab or "-"}:{t_code or "-"}'

                unusable = ''
                if t_vocab and t_code and not target:
                    unusable = 'Concept not found'
                elif target and target.invalid_reason:
                    unusable = f'Concept is retired ({target.invalid_reason})'
                elif target and target.domain_id != domain:
                    # The row is filed by the seed's domain; a concept of
                    # another domain would sit in the wrong clinical table.
                    unusable = (f'Concept is in domain {target.domain_id}, '
                                f'the seed files the metric under {domain}')
                if unusable:
                    logger.warning('%s: %s %s for metric %s', unusable, t_vocab, t_code, ow_code)
                    detail(self.style.WARNING(f'  {unusable}: {wanted} for metric {ow_code}'))
                    target = None
                if not target:
                    unresolved += 1

                row = existing.get(ow_code)
                if row is None and dry_run:
                    created += 1
                    state = 'MAPPED' if target else 'UNMAPPED'
                    detail(f'  [CREATE {state}] {ow_code} -> {wanted}')
                    continue
                if row is None:
                    row, was_created = SourceCodeConceptMapping.objects.get_or_create(
                        organization=None,
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
                            'origin_system': SEED_ORIGIN_SYSTEM,
                            'source': 'HealthKey',
                            'occurrence_count': 0,
                        },
                    )
                    if was_created:
                        created += 1
                        continue

                existed += 1
                current = row.target_concept
                if (current.pk if current else None) == (target.pk if target else None):
                    continue
                held = (f'{current.vocabulary_id}:{current.concept_code}'
                        if current else '-:-')
                if not update_seeded:
                    if dry_run:
                        detail(
                            f'  [EXISTS, DIFFERS] {ow_code}: {held}, seed {wanted} '
                            f'(--update-seeded would consider it)')
                    continue
                if not t_code:
                    # A destination is never cleared.
                    kept += 1
                    detail(f'  [KEPT] {ow_code}: {held} (the seed names no target)')
                    continue
                if not target:
                    reason = f'seed target {wanted} is not usable here'
                elif target.pk in locked:
                    reason = f'{wanted} is locked by a curator'
                else:
                    reason = _not_seed_owned(row)
                if not reason and not dry_run:
                    # Re-read under a lock: a curator may have saved or deleted
                    # the row since it was loaded above.
                    row = (SourceCodeConceptMapping.objects
                           .select_for_update().filter(pk=row.pk).first())
                    reason = _not_seed_owned(row) if row else 'row deleted during the run'
                if reason:
                    left_alone.append((ow_code, reason))
                    detail(f'  [LEFT ALONE] {ow_code}: {held}, seed {wanted} ({reason})')
                    continue
                repointed += 1
                detail(f'  [RE-POINT] {ow_code}: {held} -> {wanted}')
                if not dry_run:
                    _repoint(row, target)

        total = len(OPENWEARABLES_METRICS)
        prefix = 'DRY RUN: would have ' if dry_run else 'Done: '
        summary = (
            f'{prefix}{created} created, {existed} already existed, '
            f'{unresolved} without target concept (of {total} total).'
        )
        if update_seeded:
            summary += (
                f' --update-seeded: {repointed} re-pointed, '
                f'{len(left_alone)} left alone at another destination, '
                f'{kept} kept where the seed names no target.'
            )
        self.stdout.write(
            self.style.WARNING(summary) if dry_run else self.style.SUCCESS(summary))
