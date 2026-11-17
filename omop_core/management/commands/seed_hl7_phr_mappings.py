"""Seed SourceCodeConceptMapping rows for the HL7 PHR metrics.

The HL7 PHR Code Mapping table gives each PHR code value the Apple HealthKit
type and the Android Health Connect record it is mapped from. This command
seeds all three as proposed mappings with provenance ``HL7 PHR``, since the
table is where each code's association with a metric comes from:

- ``HL7-PHR``: the HL7 PHR code value (``stepCount``),
- ``Apple``: the HealthKit identifier (``HKQuantityTypeIdentifierStepCount``),
  except the codes ``seed_wearable_device_mappings`` seeds as approved, which
  this command never writes,
- ``HealthConnect``: the Health Connect record type (``StepsRecord``).

``HL7_PHR_METRICS`` is the first list of HL7 PHR codes, for metrics no device
vocabulary carried. ``PHR_SOURCE_METRICS`` is the wearable metrics of the
mapping proposal, one row per metric with its code in each vocabulary. A row's
destination is the proposal's target for the metric where it names one that is
loaded here, standard (or a local HK-Wearable concept) and not retired; the
rest are left for curator review. Nothing is approved.

Only wearable metrics are seeded for HealthKit and Health Connect: the
proposal's Nutrition, Symptoms, Reproductive Health, Self-care, Mental
Wellbeing and Hearing rows, ``food`` and ``medication_adherence`` are left
out of ``PHR_SOURCE_METRICS``.

This is ``seed_openwearables_mappings`` over other metric lists: the same
idempotence, the same ``--dry-run`` and ``--update-seeded`` rules, the same
unit, category, measure class and metric key seeding, once per vocabulary,
in one transaction.
"""

from django.db import transaction

from omop_core.management.commands.seed_openwearables_mappings import (
    Command as WearableSeedCommand,
    by_code,
)
from omop_core.management.commands.seed_wearable_device_mappings import APPLE_METRIC_KEYS

VOCABULARY_ID = 'HL7-PHR'
APPLE_VOCABULARY_ID = 'Apple'
HEALTH_CONNECT_VOCABULARY_ID = 'HealthConnect'
SEED_ORIGIN_SYSTEM = 'HL7 PHR'

# (source_code, display_name, unit, domain, target_vocab, target_code)
# source_code is the HL7 PHR code value. target_* are None where the HL7 PHR
# table names no LOINC code.
HL7_PHR_METRICS = [
    # -- LOINC named by the HL7 PHR table ---------------------------------
    ('totalEnergyBurned', 'Total energy burned', 'kcal', 'Measurement', 'LOINC', '41981-2'),
    ('dietaryCalcium', 'Dietary calcium', 'mg', 'Measurement', 'LOINC', '9045-6'),
    ('dietaryCarbohydrates', 'Dietary carbohydrates', 'g', 'Measurement', 'LOINC', '9059-7'),
    ('dietaryEnergyConsumed', 'Dietary energy consumed', 'kcal', 'Measurement', 'LOINC', '9052-2'),
    ('dietaryFatTotal', 'Dietary fat, total', 'g', 'Measurement', 'LOINC', '9066-2'),
    ('dietaryProtein', 'Dietary protein', 'g', 'Measurement', 'LOINC', '9079-5'),
    ('dietarySodium', 'Dietary sodium', 'mg', 'Measurement', 'LOINC', '9086-0'),

    # -- No LOINC in the HL7 PHR table ------------------------------------
    ('stepsCadence', 'Steps cadence', 'steps/min', 'Measurement', None, None),
    ('rrInterval', 'R-R interval', 'ms', 'Measurement', None, None),
    ('highHeartRateEvent', 'High heart rate event', 'count', 'Observation', None, None),
    ('lowHeartRateEvent', 'Low heart rate event', 'count', 'Observation', None, None),
    ('irregularHeartRhythmEvent', 'Irregular heart rhythm event', 'count', 'Observation', None, None),
    ('basalBodyTemperature', 'Basal body temperature', '\u00b0C', 'Measurement', None, None),
    ('hipCircumference', 'Hip circumference', 'cm', 'Measurement', None, None),
    ('boneMass', 'Bone mass', 'kg', 'Measurement', None, None),
    ('sleepArousalIndex', 'Sleep arousal index', '/h', 'Measurement', None, None),
    ('snoreIndex', 'Snore index', '/h', 'Measurement', None, None),
]

# Each metric's category in the wearable mapping proposal, as
# OPENWEARABLES_CATEGORIES is for the OpenWearables metrics.
HL7_PHR_CATEGORIES = by_code({
    'Activity': ('totalEnergyBurned', 'stepsCadence'),
    'Heart & Cardiovascular': (
        'rrInterval', 'highHeartRateEvent', 'lowHeartRateEvent', 'irregularHeartRhythmEvent',
    ),
    'Body Composition': ('basalBodyTemperature', 'hipCircumference', 'boneMass'),
    'Sleep': ('sleepArousalIndex', 'snoreIndex'),
    'Nutrition': (
        'dietaryCalcium', 'dietaryCarbohydrates', 'dietaryEnergyConsumed', 'dietaryFatTotal',
        'dietaryProtein', 'dietarySodium',
    ),
})

