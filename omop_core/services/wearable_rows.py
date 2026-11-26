"""Wearable readings come in as source codes, like a hospital's (#2835).

A device import (a Garmin sync or push, an Apple Health or ``.fit`` upload, a
phone's sync) writes one OMOP row per reading. The row carries the **device's
own code** as its source value (``steps`` under ``Garmin``, a HealthKit
identifier under ``Apple``, a record type under ``HealthConnect``), and the
patient's source-code inventory (``PatientSourceCode``) records that the
patient holds that code under that vocabulary. From there a wearable code is
resolved exactly as a hospital's is, by ``omop_core/mapping/code_resolution.py``:

* an **approved** mapping gives the row its concept: an accepted row;
* a **proposal** that qualifies makes it provisional, at the proposal's
  confidence, while ``PROVISIONAL_DATA_ENABLED`` is on;
* otherwise the row is stored unresolved (concept 0) and waits, where it used
  to be dropped. Approving the mapping re-points it, rejecting a proposal
  reverts its provisional rows, and the patient's Source Codes tab lists the
  code with the others.

This module holds only what is the device's own: which code a metric arrives
under, which table its rows are filed in, and the inventory entry. Nothing
here decides what a mapping means.

Rows written before #2835 carry their destination concept's code as the
source value. Migration 0371 re-files the ones an upload's history names;
:func:`existing_rows` still reads the rest by concept, so an old row is found
as "already stored" either way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db.models import Count, Max, Min, Q

from omop_core.models import (
    Concept, Measurement, Observation, PatientSourceCode, SourceCodeConceptMapping,
)
from omop_core.services.mappings import (
    WEARABLE_METRIC_FIELDS, WEARABLE_OBSERVATION_METRICS, WEARABLE_TYPE_CONCEPT_ID,
)
from omop_core.services.source_vocabularies import WEARABLE_DEVICE_VOCABULARIES

logger = logging.getLogger(__name__)

# The source vocabulary each upload device's codes are mapped under.
DEVICE_VOCABULARY = {'apple': 'Apple', 'garmin': 'Garmin', 'android': 'HealthConnect', 'oura': 'Oura'}
VOCABULARY_DEVICE = {vocabulary: device for device, vocabulary in DEVICE_VOCABULARY.items()}
DEVICE_VOCABULARIES = WEARABLE_DEVICE_VOCABULARIES
SOURCE_LABELS = {'apple': 'Apple Health', 'garmin': 'Garmin', 'android': 'Health Connect',
                 'oura': 'Oura Ring'}

# Oura's codes are the metric key behind a prefix. A bare ``steps`` is Garmin's,
# and a person who held one spelling under two device vocabularies would have
# one device's mapping answer for, and sweep, the other's rows.
OURA_CODE_PREFIX = 'oura_'

NO_MATCHING_CONCEPT_ID = 0
ORIGIN_SYSTEM = 'wearable-import'

# table key -> (model, pk, concept column, type-concept column, source column, date field)
TABLES = {
    'measurement': (Measurement, 'measurement_id', 'measurement_concept_id',
                    'measurement_type_concept_id', 'measurement_source_value', 'measurement_date'),
    'observation': (Observation, 'observation_id', 'observation_concept_id',
                    'observation_type_concept_id', 'observation_source_value', 'observation_date'),
}

# The Health Connect record type each metric arrives as. Fixed here, as the
# HealthKit identifiers are in the export parser: a patient's rows are filed
# under this code, so it must not depend on which mapping rows an instance
# happens to hold. ``seed_hl7_phr_mappings`` seeds the same names.
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


def _apple_codes() -> dict:
    """metric key -> HealthKit identifier, the inverse of the export parser's map."""
    from omop_core.services.wearable_parsers import _APPLE_SLEEP_TYPE, _APPLE_TYPE_MAP
    return {**{metric: code for code, metric in _APPLE_TYPE_MAP.items()},
            'sleep_duration': _APPLE_SLEEP_TYPE}


def default_table(metric_key) -> str:
    """Where a metric's rows go while no mapping says: its usual concept's domain."""
    return 'observation' if metric_key in WEARABLE_OBSERVATION_METRICS else 'measurement'


@dataclass(frozen=True)
class DeviceCode:
    """One metric as a device sends it: its source code, and what a mapping says of it now."""

    metric_key: str
    vocabulary: str
    source_code: str
    table: str
    concept: Concept | None     # the approved mapping's; None while the code is unresolved
    mapping_id: int | None

    def stored_code(self) -> str:
        """The code as a clinical row holds it: cut to the source-value column."""
        model, _pk, _concept, _type, source_col, _date = TABLES[self.table]
        return self.source_code[:model._meta.get_field(source_col).max_length or 50]


