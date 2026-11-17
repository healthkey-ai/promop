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

Each metric's unit is seeded too, as the mapping's destination unit: the UCUM
concept for the unit the proposal names (``UCUM_BY_UNIT``). ``--update-seeded``
fills a unit that is still blank on a proposed row no curator has saved, and
never changes one that is set.

The same unit is recorded as the row's source unit (``source_unit_evidence``),
which is what the list's Units column shows. ``--update-seeded`` fills it on
any row that has none.

Three things the proposal says about the metric itself are seeded too: its
category (``OPENWEARABLES_CATEGORIES``), its measure class
(``OPENWEARABLES_MEASURE_CLASSES``: how the source produces the value) and its
internal metric key (``OPENWEARABLES_METRIC_KEYS``: the vendor-neutral name the
Apple, Garmin and HL7 PHR codes for the same metric share). They are the
mapping's ``category``, ``measure_class`` and ``metric_key``, the first two
columns of the Wearables tab. ``--update-seeded`` fills any of them on any row
where it is blank, whatever the row's status, and never changes one that is set.

``seed_hl7_phr_mappings`` runs this same command over the HL7 PHR metrics.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

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


def by_code(codes_by_value):
    """``{source_code: value}`` from ``{value: (source_code, ...)}``."""
    return {code: value for value, codes in codes_by_value.items() for code in codes}


# Each metric's category in the wearable mapping proposal (its Category
# column), kept beside the metric list rather than inside its tuples. Seeded as
# the mapping's ``category``; a test holds every metric to one of
# source_vocabularies.WEARABLE_METRIC_CATEGORIES.
OPENWEARABLES_CATEGORIES = by_code({
    'Activity': (
        'steps', 'exercise_time', 'distance_walking_running', 'energy', 'basal_energy',
        'flights_climbed', 'distance_cycling', 'distance_swimming',
        'distance_downhill_snow_sports', 'distance_other', 'estimated_workout_effort_score',
        'elevation', 'average_met', 'stand_time', 'physical_effort', 'active_time', 'cadence',
        'power', 'speed', 'workout_effort_score', 'running_power', 'running_speed',
        'running_vertical_oscillation', 'running_ground_contact_time', 'running_stride_length',
        'running_vertical_ratio', 'running_stance_time_balance', 'swimming_stroke_count',
        'underwater_depth', 'push_count', 'nike_fuel',
    ),
    'Heart & Cardiovascular': (
        'resting_heart_rate', 'heart_rate_variability_sdnn', 'heart_rate_variability_rmssd',
        'walking_heart_rate_average', 'heart_rate', 'heart_rate_recovery_one_minute',
        'atrial_fibrillation_burden',
    ),
    'Blood & Respiratory': (
        'oxygen_saturation', 'respiratory_rate', 'blood_glucose', 'cgm_glucose',
        'blood_pressure_systolic', 'blood_pressure_diastolic', 'forced_vital_capacity',
        'forced_expiratory_volume_1', 'peak_expiratory_flow_rate',
        'breathing_disturbance_index', 'blood_alcohol_content', 'peripheral_perfusion_index',
    ),
    'Body Composition': (
        'weight', 'height', 'body_fat_percentage', 'body_mass_index', 'lean_body_mass',
        'body_temperature', 'skin_temperature', 'waist_circumference', 'body_fat_mass',
        'skin_temperature_deviation', 'skin_temperature_trend_deviation',
        'skeletal_muscle_mass',
    ),
    'Fitness': ('vo2_max', 'six_minute_walk_test_distance', 'cardiovascular_age'),
    'Sleep': ('sleeping_breathing_disturbances',),
    'Mobility & Gait': (
        'walking_speed', 'walking_step_length', 'walking_double_support_percentage',
        'walking_asymmetry_percentage', 'walking_steadiness', 'stair_descent_speed',
        'stair_ascent_speed',
    ),
    'Environmental': (
        'air_temperature', 'environmental_audio_exposure', 'headphone_audio_exposure',
        'environmental_sound_reduction', 'time_in_daylight', 'water_temperature',
        'uv_exposure', 'weather_temperature', 'weather_humidity',
    ),
    'Stress & Autonomic': ('garmin_stress_level', 'electrodermal_activity'),
    'Garmin-specific': (
        'garmin_body_battery', 'garmin_fitness_age', 'garmin_skin_temperature',
    ),
    'Medication & Device Use': ('inhaler_usage', 'insulin_delivery'),
    'Safety': ('number_of_times_fallen',),
    'Self-reported': ('number_of_alcoholic_beverages', 'hydration'),
    'Location': ('latitude', 'longitude'),
})