# How the source produces each metric's value, as
# OPENWEARABLES_MEASURE_CLASSES is for the OpenWearables metrics.
HL7_PHR_MEASURE_CLASSES = by_code({
    'Direct measurement': ('rrInterval', 'basalBodyTemperature', 'hipCircumference'),
    'Aggregate over interval': (
        'stepsCadence', 'highHeartRateEvent', 'lowHeartRateEvent', 'irregularHeartRhythmEvent',
        'sleepArousalIndex', 'snoreIndex',
    ),
    'Algorithmic estimate': ('totalEnergyBurned', 'boneMass'),
    'User-entered or device log': (
        'dietaryCalcium', 'dietaryCarbohydrates', 'dietaryEnergyConsumed', 'dietaryFatTotal',
        'dietaryProtein', 'dietarySodium',
    ),
})

# The proposal's Internal Metric Key for each code: the vendor-neutral name a
# device code for the same metric would share.
HL7_PHR_METRIC_KEYS = {
    'totalEnergyBurned': 'total_energy_burned',
    'dietaryCalcium': 'dietary_calcium',
    'dietaryCarbohydrates': 'dietary_carbohydrates',
    'dietaryEnergyConsumed': 'dietary_energy_consumed',
    'dietaryFatTotal': 'dietary_fat_total',
    'dietaryProtein': 'dietary_protein',
    'dietarySodium': 'dietary_sodium',
    'stepsCadence': 'steps_cadence',
    'rrInterval': 'rr_interval',
    'highHeartRateEvent': 'high_heart_rate_event',
    'lowHeartRateEvent': 'low_heart_rate_event',
    'irregularHeartRhythmEvent': 'irregular_heart_rhythm_event',
    'basalBodyTemperature': 'basal_body_temperature',
    'hipCircumference': 'hip_circumference',
    'boneMass': 'bone_mass',
    'sleepArousalIndex': 'sleep_arousal_index',
    'snoreIndex': 'snore_index',
}


