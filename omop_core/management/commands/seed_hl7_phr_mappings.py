"""Seed SourceCodeConceptMapping rows for the HL7 PHR metrics.

The HL7 PHR Code Mapping table names metrics that no OpenWearables, Apple or
Garmin source code carries yet (total energy burned, dietary nutrients, R-R
interval, heart rhythm events and so on). Each becomes one proposed mapping
whose source code is the HL7 PHR code value, in the ``HL7-PHR`` source
vocabulary, with provenance ``HL7 PHR``. Where the HL7 table gives a LOINC
code it is the proposed destination; the rest are left for curator review.

This is ``seed_openwearables_mappings`` over another metric list: the same
idempotence, the same ``--dry-run`` and ``--update-seeded`` rules, the same
unit, category, measure class and metric key seeding.
"""

from omop_core.management.commands.seed_openwearables_mappings import (
    Command as WearableSeedCommand,
    by_code,
)

VOCABULARY_ID = 'HL7-PHR'
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


class Command(WearableSeedCommand):
    help = 'Seed SourceCodeConceptMapping rows for the HL7 PHR metrics.'

    vocabulary_id = VOCABULARY_ID
    origin_system = SEED_ORIGIN_SYSTEM
    metrics = HL7_PHR_METRICS
    categories = HL7_PHR_CATEGORIES
    measure_classes = HL7_PHR_MEASURE_CLASSES
    metric_keys = HL7_PHR_METRIC_KEYS
