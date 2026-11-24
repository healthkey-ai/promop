"""Wearable rows carry their device's source code (#2835).

A wearable import used to store the destination concept's code as a row's
source value (``55423-8`` for Garmin steps). From #2835 a row is filed under
the device's own code (``steps`` under ``Garmin``), with an entry in the
patient's source-code inventory, so a wearable code is resolved, re-pointed
and listed exactly as a hospital's is.

This re-files the rows already stored, where an upload's history says whose
they are: each ``WearableUpload.sample_summary`` entry that names the row it
wrote (``row_id``, recorded since #2794) gives the device and the metric. A row
is touched only while it still looks as the importer left it: typed as a
wearable row, with its own concept's code as the source value. The patient's
inventory gets one entry per code that has rows.

Rows from uploads older than #2794 name no row and are left as they are: the
importer still finds them by concept, and the 30-day summaries read them by
concept as before. They are simply not listed as device codes.

Safe to run twice. The reverse does nothing: the old source value was only a
copy of the concept's code, and a row under its device code reads the same
everywhere.
"""
import logging
from collections import defaultdict

from django.db import connection as default_connection, migrations
from django.db.models import F

logger = logging.getLogger(__name__)

WEARABLE_TYPE_CONCEPT_ID = 32865
DEVICE_VOCABULARY = {'apple': 'Apple', 'garmin': 'Garmin', 'android': 'HealthConnect'}
DEVICE_LABEL = {'apple': 'Apple Health', 'garmin': 'Garmin', 'android': 'Health Connect'}

# Frozen copies of the importer's own tables as #2835 has them: a migration
# must not change meaning when they do.
APPLE_CODES = {
    'steps': 'HKQuantityTypeIdentifierStepCount',
    'active_minutes': 'HKQuantityTypeIdentifierAppleExerciseTime',
    'resting_hr': 'HKQuantityTypeIdentifierRestingHeartRate',
    'hrv_sdnn': 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN',
    'spo2': 'HKQuantityTypeIdentifierOxygenSaturation',
    'respiratory_rate': 'HKQuantityTypeIdentifierRespiratoryRate',
    'vo2_max': 'HKQuantityTypeIdentifierVO2Max',
    'distance': 'HKQuantityTypeIdentifierDistanceWalkingRunning',
    'walking_speed': 'HKQuantityTypeIdentifierWalkingSpeed',
    'walking_step_length': 'HKQuantityTypeIdentifierWalkingStepLength',
    'walking_double_support_pct': 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage',
    'walking_hr_avg': 'HKQuantityTypeIdentifierWalkingHeartRateAverage',
    'flights_climbed': 'HKQuantityTypeIdentifierFlightsClimbed',
    'active_energy': 'HKQuantityTypeIdentifierActiveEnergyBurned',
    'basal_energy': 'HKQuantityTypeIdentifierBasalEnergyBurned',
    'body_mass': 'HKQuantityTypeIdentifierBodyMass',
    'sleep_duration': 'HKCategoryTypeIdentifierSleepAnalysis',
}
HEALTH_CONNECT_CODES = {
    'steps': 'StepsRecord',
    'resting_hr': 'RestingHeartRateRecord',
    'hrv_rmssd': 'HeartRateVariabilityRmssdRecord',
    'spo2': 'OxygenSaturationRecord',
    'respiratory_rate': 'RespiratoryRateRecord',
    'vo2_max': 'Vo2MaxRecord',
    'flights_climbed': 'FloorsClimbedRecord',
    'active_energy': 'ActiveCaloriesBurnedRecord',
    'basal_energy': 'BasalMetabolicRateRecord',
    'body_mass': 'WeightRecord',
    'sleep_duration': 'SleepSessionRecord',
    'distance': 'DistanceRecord',
}
UNITS = {
    'steps': '/d', 'active_minutes': 'min', 'resting_hr': '/min', 'hrv_sdnn': 'ms',
    'hrv_rmssd': 'ms', 'spo2': '%', 'respiratory_rate': '/min', 'sleep_duration': 'h',
    'vo2_max': 'mL/kg/min', 'distance': 'km', 'walking_speed': 'km/hr',
    'walking_step_length': 'cm', 'walking_double_support_pct': '%', 'walking_hr_avg': '/min',
    'flights_climbed': '{flights}', 'active_energy': 'kcal', 'basal_energy': 'kcal',
    'body_mass': 'kg',
}
# table -> (model, concept relation, type-concept column, source column)
TABLES = {
    'measurement': ('Measurement', 'measurement_concept', 'measurement_type_concept_id',
                    'measurement_source_value'),
    'observation': ('Observation', 'observation_concept', 'observation_type_concept_id',
                    'observation_source_value'),
}
CHUNK = 2000