# How the source produces each metric's value (the proposal's Measure Class
# column), seeded as the mapping's ``measure_class``: one of
# source_vocabularies.WEARABLE_MEASURE_CLASSES. A property of the source
# metric, so two sources of one destination concept may differ.
OPENWEARABLES_MEASURE_CLASSES = by_code({
    'Direct measurement': (
        'oxygen_saturation', 'respiratory_rate', 'weight', 'heart_rate', 'blood_glucose',
        'cgm_glucose', 'blood_pressure_systolic', 'blood_pressure_diastolic', 'height',
        'body_temperature', 'skin_temperature', 'waist_circumference', 'forced_vital_capacity',
        'forced_expiratory_volume_1', 'peak_expiratory_flow_rate', 'elevation',
        'garmin_skin_temperature', 'peripheral_perfusion_index', 'cadence', 'power', 'speed',
        'running_power', 'running_speed', 'underwater_depth', 'electrodermal_activity',
    ),
    'Aggregate over interval': (
        'steps', 'exercise_time', 'resting_heart_rate', 'distance_walking_running',
        'heart_rate_variability_sdnn', 'heart_rate_variability_rmssd',
        'walking_heart_rate_average', 'flights_climbed', 'breathing_disturbance_index',
        'six_minute_walk_test_distance', 'distance_cycling', 'distance_swimming',
        'distance_downhill_snow_sports', 'distance_other', 'heart_rate_recovery_one_minute',
        'sleeping_breathing_disturbances', 'stand_time', 'swimming_stroke_count',
        'time_in_daylight', 'uv_exposure', 'inhaler_usage', 'push_count',
        'atrial_fibrillation_burden', 'number_of_times_fallen',
    ),
    'Algorithmic estimate': (
        'vo2_max', 'energy', 'basal_energy', 'walking_speed', 'walking_step_length',
        'walking_double_support_percentage', 'body_fat_percentage', 'body_mass_index',
        'lean_body_mass', 'body_fat_mass', 'average_met', 'walking_asymmetry_percentage',
        'stair_descent_speed', 'stair_ascent_speed', 'skin_temperature_deviation',
        'skin_temperature_trend_deviation', 'skeletal_muscle_mass',
        'running_vertical_oscillation', 'running_ground_contact_time', 'running_stride_length',
        'running_vertical_ratio', 'running_stance_time_balance',
    ),
    'Vendor score': (
        'estimated_workout_effort_score', 'cardiovascular_age', 'garmin_stress_level',
        'garmin_body_battery', 'garmin_fitness_age', 'walking_steadiness', 'physical_effort',
        'workout_effort_score', 'nike_fuel',
    ),
    'Environmental context': (
        'air_temperature', 'environmental_audio_exposure', 'headphone_audio_exposure',
        'environmental_sound_reduction', 'water_temperature', 'weather_temperature',
        'weather_humidity', 'latitude', 'longitude',
    ),
    'User-entered or device log': (
        'blood_alcohol_content', 'active_time', 'insulin_delivery',
        'number_of_alcoholic_beverages', 'hydration',
    ),
})

# The proposal's Internal Metric Key for the OpenWearables codes it names one
# for: the key the Apple and Garmin codes for the same metric carry
# (seed_wearable_device_mappings). Every other metric is keyed by its own code.
_METRIC_KEY_OVERRIDES = {
    'exercise_time': 'active_minutes',
    'resting_heart_rate': 'resting_hr',
    'oxygen_saturation': 'spo2',
    'distance_walking_running': 'distance',
    'energy': 'active_energy',
    'heart_rate_variability_sdnn': 'hrv_sdnn',
    'heart_rate_variability_rmssd': 'hrv_rmssd',
    'walking_double_support_percentage': 'walking_double_support_pct',
    'walking_heart_rate_average': 'walking_hr_avg',
    'weight': 'body_mass',
}
# Seeded as the mapping's ``metric_key``: lowercase snake case
# (source_vocabularies.clean_metric_key), one key per metric.
OPENWEARABLES_METRIC_KEYS = {
    code: _METRIC_KEY_OVERRIDES.get(code, code) for code, *_rest in OPENWEARABLES_METRICS
}