# The wearable metrics of the mapping proposal that the HL7 PHR table gives a
# code in at least one of the three vocabularies, in the proposal's order.
# (metric_key, display_name, unit, domain, target_vocab, target_code,
#  hl7_phr_code, healthkit_identifier, health_connect_record)
# A code is None where the proposal names no single code for the metric: a
# blank cell, or an HL7 cell that is a note ("sleepAHI (related)"), a pair
# ("cyclingCadence / stepsCadence") or a FHIR profile rather than a code value.
# unit is '' where the proposal names none; target_* are None where it names
# no target. metric_key is the proposal's Internal Metric Key, or the key
# OPENWEARABLES_METRIC_KEYS gives the metric's OpenWearables code where the
# proposal has none, so every code for one metric carries one key.
PHR_SOURCE_METRICS = [
    # -- The proposal's keyed metrics --
    ('steps', 'Steps', 'count', 'Observation', 'LOINC', '55423-8',
     'stepCount', 'HKQuantityTypeIdentifierStepCount', 'StepsRecord'),
    ('active_minutes', 'Exercise time', 'min', 'Observation', 'LOINC', '55411-3',
     'appleExerciseTime', 'HKQuantityTypeIdentifierAppleExerciseTime', None),
    ('resting_hr', 'Resting heart rate', 'bpm', 'Measurement', 'LOINC', '40443-4',
     'restingHeartRate', 'HKQuantityTypeIdentifierRestingHeartRate', 'RestingHeartRateRecord'),
    ('spo2', 'Oxygen saturation (SpO2)', '%', 'Measurement', 'LOINC', '59408-5',
     'oxygenSaturation', 'HKQuantityTypeIdentifierOxygenSaturation', 'OxygenSaturationRecord'),
    ('respiratory_rate', 'Respiratory rate', 'breaths/min', 'Measurement', 'LOINC', '9279-1',
     'respiratoryRate', 'HKQuantityTypeIdentifierRespiratoryRate', 'RespiratoryRateRecord'),
    ('vo2_max', 'VO2 max', 'mL/kg/min', 'Measurement', 'LOINC', '94122-9',
     'vo2Max', 'HKQuantityTypeIdentifierVO2Max', 'Vo2MaxRecord'),
    ('distance', 'Walking + running distance', 'm', 'Measurement', 'LOINC', '112427-0',
     'distanceWalkingRunning', 'HKQuantityTypeIdentifierDistanceWalkingRunning', None),
    ('active_energy', 'Active energy burned', 'kcal', 'Measurement', 'LOINC', '93819-1',
     'activeEnergyBurned', 'HKQuantityTypeIdentifierActiveEnergyBurned', 'ActiveCaloriesBurnedRecord'),
    ('sleep_duration', 'Sleep duration', 'h', 'Observation', 'LOINC', '93832-4',
     'sleepAnalysis', 'HKCategoryTypeIdentifierSleepAnalysis', 'SleepSessionRecord'),
    ('basal_energy', 'Basal energy expenditure', 'kcal', 'Measurement', 'HK-Wearable', 'HK-WEAR-BASAL-ENERGY',
     'basalEnergyBurned', 'HKQuantityTypeIdentifierBasalEnergyBurned', 'BasalMetabolicRateRecord'),
    ('hrv_sdnn', 'Heart rate variability (SDNN)', 'ms', 'Measurement', 'LOINC', '80404-7',
     'heartRateVariabilitySDNN', 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN', None),
    ('walking_speed', 'Walking speed', 'm/s', 'Measurement', 'LOINC', '41957-2',
     'walkingSpeed', 'HKQuantityTypeIdentifierWalkingSpeed', None),
    ('walking_step_length', 'Walking step length', 'cm', 'Measurement', 'HK-Wearable', 'HK-WEAR-STEP-LENGTH',
     'walkingStepLength', 'HKQuantityTypeIdentifierWalkingStepLength', None),
    ('walking_double_support_pct', 'Walking double support percentage', '%', 'Measurement', 'HK-Wearable', 'HK-WEAR-DBL-SUPPORT',
     'walkingDoubleSupportPercentage', 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage', None),
    ('walking_hr_avg', 'Walking heart rate average', 'bpm', 'Measurement', 'HK-Wearable', 'HK-WEAR-WALK-HR',
     'walkingHeartRateAverage', 'HKQuantityTypeIdentifierWalkingHeartRateAverage', None),
    ('flights_climbed', 'Flights of stairs climbed', 'count', 'Observation', 'LOINC', '100304-5',
     'flightsClimbed', 'HKQuantityTypeIdentifierFlightsClimbed', 'FloorsClimbedRecord'),
    ('body_mass', 'Body weight', 'kg', 'Measurement', 'LOINC', '29463-7',
     'bodyMass', 'HKQuantityTypeIdentifierBodyMass', 'WeightRecord'),
    ('heart_rate', 'Heart rate', 'bpm', 'Measurement', 'LOINC', '8867-4',
     'heartRate', 'HKQuantityTypeIdentifierHeartRate', 'HeartRateRecord'),
    # The HL7 table has one glucose code, which the proposal also lists against
    # cgm_glucose. One code is one mapping: it is seeded here, for blood glucose.
    ('blood_glucose', 'Blood glucose', 'mg/dL', 'Measurement', 'LOINC', '2339-0',
     'bloodGlucose', 'HKQuantityTypeIdentifierBloodGlucose', 'BloodGlucoseRecord'),
    ('blood_pressure_systolic', 'Systolic blood pressure', 'mmHg', 'Measurement', 'LOINC', '8480-6',
     'bloodPressureSystolic', 'HKQuantityTypeIdentifierBloodPressureSystolic', None),
    ('blood_pressure_diastolic', 'Diastolic blood pressure', 'mmHg', 'Measurement', 'LOINC', '8462-4',
     'bloodPressureDiastolic', 'HKQuantityTypeIdentifierBloodPressureDiastolic', None),
    ('height', 'Body height', 'cm', 'Measurement', 'LOINC', '8302-2',
     'height', 'HKQuantityTypeIdentifierHeight', 'HeightRecord'),
    ('body_fat_percentage', 'Body fat percentage', '%', 'Measurement', 'LOINC', '41982-0',
     'bodyFatPercentage', 'HKQuantityTypeIdentifierBodyFatPercentage', 'BodyFatRecord'),
    ('body_mass_index', 'Body mass index (BMI)', 'kg/m2', 'Measurement', 'LOINC', '39156-5',
     'bodyMassIndex', 'HKQuantityTypeIdentifierBodyMassIndex', None),
    ('lean_body_mass', 'Lean body mass', 'kg', 'Measurement', 'LOINC', '91557-9',
     'leanBodyMass', 'HKQuantityTypeIdentifierLeanBodyMass', 'LeanBodyMassRecord'),
    ('body_temperature', 'Body temperature', '\u00b0C', 'Measurement', 'LOINC', '8310-5',
     'bodyTemperature', 'HKQuantityTypeIdentifierBodyTemperature', 'BodyTemperatureRecord'),
    ('waist_circumference', 'Waist circumference', 'cm', 'Measurement', 'LOINC', '8280-0',
     'waistCircumference', 'HKQuantityTypeIdentifierWaistCircumference', 'WaistCircumferenceRecord'),
    ('forced_vital_capacity', 'Forced vital capacity (FVC)', 'L', 'Measurement', 'LOINC', '19870-5',
     'forcedVitalCapacity', 'HKQuantityTypeIdentifierForcedVitalCapacity', None),
    ('forced_expiratory_volume_1', 'Forced expiratory volume in 1s (FEV1)', 'L', 'Measurement', 'LOINC', '20150-9',
     'forcedExpiratoryVolume1', 'HKQuantityTypeIdentifierForcedExpiratoryVolume1', None),
    ('peak_expiratory_flow_rate', 'Peak expiratory flow rate (PEFR)', 'L/min', 'Measurement', 'LOINC', '33452-4',
     'peakExpiratoryFlowRate', 'HKQuantityTypeIdentifierPeakExpiratoryFlowRate', None),
    ('six_minute_walk_test_distance', '6-minute walk test distance', 'm', 'Measurement', 'LOINC', '64098-7',
     'sixMinuteWalkTestDistance', 'HKQuantityTypeIdentifierSixMinuteWalkTestDistance', None),
    ('distance_cycling', 'Cycling distance', 'm', 'Measurement', 'LOINC', '93818-3',
     'distanceCycling', 'HKQuantityTypeIdentifierDistanceCycling', None),
    ('distance_swimming', 'Swimming distance', 'm', 'Measurement', 'LOINC', '93816-7',
     'distanceSwimming', 'HKQuantityTypeIdentifierDistanceSwimming', None),
    ('distance_downhill_snow_sports', 'Downhill snow sports distance', 'm', 'Measurement', 'LOINC', '103208-5',
     'distanceDownhillSnowSports', 'HKQuantityTypeIdentifierDistanceDownhillSnowSports', None),
    # HL7's generic 'distance' is this metric; the key 'distance' is walking + running.
    ('distance_other', 'Other activity distance', 'm', 'Measurement', 'LOINC', '103208-5',
     'distance', None, 'DistanceRecord'),
    ('estimated_workout_effort_score', 'Estimated workout effort score', 'score', 'Observation', 'HK-Wearable', 'HK-WEAR-OW-WORKOUT-EFFORT',
     'estimatedWorkoutEffortScore', None, None),
    # The proposal names SNOMED 16821004 for average MET and marks it unverified;
    # like the OpenWearables code it stays unmapped until an expert has reviewed it.
    ('average_met', 'Average metabolic equivalent (MET)', 'MET', 'Observation', None, None,
     'met', None, None),
    ('blood_alcohol_content', 'Blood alcohol content', 'mg/dL', 'Measurement', 'SNOMED', '54791000237107',
     None, 'HKQuantityTypeIdentifierBloodAlcoholContent', None),

    # -- OpenWearables metrics the proposal gives no key: keyed by their OpenWearables code --
    ('heart_rate_recovery_one_minute', 'Heart rate recovery (1 min)', 'bpm', 'Measurement', None, None,
     'heartRateRecoveryOneMinute', 'HKQuantityTypeIdentifierHeartRateRecoveryOneMinute', None),
    ('walking_asymmetry_percentage', 'Walking asymmetry percentage', '%', 'Measurement', None, None,
     'walkingAsymmetryPercentage', 'HKQuantityTypeIdentifierWalkingAsymmetryPercentage', None),
    ('walking_steadiness', 'Walking steadiness', '%', 'Measurement', None, None,
     'appleWalkingSteadiness', 'HKQuantityTypeIdentifierAppleWalkingSteadiness', None),
    ('stair_descent_speed', 'Stair descent speed', 'm/s', 'Measurement', None, None,
     'stairDescentSpeed', 'HKQuantityTypeIdentifierStairDescentSpeed', None),
    ('stair_ascent_speed', 'Stair ascent speed', 'm/s', 'Measurement', None, None,
     'stairAscentSpeed', 'HKQuantityTypeIdentifierStairAscentSpeed', None),
    ('sleeping_breathing_disturbances', 'Sleeping breathing disturbances', 'count', 'Observation', None, None,
     'appleSleepingBreathingDisturbances', None, None),
    ('peripheral_perfusion_index', 'Peripheral perfusion index', 'score', 'Measurement', None, None,
     'peripheralPerfusionIndex', 'HKQuantityTypeIdentifierPeripheralPerfusionIndex', None),
    ('physical_effort', 'Physical effort score', 'score', 'Observation', None, None,
     'physicalEffort', None, None),
    ('power', 'Power output', 'W', 'Measurement', None, None,
     'power', None, 'PowerRecord'),
    ('speed', 'Speed', 'm/s', 'Measurement', None, None,
     'speed', None, 'SpeedRecord'),
    ('workout_effort_score', 'Workout effort score', 'score', 'Observation', None, None,
     'workoutEffortScore', None, None),
    ('running_power', 'Running power', 'W', 'Measurement', None, None,
     'runningPower', 'HKQuantityTypeIdentifierRunningPower', None),
    ('running_speed', 'Running speed', 'm/s', 'Measurement', None, None,
     'runningSpeed', 'HKQuantityTypeIdentifierRunningSpeed', None),
    ('running_vertical_oscillation', 'Running vertical oscillation', 'cm', 'Measurement', None, None,
     'runningVerticalOscillation', 'HKQuantityTypeIdentifierRunningVerticalOscillation', None),
    ('running_ground_contact_time', 'Running ground contact time', 'ms', 'Measurement', None, None,
     'runningGroundContactTime', 'HKQuantityTypeIdentifierRunningGroundContactTime', None),
    ('running_stride_length', 'Running stride length', 'cm', 'Measurement', None, None,
     'runningStrideLength', 'HKQuantityTypeIdentifierRunningStrideLength', None),
    ('swimming_stroke_count', 'Swimming stroke count', 'count', 'Measurement', None, None,
     'swimmingStrokeCount', 'HKQuantityTypeIdentifierSwimmingStrokeCount', 'SwimmingStrokesRecord'),
    ('underwater_depth', 'Underwater depth', 'm', 'Measurement', None, None,
     'underwaterDepth', 'HKQuantityTypeIdentifierUnderwaterDepth', None),
    ('environmental_audio_exposure', 'Environmental audio exposure', 'dB', 'Observation', None, None,
     'environmentalAudioExposure', 'HKQuantityTypeIdentifierEnvironmentalAudioExposure', None),
    ('headphone_audio_exposure', 'Headphone audio exposure', 'dB', 'Observation', None, None,
     'headphoneAudioExposure', 'HKQuantityTypeIdentifierHeadphoneAudioExposure', None),
    ('time_in_daylight', 'Time in daylight', 'min', 'Observation', None, None,
     'timeInDaylight', None, None),
    ('water_temperature', 'Water temperature', '\u00b0C', 'Measurement', None, None,
     'waterTemperature', 'HKQuantityTypeIdentifierWaterTemperature', None),
    ('uv_exposure', 'UV exposure', 'count', 'Observation', None, None,
     'uvExposure', 'HKQuantityTypeIdentifierUVExposure', None),
    ('inhaler_usage', 'Inhaler usage', 'count', 'Observation', None, None,
     'inhalerUsage', 'HKQuantityTypeIdentifierInhalerUsage', None),
    ('electrodermal_activity', 'Electrodermal activity', 'μS', 'Measurement', None, None,
     'electrodermalActivity', 'HKQuantityTypeIdentifierElectrodermalActivity', None),
    ('push_count', 'Push count (wheelchair)', 'count', 'Observation', None, None,
     'pushCount', 'HKQuantityTypeIdentifierPushCount', 'WheelchairPushesRecord'),
    ('atrial_fibrillation_burden', 'Atrial fibrillation burden', '%', 'Measurement', None, None,
     'atrialFibrillationBurden', 'HKQuantityTypeIdentifierAtrialFibrillationBurden', None),
    ('insulin_delivery', 'Insulin delivery', 'U', 'Measurement', None, None,
     None, 'HKQuantityTypeIdentifierInsulinDelivery', None),
    ('number_of_times_fallen', 'Number of times fallen', 'count', 'Observation', None, None,
     'numberOfTimesFallen', 'HKQuantityTypeIdentifierNumberOfTimesFallen', None),
    ('number_of_alcoholic_beverages', 'Number of alcoholic beverages', 'count', 'Observation', None, None,
     None, 'HKQuantityTypeIdentifierNumberOfAlcoholicBeverages', None),
    ('nike_fuel', 'Nike Fuel points', 'count', 'Observation', None, None,
     'nikeFuel', 'HKQuantityTypeIdentifierNikeFuel', None),
    ('hydration', 'Hydration intake', 'mL', 'Observation', None, None,
     'dietaryWater', 'HKQuantityTypeIdentifierDietaryWater', 'HydrationRecord'),

    # -- Added from the HL7 PHR code list --
    # From here to snore_index the HL7 PHR codes are those of HL7_PHR_METRICS, which
    # seeds them; the rows add the HealthKit and Health Connect codes.
    ('total_energy_burned', 'Total energy burned', 'kcal', 'Measurement', 'LOINC', '41981-2',
     'totalEnergyBurned', None, 'TotalCaloriesBurnedRecord'),
    ('steps_cadence', 'Steps cadence', 'steps/min', 'Measurement', None, None,
     'stepsCadence', None, 'StepsCadenceRecord'),
    ('rr_interval', 'R-R interval', 'ms', 'Measurement', None, None,
     'rrInterval', None, None),
    ('high_heart_rate_event', 'High heart rate event', 'count', 'Observation', None, None,
     'highHeartRateEvent', 'HKCategoryTypeIdentifierHighHeartRateEvent', None),
    ('low_heart_rate_event', 'Low heart rate event', 'count', 'Observation', None, None,
     'lowHeartRateEvent', 'HKCategoryTypeIdentifierLowHeartRateEvent', None),
    ('irregular_heart_rhythm_event', 'Irregular heart rhythm event', 'count', 'Observation', None, None,
     'irregularHeartRhythmEvent', 'HKCategoryTypeIdentifierIrregularHeartRhythmEvent', None),
    ('basal_body_temperature', 'Basal body temperature', '\u00b0C', 'Measurement', None, None,
     'basalBodyTemperature', 'HKQuantityTypeIdentifierBasalBodyTemperature', 'BasalBodyTemperatureRecord'),
    ('hip_circumference', 'Hip circumference', 'cm', 'Measurement', None, None,
     'hipCircumference', None, 'HipCircumferenceRecord'),
    ('bone_mass', 'Bone mass', 'kg', 'Measurement', None, None,
     'boneMass', None, 'BoneMassRecord'),
    ('sleep_arousal_index', 'Sleep arousal index', '/h', 'Measurement', None, None,
     'sleepArousalIndex', None, None),
    ('snore_index', 'Snore index', '/h', 'Measurement', None, None,
     'snoreIndex', None, None),

    # -- Added from the HL7 PHR Code Mapping table. The proposal gives these no
    # domain, unit or measure class: each is assigned here by its nearest
    # described metric (docs/wearable-omop-mapping.md has the rules), and no
    # unit is guessed beyond speed, distance, cadence, time and temperature --
    ('cycling_pedaling_cadence', 'Cycling pedaling cadence', 'rpm', 'Measurement', None, None,
     'cyclingPedalingCadence', None, 'CyclingPedalingCadenceRecord'),
    ('elevation_gained', 'Elevation gained', 'm', 'Measurement', None, None,
     'elevationGained', None, 'ElevationGainedRecord'),
    ('apple_move_time', 'Apple move time', 'min', 'Observation', None, None,
     'appleMoveTime', 'HKQuantityTypeIdentifierAppleMoveTime', None),
    ('apple_stand_hour', 'Apple stand hour', '', 'Observation', None, None,
     'appleStandHour', 'HKCategoryTypeIdentifierAppleStandHour', None),
    ('apple_stand_time', 'Apple stand time', 'min', 'Observation', None, None,
     'appleStandTime', 'HKQuantityTypeIdentifierAppleStandTime', None),
    ('distance_wheelchair', 'Wheelchair distance', 'm', 'Measurement', None, None,
     'distanceWheelchair', 'HKQuantityTypeIdentifierDistanceWheelchair', None),
    ('low_cardio_fitness_event', 'Low cardio fitness event', '', 'Observation', None, None,
     'lowCardioFitnessEvent', 'HKCategoryTypeIdentifierLowCardioFitnessEvent', None),
    ('pace', 'Pace', '', 'Measurement', None, None,
     'pace', None, None),
    ('moderate_activity', 'Moderate activity', '', 'Observation', None, None,
     'moderateActivity', None, None),
    ('vigorous_activity', 'Vigorous activity', '', 'Observation', None, None,
     'vigorousActivity', None, None),
    ('moderate_to_vigorous_activity', 'Moderate to vigorous activity', '', 'Observation', None, None,
     'moderateToVigorousActivity', None, None),
    ('met_by_standard_rmr', 'MET by standard resting metabolic rate', '', 'Observation', None, None,
     'metByStandardRmr', None, None),
    ('cross_country_skiing_speed', 'Cross-country skiing speed', 'm/s', 'Measurement', None, None,
     'crossCountrySkiingSpeed', None, None),
    ('distance_cross_country_skiing', 'Cross-country skiing distance', 'm', 'Measurement', None, None,
     'distanceCrossCountrySkiing', None, None),
    ('paddle_sports_speed', 'Paddle sports speed', 'm/s', 'Measurement', None, None,
     'paddleSportsSpeed', None, None),
    ('distance_paddle_sports', 'Paddle sports distance', 'm', 'Measurement', None, None,
     'distancePaddleSports', None, None),
    ('rowing_speed', 'Rowing speed', 'm/s', 'Measurement', None, None,
     'rowingSpeed', None, None),
    ('distance_rowing', 'Rowing distance', 'm', 'Measurement', None, None,
     'distanceRowing', None, None),
    ('distance_skating_sports', 'Skating sports distance', 'm', 'Measurement', None, None,
     'distanceSkatingSports', None, None),
    ('apple_sleeping_wrist_temperature', 'Apple sleeping wrist temperature', '\u00b0C', 'Measurement', None, None,
     'appleSleepingWristTemperature', 'HKQuantityTypeIdentifierAppleSleepingWristTemperature', None),
    ('environmental_audio_exposure_event', 'Environmental audio exposure event', '', 'Observation', None, None,
     'environmentalAudioExposureEvent', 'HKCategoryTypeIdentifierEnvironmentalAudioExposureEvent', None),
    ('headphone_audio_exposure_event', 'Headphone audio exposure event', '', 'Observation', None, None,
     'headphoneAudioExposureEvent', 'HKCategoryTypeIdentifierHeadphoneAudioExposureEvent', None),
    # HKElectrocardiogramType is what the HL7 PHR table gives: a HealthKit class
    # name, not a type identifier. Seeded as the table spells it.
    ('electrocardiogram', 'Electrocardiogram', '', 'Observation', None, None,
     'electrocardiogram', 'HKElectrocardiogramType', None),
    ('heart_beat_series', 'Heartbeat series', '', 'Measurement', None, None,
     'heartBeatSeries', 'HKDataTypeIdentifierHeartbeatSeries', None),
    ('inspiratory_time', 'Inspiratory time', '', 'Measurement', None, None,
     'inspiratoryTime', None, None),
    ('ventilation_cycle_time', 'Ventilation cycle time', '', 'Measurement', None, None,
     'ventilationCycleTime', None, None),
    ('minute_volume', 'Minute volume', '', 'Measurement', None, None,
     'minuteVolume', None, None),
    ('breath_carbon_monoxide', 'Breath carbon monoxide', '', 'Measurement', None, None,
     'breathCarbonMonoxide', None, None),
    ('sleep_episode', 'Sleep episode', '', 'Observation', None, None,
     'sleepEpisode', None, None),
    ('snore_event', 'Snore event', '', 'Observation', None, None,
     'snoreEvent', None, None),
    ('sleep_ahi', 'Sleep apnea-hypopnea index (AHI)', '', 'Measurement', None, None,
     'sleepAHI', None, None),
    ('sleep_apnea_event', 'Sleep apnea event', '', 'Observation', None, None,
     'sleepApneaEvent', None, None),
    ('apple_walking_steadiness_event', 'Apple walking steadiness event', '', 'Observation', None, None,
     'appleWalkingSteadinessEvent', 'HKCategoryTypeIdentifierAppleWalkingSteadinessEvent', None),
    ('exercise_event', 'Exercise event', '', 'Observation', None, None,
     'exerciseEvent', None, 'ExerciseEventRecord'),
    ('exercise_lap', 'Exercise lap', '', 'Observation', None, None,
     'exerciseLap', None, 'ExerciseLapRecord'),
    # 'exerciiseRepetitions' is how the HL7 PHR table spells the code, so it is
    # the source code a PHR document carries.
    ('exercise_repetitions', 'Exercise repetitions', '', 'Observation', None, None,
     'exerciiseRepetitions', None, 'ExerciseRepetitionsRecord'),
    ('workout_activity', 'Workout activity', '', 'Observation', None, None,
     'workoutActivity', None, 'ExerciseSessionRecord'),
    ('blood_pressure', 'Blood pressure', '', 'Measurement', None, None,
     'bloodPressure', 'HKCorrelationTypeIdentifierBloodPressure', 'BloodPressureRecord'),
]

# Each metric's category in the proposal, by metric key.
PHR_SOURCE_CATEGORIES = by_code({
    'Activity': (
        'steps', 'active_minutes', 'distance', 'active_energy', 'basal_energy',
        'flights_climbed', 'distance_cycling', 'distance_swimming',
        'distance_downhill_snow_sports', 'distance_other', 'estimated_workout_effort_score',
        'average_met', 'physical_effort', 'power', 'speed', 'workout_effort_score',
        'running_power', 'running_speed', 'running_vertical_oscillation',
        'running_ground_contact_time', 'running_stride_length', 'swimming_stroke_count',
        'underwater_depth', 'push_count', 'nike_fuel', 'total_energy_burned', 'steps_cadence',
        'cycling_pedaling_cadence', 'elevation_gained', 'apple_move_time', 'apple_stand_hour',
        'apple_stand_time', 'distance_wheelchair', 'pace', 'moderate_activity',
        'vigorous_activity', 'moderate_to_vigorous_activity', 'cross_country_skiing_speed',
        'distance_cross_country_skiing', 'paddle_sports_speed', 'distance_paddle_sports',
        'rowing_speed', 'distance_rowing', 'distance_skating_sports', 'exercise_event',
        'exercise_lap', 'exercise_repetitions', 'workout_activity',
    ),
    'Fitness': (
        'vo2_max', 'six_minute_walk_test_distance', 'low_cardio_fitness_event',
        'met_by_standard_rmr',
    ),
    'Heart & Cardiovascular': (
        'resting_hr', 'hrv_sdnn', 'walking_hr_avg', 'heart_rate',
        'heart_rate_recovery_one_minute', 'atrial_fibrillation_burden', 'rr_interval',
        'high_heart_rate_event', 'low_heart_rate_event', 'irregular_heart_rhythm_event',
        'electrocardiogram', 'heart_beat_series',
    ),
    'Blood & Respiratory': (
        'spo2', 'respiratory_rate', 'blood_glucose', 'blood_pressure_systolic',
        'blood_pressure_diastolic', 'forced_vital_capacity', 'forced_expiratory_volume_1',
        'peak_expiratory_flow_rate', 'blood_alcohol_content', 'peripheral_perfusion_index',
        'inspiratory_time', 'ventilation_cycle_time', 'minute_volume', 'breath_carbon_monoxide',
        'blood_pressure',
    ),
    'Body Composition': (
        'body_mass', 'height', 'body_fat_percentage', 'body_mass_index', 'lean_body_mass',
        'body_temperature', 'waist_circumference', 'basal_body_temperature',
        'hip_circumference', 'bone_mass', 'apple_sleeping_wrist_temperature',
    ),
    'Sleep': (
        'sleep_duration', 'sleeping_breathing_disturbances', 'sleep_arousal_index',
        'snore_index', 'sleep_episode', 'snore_event', 'sleep_ahi', 'sleep_apnea_event',
    ),
    'Mobility & Gait': (
        'walking_speed', 'walking_step_length', 'walking_double_support_pct',
        'walking_asymmetry_percentage', 'walking_steadiness', 'stair_descent_speed',
        'stair_ascent_speed', 'apple_walking_steadiness_event',
    ),
    'Medication & Device Use': (
        'inhaler_usage', 'insulin_delivery',
    ),
    'Safety': (
        'number_of_times_fallen',
    ),
    'Self-reported': (
        'number_of_alcoholic_beverages', 'hydration',
    ),
    'Stress & Autonomic': (
        'electrodermal_activity',
    ),
    'Environmental': (
        'environmental_audio_exposure', 'headphone_audio_exposure', 'time_in_daylight',
        'water_temperature', 'uv_exposure', 'environmental_audio_exposure_event',
        'headphone_audio_exposure_event',
    ),
})

# How the source produces each metric's value, by metric key: the proposal's
# Measure Class, or the class of its nearest metric where the proposal has none.
PHR_SOURCE_MEASURE_CLASSES = by_code({
    'Direct measurement': (
        'spo2', 'respiratory_rate', 'body_mass', 'heart_rate', 'blood_glucose',
        'blood_pressure_systolic', 'blood_pressure_diastolic', 'height', 'body_temperature',
        'waist_circumference', 'forced_vital_capacity', 'forced_expiratory_volume_1',
        'peak_expiratory_flow_rate', 'peripheral_perfusion_index', 'power', 'speed',
        'running_power', 'running_speed', 'underwater_depth', 'electrodermal_activity',
        'rr_interval', 'basal_body_temperature', 'hip_circumference',
        'cycling_pedaling_cadence', 'pace', 'cross_country_skiing_speed', 'paddle_sports_speed',
        'rowing_speed', 'apple_sleeping_wrist_temperature', 'electrocardiogram',
        'heart_beat_series', 'inspiratory_time', 'ventilation_cycle_time', 'minute_volume',
        'breath_carbon_monoxide', 'blood_pressure',
    ),
    'Aggregate over interval': (
        'steps', 'active_minutes', 'resting_hr', 'distance', 'sleep_duration', 'hrv_sdnn',
        'walking_hr_avg', 'flights_climbed', 'six_minute_walk_test_distance',
        'distance_cycling', 'distance_swimming', 'distance_downhill_snow_sports',
        'distance_other', 'heart_rate_recovery_one_minute', 'sleeping_breathing_disturbances',
        'swimming_stroke_count', 'time_in_daylight', 'uv_exposure', 'inhaler_usage',
        'push_count', 'atrial_fibrillation_burden', 'number_of_times_fallen', 'steps_cadence',
        'high_heart_rate_event', 'low_heart_rate_event', 'irregular_heart_rhythm_event',
        'sleep_arousal_index', 'snore_index', 'elevation_gained', 'apple_move_time',
        'apple_stand_hour', 'apple_stand_time', 'distance_wheelchair',
        'low_cardio_fitness_event', 'moderate_activity', 'vigorous_activity',
        'moderate_to_vigorous_activity', 'distance_cross_country_skiing',
        'distance_paddle_sports', 'distance_rowing', 'distance_skating_sports',
        'environmental_audio_exposure_event', 'headphone_audio_exposure_event', 'sleep_episode',
        'snore_event', 'sleep_ahi', 'sleep_apnea_event', 'apple_walking_steadiness_event',
        'exercise_repetitions',
    ),
    'Algorithmic estimate': (
        'vo2_max', 'active_energy', 'basal_energy', 'walking_speed', 'walking_step_length',
        'walking_double_support_pct', 'body_fat_percentage', 'body_mass_index',
        'lean_body_mass', 'average_met', 'walking_asymmetry_percentage', 'stair_descent_speed',
        'stair_ascent_speed', 'running_vertical_oscillation', 'running_ground_contact_time',
        'running_stride_length', 'total_energy_burned', 'bone_mass', 'met_by_standard_rmr',
    ),
    'Vendor score': (
        'estimated_workout_effort_score', 'walking_steadiness', 'physical_effort',
        'workout_effort_score', 'nike_fuel',
    ),
    'Environmental context': (
        'environmental_audio_exposure', 'headphone_audio_exposure', 'water_temperature',
    ),
    'User-entered or device log': (
        'blood_alcohol_content', 'insulin_delivery', 'number_of_alcoholic_beverages',
        'hydration', 'exercise_event', 'exercise_lap', 'workout_activity',
    ),
})


def _vocabulary_lists(column, seeded_elsewhere=()):
    """What ``seed`` needs for the codes in one *column* of PHR_SOURCE_METRICS.

    ``(metrics, categories, measure_classes, metric_keys)``: the metrics in the
    six-field shape of HL7_PHR_METRICS and the three maps by source code.
    A code in *seeded_elsewhere* is left out: another list already seeds it.
    """
    metrics, metric_keys = [], {}
    for row in PHR_SOURCE_METRICS:
        key, display, unit, domain, target_vocab, target_code = row[:6]
        code = row[column]
        if code and code not in seeded_elsewhere:
            metrics.append((code, display, unit, domain, target_vocab, target_code))
            metric_keys[code] = key
    return (
        metrics,
        {code: PHR_SOURCE_CATEGORIES[key] for code, key in metric_keys.items()},
        {code: PHR_SOURCE_MEASURE_CLASSES[key] for code, key in metric_keys.items()},
        metric_keys,
    )


_HL7_PHR_ADDED = _vocabulary_lists(6, seeded_elsewhere=HL7_PHR_METRIC_KEYS)
# The HealthKit identifiers the device seed owns are approved mappings the
# Apple adapter resolves through; they are not this command's to seed.
_APPLE = _vocabulary_lists(7, seeded_elsewhere=APPLE_METRIC_KEYS)
_HEALTH_CONNECT = _vocabulary_lists(8)

# (vocabulary_id, metrics, categories, measure_classes, metric_keys) for each
# vocabulary a run seeds, in the order it reports them.
SEED_SETS = (
    (VOCABULARY_ID,
     HL7_PHR_METRICS + _HL7_PHR_ADDED[0],
     {**HL7_PHR_CATEGORIES, **_HL7_PHR_ADDED[1]},
     {**HL7_PHR_MEASURE_CLASSES, **_HL7_PHR_ADDED[2]},
     {**HL7_PHR_METRIC_KEYS, **_HL7_PHR_ADDED[3]}),
    (APPLE_VOCABULARY_ID, *_APPLE),
    (HEALTH_CONNECT_VOCABULARY_ID, *_HEALTH_CONNECT),
)


class Command(WearableSeedCommand):
    help = ('Seed SourceCodeConceptMapping rows for the HL7 PHR metrics and the '
            'HealthKit and Health Connect codes the HL7 PHR table maps them from.')

    origin_system = SEED_ORIGIN_SYSTEM
    # A proposal target that is not a standard concept here is not proposed.
    require_standard_target = True

    def handle(self, **options):
        totals = {'created': 0, 'existed': 0, 'unresolved': 0, 'total': 0}
        # One transaction for the three vocabularies: a run seeds all or none.
        try:
            with transaction.atomic():
                for vocabulary_id, *lists in SEED_SETS:
                    counts = self.seed(vocabulary_id, *lists, label=vocabulary_id, **options)
                    for name, count in counts.items():
                        totals[name] += count
        except Exception:
            # Each vocabulary reports as it finishes, so a failure in a later
            # one follows lines that read as done. They were not kept.
            self.stderr.write(self.style.ERROR(
                'The run failed and was rolled back: nothing above was written.'))
            raise
        prefix = 'DRY RUN total: would have ' if options['dry_run'] else 'Done: '
        summary = (
            f"{prefix}{totals['created']} created, {totals['existed']} already existed, "
            f"{totals['unresolved']} without target concept (of {totals['total']} total)."
        )
        self.stdout.write(
            self.style.WARNING(summary) if options['dry_run'] else self.style.SUCCESS(summary))