def _code(device, metric, health_connect):
    if device == 'garmin':
        return metric
    if device == 'apple':
        return APPLE_CODES.get(metric)
    return health_connect.get(metric) or HEALTH_CONNECT_CODES.get(metric) or metric


def refile(apps, schema_editor):
    connection = schema_editor.connection if schema_editor is not None else default_connection
    if connection.vendor != 'postgresql':
        return
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Inventory = apps.get_model('omop_core', 'PatientSourceCode')

    named = defaultdict(lambda: defaultdict(set))       # (device, metric, table) -> person -> row ids
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT u.person_id, u.device_type, e->>'metric', e->>'row_table', e->>'row_id'
            FROM wearable_upload u
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(u.sample_summary) = 'array'
                     THEN u.sample_summary ELSE '[]'::jsonb END) AS e
            WHERE e->>'row_id' ~ '^[0-9]{1,18}$' AND e->>'row_table' IN ('measurement', 'observation')
              AND e->>'metric' IS NOT NULL
            """)
        for person_id, device, metric, table, row_id in cursor.fetchall():
            if device in DEVICE_VOCABULARY:
                named[device, metric, table][person_id].add(int(row_id))
    if not named:
        return

    # A Health Connect metric is filed under the code of the approved mapping
    # that names it, where a curator approved one, as the importer files it.
    health_connect = dict(
        Mapping.objects.filter(
            source_vocabulary_id='HealthConnect', status='approved', organization__isnull=True,
            target_concept__isnull=False,
        ).exclude(metric_key='').order_by('id').values_list('metric_key', 'source_code'))

    refiled = listed = 0
    for (device, metric, table), persons in named.items():
        code = _code(device, metric, health_connect)
        if not code:
            continue
        model_name, concept_rel, type_col, source_col = TABLES[table]
        Model = apps.get_model('omop_core', model_name)
        stored = code[:Model._meta.get_field(source_col).max_length or 50]
        for person_id, row_ids in persons.items():
            row_ids = sorted(row_ids)
            for start in range(0, len(row_ids), CHUNK):
                refiled += Model.objects.filter(
                    pk__in=row_ids[start:start + CHUNK], person_id=person_id,
                    **{type_col: WEARABLE_TYPE_CONCEPT_ID,
                       source_col: F(f'{concept_rel}__concept_code')},
                ).update(**{source_col: stored})
            count = Model.objects.filter(
                person_id=person_id, **{type_col: WEARABLE_TYPE_CONCEPT_ID, source_col: stored},
            ).count()
            if not count:
                continue
            _entry, created = Inventory.objects.get_or_create(
                person_id=person_id, source_value=code,
                source_vocabulary_id=DEVICE_VOCABULARY[device], omop_table=table,
                defaults={
                    'occurrence_count': count, 'source_unit': UNITS.get(metric, ''),
                    'source_metadata': {'device': DEVICE_LABEL[device], 'metric': metric},
                })
            listed += int(created)
    logger.warning('Wearable rows re-filed under their device codes: %d row(s), %d inventory entr(ies).',
                   refiled, listed)


def keep(apps, schema_editor):
    # Nothing to restore: the old source value was a copy of the row's own
    # concept code, and nothing reads a wearable row by it once rows carry
    # their device code.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0370_garmin_personal_key'),
    ]

    operations = [
        migrations.RunPython(refile, keep),
    ]