def device_codes(device_type, metric_keys) -> dict:
    """``{metric_key: DeviceCode}`` for the metrics of one upload.

    The code is the device's own. The concept is the **approved** mapping's for
    exactly that vocabulary and code (a global one, the later row among
    several); a proposal is not read here: the resolver decides what a
    proposal makes of an unresolved row, for wearable codes as for any other.

    A Health Connect mapping a curator approved under another code for the
    metric (its ``metric_key``) still answers, and the rows are then filed
    under that code: it is the one the approval's re-point looks for.
    """
    vocabulary = DEVICE_VOCABULARY.get(device_type)
    if vocabulary is None:
        return {}
    metric_keys = list(dict.fromkeys(metric_keys))
    if device_type == 'garmin':
        codes = {metric: metric for metric in metric_keys}
    elif device_type == 'oura':
        codes = {metric: OURA_CODE_PREFIX + metric for metric in metric_keys}
    elif device_type == 'apple':
        apple = _apple_codes()
        codes = {metric: apple[metric] for metric in metric_keys if metric in apple}
    else:
        codes = {metric: HEALTH_CONNECT_CODES.get(metric, metric) for metric in metric_keys}

    mappings = list(
        SourceCodeConceptMapping.objects
        .filter(source_vocabulary_id=vocabulary, organization__isnull=True)
        .filter(Q(source_code__in=set(codes.values()))
                | (Q(metric_key__in=metric_keys, status='approved') if device_type == 'android' else Q()))
        .select_related('target_concept')
        .order_by('id'))

    if device_type == 'android':
        # The approved row that names the metric, under whatever code it holds.
        for mapping in mappings:
            if (mapping.status == 'approved' and mapping.target_concept_id
                    and mapping.metric_key in codes):
                codes[mapping.metric_key] = mapping.source_code

    by_code = {}
    for mapping in mappings:
        current = by_code.get(mapping.source_code)
        # Approved over anything else; the later row among equals.
        if current is None or mapping.status == 'approved' or current.status != 'approved':
            by_code[mapping.source_code] = mapping

    resolved = {}
    for metric, code in codes.items():
        mapping = by_code.get(code)
        approved = (mapping is not None and mapping.status == 'approved'
                    and mapping.target_concept is not None)
        table = (mapping.omop_table if mapping is not None else '') or ''
        if table not in TABLES:
            table = (('observation' if mapping.target_concept.domain_id == 'Observation' else 'measurement')
                     if approved else default_table(metric))
        resolved[metric] = DeviceCode(
            metric_key=metric, vocabulary=vocabulary, source_code=code, table=table,
            concept=mapping.target_concept if approved else None,
            mapping_id=mapping.pk if mapping is not None else None)
    return resolved


def queue_unmapped(codes) -> int:
    """A queue row for each code no mapping row holds, as ingest queues a hospital's.

    Proposed, with no destination: what ``enqueue_unmapped_source_codes``
    leaves, and what Suggest and a curator pick up. Returns how many were made.
    """
    made = 0
    for code in codes:
        if code.mapping_id is not None:
            continue
        _row, created = SourceCodeConceptMapping.objects.get_or_create(
            source_vocabulary_id=code.vocabulary, source_code=code.source_code, organization=None,
            defaults={
                # Not ``origin='import'``: a re-point and a relocation take an
                # import row's description for a second row key, and this one
                # ("steps", "spo2") is another device's code and a hospital's.
                'omop_table': code.table, 'status': 'proposed',
                'origin_system': ORIGIN_SYSTEM, 'metric_key': code.metric_key,
                'source_code_description': code.metric_key.replace('_', ' '),
                'occurrence_count': 1,
            })
        made += int(created)
    return made


def existing_rows(person, code, days):
    """The patient's stored rows for one device code within ``days``.

    Yields ``(table, row id, date, value, type concept id, filed under the
    code)``. Rows filed under the code, and, where the code is resolved, rows
    at its concept: what was written before rows carried their device code,
    and another device's reading of the same metric, which was always read as
    "already stored" when the value was the same.

    Both tables are read. A code whose mapping moved from one to the other
    has rows in the table it left until they are relocated, and a row that
    was relocated has a new id its upload's history does not name; reading
    only the mapping's table would store such a day a second time.
    """
    for table, (model, pk, concept_col, type_col, source_col, date_field) in TABLES.items():
        stored = code.source_code[:model._meta.get_field(source_col).max_length or 50]
        match = Q(**{source_col: stored})
        if code.concept is not None:
            match |= Q(**{concept_col: code.concept.pk})
        for row_id, day, value, type_id, source in model.objects.filter(
                match, person=person, is_erroneous=False, **{f'{date_field}__range': days},
        ).values_list(pk, date_field, 'value_as_number', type_col, source_col):
            yield table, row_id, day, value, type_id, source == stored