# The UCUM code for each display unit the metric lists use (the proposal's
# "UCUM Unit" column). A unit with no UCUM concept loaded here is left unset.
UCUM_BY_UNIT = {
    '%': '%', '/h': '/h', 'L': 'L', 'L/min': 'L/min', 'MET': '[MET]', 'U': '[IU]',
    'W': 'W', 'bpm': '/min', 'breaths/min': '/min', 'steps/min': '/min', 'rpm': '/min',
    'cm': 'cm', 'count': '1', 'dB': 'dB', 'g': 'g', 'h': 'h', 'kcal': 'kcal', 'kg': 'kg',
    'kg/m2': 'kg/m2', 'm': 'm', 'm/s': 'm/s', 'mL': 'mL', 'mL/kg/min': 'mL/min/kg',
    'mg': 'mg', 'mg/dL': 'mg/dL', 'min': 'min', 'mmHg': 'mm[Hg]', 'ms': 'ms',
    'score': '{score}', 'years': 'a', '\u00b0': 'deg', '\u00b0C': 'Cel', '\u03bcS': 'uS',
    # Units only the Apple and Garmin adapters report in.
    'km': 'km', 'km/hr': 'km/h',
}
UCUM_SYSTEM = 'http://unitsofmeasure.org'

# Metrics that report a *difference* in temperature. Their source unit is
# recorded, but they get no destination unit: an approved mapping's unit is a
# conversion target, and the temperature conversion is for absolute readings
# (0.5 degF as a reading is -17.5 Cel; as a difference it is 0.28).
NO_DESTINATION_UNIT = frozenset({
    'skin_temperature_deviation', 'skin_temperature_trend_deviation',
    'garmin_skin_temperature',
})


def unit_evidence(unit):
    """``source_unit_evidence`` for a metric its source reports in *unit*.

    One entry, with the UCUM code where the unit has one. It carries no
    ``count`` (it is not a sighting, so the approval unit check ignores it) and
    no ``source`` (so a rebuild from patient data keeps it).
    """
    if not unit:
        return []
    code = UCUM_BY_UNIT.get(unit)
    if code:
        return [{'code': code, 'display': unit, 'system': UCUM_SYSTEM}]
    return [{'display': unit}]


def _curator_saved(row):
    """Whether a person has saved *row*, whatever they changed on it."""
    return bool(row.decision_surface or row.updated_by_id or row.reviewer_id
                or row.curator_note_at)


def _not_seed_owned(row, origin_system=SEED_ORIGIN_SYSTEM, seed_unit_ids=()):
    """Why ``--update-seeded`` must leave *row* alone, or '' when it may move it.

    A row is the seed's only while nothing else has set its destination or
    curated it. Suggest stamps its own ``origin_system`` when it proposes a
    destination, and every curator save records ``decision_surface`` and
    ``updated_by``. ``copy_reference_data`` clears ``updated_by``, so what only
    a person enters (a note, a unit, LOINC axes, SNOMED attributes) is checked
    too. A unit the seed itself assigns (*seed_unit_ids*) is not a person's. A run
    Suggest declined leaves the destination empty and the row the seed's;
    ``_repoint`` drops the note that run left.
    """
    if row.status != 'proposed':
        return f'status is {row.status}'
    if row.origin != 'import' or row.origin_system != origin_system:
        return f'destination set by {row.origin_system or row.origin or "another writer"}'
    if row.suggested_target_concept_id or row.suggestion_confidence is not None:
        return 'carries a Suggest answer'
    curated_unit = (row.destination_unit_concept_id is not None
                    and row.destination_unit_concept_id not in seed_unit_ids)
    if _curator_saved(row) or curated_unit or row.loinc_axes or row.snomed_attributes:
        return 'saved by a curator'
    return ''