def record_inventory(person, codes, units) -> None:
    """The patient holds these codes under the device's vocabulary: their inventory says so.

    One row per code, as the ETL's source-code upload leaves for a hospital
    code, with the number of rows now stored for it. The resolver reads the
    inventory to know which code system a row's code came from; the Source
    Codes tab lists it. A code with no stored row keeps no entry: the
    resolver creates a row for an inventory entry that has none.
    """
    for code in codes:
        model, _pk, _concept, type_col, source_col, _date = TABLES[code.table]
        stored = model.objects.filter(
            person=person, **{source_col: code.stored_code(), type_col: WEARABLE_TYPE_CONCEPT_ID},
        ).count()
        entries = PatientSourceCode.objects.filter(
            person=person, source_value=code.source_code, source_vocabulary_id=code.vocabulary)
        if not stored:
            entries.delete()
            continue
        # Filed under one table: a code whose mapping moved has its rows moved with it.
        entries.exclude(omop_table=code.table).delete()
        PatientSourceCode.objects.update_or_create(
            person=person, source_value=code.source_code, source_vocabulary_id=code.vocabulary,
            omop_table=code.table,
            defaults={
                'occurrence_count': stored,
                'source_unit': units.get(code.metric_key) or '',
                'source_metadata': {
                    'device': SOURCE_LABELS[VOCABULARY_DEVICE[code.vocabulary]],
                    'metric': code.metric_key,
                },
            })


def delete_moved(person, device_type, entries) -> int:
    """Delete the rows of an upload's history entries that are no longer under their ids.

    A row relocated with its code's mapping has a new id in the other table.
    It is found by what it is: this device's code, a wearable row, that day,
    that value. A row replaced by a later sync carries another value and is
    another upload's; nothing matches it here.
    """
    from datetime import date as _date
    codes = device_codes(device_type, [entry.get('metric') for entry in entries
                                       if isinstance(entry.get('metric'), str)])
    deleted = 0
    for entry in entries:
        code = codes.get(entry.get('metric'))
        try:
            day = _date.fromisoformat(str(entry.get('date')))
            value = float(entry.get('value'))
        except (TypeError, ValueError):
            continue
        if code is None:
            continue
        for _table, (model, _pk, _concept, type_col, source_col, date_field) in TABLES.items():
            stored = code.source_code[:model._meta.get_field(source_col).max_length or 50]
            count, _ = model.objects.filter(
                person=person, value_as_number=value,
                **{source_col: stored, type_col: WEARABLE_TYPE_CONCEPT_ID, date_field: day},
            ).delete()
            deleted += count
    return deleted


def prune_inventory(person) -> None:
    """Drop the inventory entry of a device code the patient no longer has a row for.

    After an upload is deleted. Left behind, the entry would have Generate
    OMOP create an empty row for a reading that is gone.
    """
    for entry in PatientSourceCode.objects.filter(
            person=person, source_vocabulary_id__in=DEVICE_VOCABULARIES):
        spec = TABLES.get(entry.omop_table)
        if spec is None:
            continue
        model, _pk, _concept, _type, source_col, _date = spec
        width = model._meta.get_field(source_col).max_length or 50
        if not model.objects.filter(person=person, **{source_col: entry.source_value[:width]}).exists():
            entry.delete()


def needs_resolving(person, codes) -> list:
    """The codes of this upload that hold a row the resolver may have something to say about.

    Unresolved, or provisional: a mapping approved since, a proposal that
    qualifies now, or one that no longer does.
    """
    waiting = []
    for code in codes:
        model, _pk, concept_col, _type, source_col, _date = TABLES[code.table]
        if model.objects.filter(person=person, **{source_col: code.stored_code()}).filter(
                Q(**{concept_col: NO_MATCHING_CONCEPT_ID}) | Q(confidence__isnull=False)).exists():
            waiting.append(code)
    return waiting


# ---- what the Source Codes tab says of a wearable code -------------------------------