def _unit_not_fillable(row):
    """Why ``--update-seeded`` must not set *row*'s unit, or '' when it may.

    Filling a blank unit overrides nothing, so the row need not be the seed's:
    a destination Suggest proposed is still a proposal about this metric. A
    unit that is set is never changed, and neither is a row somebody decided
    (approved or rejected) or saved. Axes and attributes count as a save for
    the reason ``_not_seed_owned`` gives: on a copied instance they are the only
    trace of the curator who entered them.
    """
    if row.destination_unit_concept_id is not None:
        return 'a unit is already set'
    if row.status != 'proposed':
        return f'status is {row.status}'
    if _curator_saved(row) or row.loinc_axes or row.snomed_attributes:
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

    # What a run seeds. seed_hl7_phr_mappings overrides all six.
    vocabulary_id = VOCABULARY_ID
    origin_system = SEED_ORIGIN_SYSTEM
    metrics = OPENWEARABLES_METRICS
    categories = OPENWEARABLES_CATEGORIES
    measure_classes = OPENWEARABLES_MEASURE_CLASSES
    metric_keys = OPENWEARABLES_METRIC_KEYS

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created or re-pointed without writing.',
        )
        parser.add_argument(
            '--update-seeded', action='store_true',
            help='Re-point existing rows whose target differs from the seed, '
                 'but only rows nothing but this command has written: proposed, '
                 'origin import / this seed, no curator decision. Never clears '
                 'a destination. Also fills a blank unit on a proposed row no '
                 'curator has saved, and a blank source unit, category, '
                 'measure class or metric key on any row.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        update_seeded = options['update_seeded']
        detail = self.stdout.write if options['verbosity'] >= 1 else (lambda _line: None)

        # Collect all target codes that need concept lookup, grouped by vocab.
        vocab_codes: dict[str, set[str]] = {}
        for _code, _name, _unit, _domain, t_vocab, t_code in self.metrics:
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
        # The unit each metric is recorded in, as a UCUM concept.
        ucum_codes = {UCUM_BY_UNIT[unit] for _c, _n, unit, *_rest in self.metrics
                      if unit in UCUM_BY_UNIT}
        units = {
            c.concept_code: c for c in Concept.objects.filter(
                vocabulary_id='UCUM', concept_code__in=ucum_codes,
                standard_concept='S', invalid_reason__isnull=True,
            ).only('concept_id', 'concept_code')
        }
        seed_unit_ids = {c.pk for c in units.values()}
        created = existed = unresolved = repointed = kept = units_filled = 0
        source_units_filled = fillable = 0
        described_filled = {'category': 0, 'measure_class': 0, 'metric_key': 0}
        describable = 0
        without_unit = sorted({unit for _c, _n, unit, *_rest in self.metrics
                               if unit and UCUM_BY_UNIT.get(unit) not in units})
        left_alone = []  # (source_code, reason): the seed's target was not applied
        with transaction.atomic():
            existing = {
                row.source_code: row
                for row in SourceCodeConceptMapping.objects.filter(
                    organization=None, source_vocabulary_id=self.vocabulary_id,
                ).select_related('target_concept')
            }
            # A concept a curator holds refuses every write that names it
            # (#2390): the seed's targets, and the destinations rows have now.
            locked = set(
                ConceptMappingLock.objects
                .filter(concept_id__in={
                    *(c.pk for c in concepts.values()),
                    *(r.target_concept_id for r in existing.values() if r.target_concept_id),
                })
                .values_list('concept_id', flat=True)
            )
            for ow_code, display, unit, domain, t_vocab, t_code in self.metrics:
                target = concepts.get((t_vocab, t_code)) if t_vocab else None
                unit_concept = (None if ow_code in NO_DESTINATION_UNIT
                                else units.get(UCUM_BY_UNIT.get(unit)))
                evidence = unit_evidence(unit)
                # What the proposal says about the metric itself.
                described = (
                    ('category', self.categories.get(ow_code, ''), 'CATEGORY'),
                    ('measure_class', self.measure_classes.get(ow_code, ''), 'MEASURE CLASS'),
                    ('metric_key', self.metric_keys.get(ow_code, ''), 'METRIC KEY'),
                )
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
                        source_vocabulary_id=self.vocabulary_id,
                        source_code=ow_code,
                        defaults={
                            'domain_id': domain,
                            'source_code_description': desc[:255],
                            'target_concept': target,
                            'destination_vocabulary_id': target.vocabulary_id if target else '',
                            'destination_unit_concept': unit_concept,
                            'source_unit_evidence': evidence,
                            **{field: value for field, value, _tag in described},
                            'omop_table': omop_table,
                            'status': 'proposed',
                            'origin': 'import',
                            'origin_system': self.origin_system,
                            'source': 'HealthKey',
                            'occurrence_count': 0,
                        },
                    )
                    if was_created:
                        created += 1
                        continue

                existed += 1
                if update_seeded and evidence and not row.source_unit_evidence:
                    # The metric's own unit, on a row that records none: nothing
                    # is overridden, so status and ownership do not matter. Like
                    # every sighting it leaves updated_at alone.
                    if dry_run or SourceCodeConceptMapping.objects.filter(
                            pk=row.pk, source_unit_evidence=[]).update(
                                source_unit_evidence=evidence):
                        source_units_filled += 1
                        row.source_unit_evidence = evidence
                        detail(f'  [SOURCE UNIT] {ow_code}: '
                               f'{evidence[0].get("code") or evidence[0]["display"]}')
                blank = [(field, value, tag) for field, value, tag in described
                         if value and not getattr(row, field)]
                if blank and not update_seeded:
                    describable += 1
                for field, value, tag in blank if update_seeded else ():
                    # The metric's own category, measure class or key, on a row
                    # that has none: nothing is overridden, so status and
                    # ownership do not matter. The list API returns them, so
                    # updated_at is stamped for a reader following the delta.
                    if dry_run or SourceCodeConceptMapping.objects.filter(
                            pk=row.pk, **{field: ''}).update(
                                **{field: value, 'updated_at': timezone.now()}):
                        described_filled[field] += 1
                        setattr(row, field, value)
                        detail(f'  [{tag}] {ow_code}: {value}')
                if (update_seeded and unit_concept is not None
                        and not _unit_not_fillable(row)
                        and row.target_concept_id in locked):
                    detail(f'  [UNIT LEFT ALONE] {ow_code}: its destination is locked by a curator')
                elif (update_seeded and unit_concept is not None
                        and not _unit_not_fillable(row)):
                    if not dry_run:
                        # Re-read under a lock, as the re-point below does.
                        locked_row = (SourceCodeConceptMapping.objects
                                      .select_for_update(of=('self',))
                                      .select_related('target_concept').filter(pk=row.pk).first())
                        if (locked_row is None or _unit_not_fillable(locked_row)
                                or locked_row.target_concept_id != row.target_concept_id):
                            # Deleted, saved, or moved to a destination whose
                            # lock was not checked above.
                            locked_row = None
                        else:
                            locked_row.destination_unit_concept = unit_concept
                            locked_row.save(update_fields=['destination_unit_concept', 'updated_at'])
                            row = existing[ow_code] = locked_row
                    if dry_run or locked_row is not None:
                        units_filled += 1
                        detail(f'  [UNIT] {ow_code}: {unit_concept.concept_code}')
                if not update_seeded and (
                        (evidence and not row.source_unit_evidence)
                        or (unit_concept is not None and not _unit_not_fillable(row))):
                    fillable += 1
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
                    reason = _not_seed_owned(row, self.origin_system, seed_unit_ids)
                if not reason and not dry_run:
                    # Re-read under a lock: a curator may have saved or deleted
                    # the row since it was loaded above.
                    row = (SourceCodeConceptMapping.objects
                           .select_for_update().filter(pk=row.pk).first())
                    reason = (_not_seed_owned(row, self.origin_system, seed_unit_ids)
                              if row else 'row deleted during the run')
                if reason:
                    left_alone.append((ow_code, reason))
                    detail(f'  [LEFT ALONE] {ow_code}: {held}, seed {wanted} ({reason})')
                    continue
                repointed += 1
                detail(f'  [RE-POINT] {ow_code}: {held} -> {wanted}')
                if not dry_run:
                    _repoint(row, target)

        if without_unit:
            detail(self.style.WARNING(
                '  No UCUM concept loaded for: '
                + ', '.join(f'{unit} ({UCUM_BY_UNIT.get(unit, "no UCUM code")})'
                            for unit in without_unit)
                + ' (destination unit left unset)'))
        total = len(self.metrics)
        prefix = 'DRY RUN: would have ' if dry_run else 'Done: '
        summary = (
            f'{prefix}{created} created, {existed} already existed, '
            f'{unresolved} without target concept (of {total} total).'
        )
        if update_seeded:
            summary += (
                f' --update-seeded: {repointed} re-pointed, '
                f'{len(left_alone)} left alone at another destination, '
                f'{kept} kept where the seed names no target, '
                f'{units_filled} units filled, '
                f'{source_units_filled} source units filled, '
                f"{described_filled['category']} categories filled, "
                f"{described_filled['measure_class']} measure classes filled, "
                f"{described_filled['metric_key']} metric keys filled."
            )
        if fillable:
            summary += (f' {fillable} existing rows have no unit or source unit; '
                        '--update-seeded would fill them.')
        if describable:
            summary += (f' {describable} existing rows have no category, measure '
                        'class or metric key; --update-seeded would fill it.')
        self.stdout.write(
            self.style.WARNING(summary) if dry_run else self.style.SUCCESS(summary))