def _metric_of(vocabulary, source_code, apple, health_connect, mapping_metrics) -> str:
    if vocabulary == 'Garmin':
        return source_code
    if vocabulary == 'Oura':
        return source_code.removeprefix(OURA_CODE_PREFIX)
    if vocabulary == 'Apple':
        return apple.get(source_code, '')
    return mapping_metrics.get(source_code) or health_connect.get(source_code, '')


def describe(person, entries) -> None:
    """Add ``wearable`` to each source-code entry that is a device's code, in place.

    How many of the patient's rows for the code are accepted, provisional or
    still unresolved, over which days, and which 30-day metrics the code
    feeds. Counts and dates only. One grouped query per table, whatever the
    number of codes.
    """
    # Only what the patient's inventory lists (``source_code_id``): an entry
    # aggregated from the clinical tables has no vocabulary of its own, and
    # takes a mapping's by code alone, so a hospital's "steps" would read as
    # Garmin's.
    mine = [entry for entry in entries
            if entry.get('source_code_id') and entry.get('source_vocabulary_id') in DEVICE_VOCABULARIES
            and entry.get('omop_table') in TABLES]
    if not mine:
        return
    from omop_core.services.wearable_parsers import _APPLE_SLEEP_TYPE, _APPLE_TYPE_MAP
    apple = {**_APPLE_TYPE_MAP, _APPLE_SLEEP_TYPE: 'sleep_duration'}
    health_connect = {code: metric for metric, code in HEALTH_CONNECT_CODES.items()}
    mapping_metrics = dict(
        SourceCodeConceptMapping.objects
        .filter(source_vocabulary_id='HealthConnect', organization__isnull=True,
                source_code__in=[e['source_code'] for e in mine
                                 if e['source_vocabulary_id'] == 'HealthConnect'])
        .exclude(metric_key='').order_by('id').values_list('source_code', 'metric_key'))

    counts = {}
    for table, (model, pk, concept_col, type_col, source_col, date_field) in TABLES.items():
        width = model._meta.get_field(source_col).max_length or 50
        codes = {entry['source_value'][:width] for entry in mine if entry['omop_table'] == table}
        if not codes:
            continue
        unresolved = Q(**{concept_col: NO_MATCHING_CONCEPT_ID}, confidence__isnull=True)
        for row in (model.objects
                    # The importer's rows, not a hospital's of the same spelling.
                    .filter(person=person, is_erroneous=False, **{f'{source_col}__in': codes,
                                                                 type_col: WEARABLE_TYPE_CONCEPT_ID})
                    .values(source_col)
                    .annotate(rows=Count(pk),
                              provisional=Count(pk, filter=Q(confidence__isnull=False)),
                              unresolved=Count(pk, filter=unresolved),
                              confidence=Min('confidence'),
                              first=Min(date_field), last=Max(date_field))):
            counts[table, row[source_col]] = row

    for entry in mine:
        model, _pk, _concept, _type, source_col, _date = TABLES[entry['omop_table']]
        width = model._meta.get_field(source_col).max_length or 50
        row = counts.get((entry['omop_table'], entry['source_value'][:width]), {})
        rows, provisional, unresolved = (row.get('rows', 0), row.get('provisional', 0),
                                         row.get('unresolved', 0))
        accepted = rows - provisional - unresolved
        kinds = [name for name, count in (('accepted', accepted), ('provisional', provisional),
                                          ('unresolved', unresolved)) if count]
        # One word when every row is the same kind; "mixed" says to read the counts.
        status = 'none' if not kinds else kinds[0] if len(kinds) == 1 else 'mixed'
        metric = _metric_of(entry['source_vocabulary_id'], entry['source_code'],
                            apple, health_connect, mapping_metrics)
        entry['wearable'] = {
            'device': VOCABULARY_DEVICE[entry['source_vocabulary_id']],
            'source': SOURCE_LABELS[VOCABULARY_DEVICE[entry['source_vocabulary_id']]],
            'metric_key': metric,
            'rows': rows,
            'accepted_rows': accepted,
            'provisional_rows': provisional,
            'unresolved_rows': unresolved,
            # The lowest among the provisional rows; null when there are none.
            'confidence': row.get('confidence'),
            'first_date': row['first'].isoformat() if row.get('first') else None,
            'last_date': row['last'].isoformat() if row.get('last') else None,
            'status': status,
            'fields': [{'name': name, 'label': label}
                       for name, label in WEARABLE_METRIC_FIELDS.get(metric, ())],
        }
