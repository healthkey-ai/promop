"""The OMOP rows behind a 30-day wearable metric, and its daily values over time (#2887).

Each 30-day aggregate on the Wearables tab is computed by
``patient_record_service._get_wearable_data`` from a patient's measurement and
observation rows. This module reads those same rows back, so the tab can

* show the day-by-day values the aggregate was taken over, as a chart, with the
  30-day window marked, and
* name the concept groups the rows sit in, which is what the OMOP tab opens.

The match mirrors the derivation: a row belongs to a metric when its concept's
code is the metric's wearable code, or, for a row whose concept is not any
wearable code, when its source value is. Entered-in-error rows and readings
outside the metric's artifact bounds are left out, as there. Unlike the
derivation (which reads approved rows for the record and provisional rows for
the provisional pass) this reads both, and says which each day is made of.

Nothing here decides who may read a patient; the caller does. A value is never
logged.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Avg, Count, Min, Q, Sum

from omop_core.services import omop_row_pages
from omop_core.services.mappings import (
    WEARABLE_ARTIFACT_BOUNDS, WEARABLE_CONCEPT_CODE, WEARABLE_METRIC_FIELDS,
)

# The newest days returned. A wearable adds a row per metric per day, so a
# multi-year history stays a few hundred points; this bounds it regardless.
MAX_DAYS = 365
WINDOW_DAYS = 30

# A day's sum for a metric that accumulates through the day, as the derivation
# totals them before averaging; a mean for the rest.
_SUMMED = frozenset({
    'steps', 'active_minutes', 'sleep_duration', 'distance', 'flights_climbed',
    'active_energy', 'basal_energy',
})

# What each field's number is, in words, for the chart's caption.
AGGREGATES = {
    'median_daily_steps_30d': 'Median of the days\' step totals',
    'activity_trend_30d': 'Compares the mean daily steps of the window\'s first and second halves',
    'oxygen_saturation_min_30d': 'The lowest single reading in the window',
}


def _statistic(field: str, metric: str) -> str:
    if field == 'oxygen_saturation_min_30d':
        return 'min'
    return 'sum' if metric in _SUMMED else 'mean'


def _aggregate(field: str, metric: str, label: str) -> str:
    if field in AGGREGATES:
        return AGGREGATES[field]
    if _statistic(field, metric) == 'sum':
        return f'Mean of the days\' totals ({label})'
    return f'Mean of the days\' averages ({label})'


def _build_fields() -> dict:
    fields = {}
    for metric, entries in WEARABLE_METRIC_FIELDS.items():
        for name, label in entries:
            fields[name] = (metric, label)
    return fields


# field name -> (metric key, the label the tab gives it)
FIELD_METRICS = _build_fields()

_ALL_CODES = frozenset(WEARABLE_CONCEPT_CODE.values())

# Per table: the concept's code column and the source value a row is matched by.
_TABLES = {
    'measurements': ('measurement_concept__concept_code', 'measurement_source_value'),
    'observations': ('observation_concept__concept_code', 'observation_source_value'),
}


def _rows(person, table: str, metric: str):
    """The patient's rows of one metric in one table: the derivation's own match."""
    spec = omop_row_pages.SPECS[table]
    code_col, source_col = _TABLES[table]
    code = WEARABLE_CONCEPT_CODE[metric]
    lo, hi = WEARABLE_ARTIFACT_BOUNDS[metric]
    # A row keys on its concept's code when that is a wearable code, else on its
    # source value, so a present-but-unrelated concept does not shadow the
    # source value.
    by_concept = Q(**{code_col: code})
    by_source = Q(**{source_col: code}) & (
        Q(**{f'{code_col}__isnull': True}) | ~Q(**{f'{code_col}__in': _ALL_CODES}))
    return spec, spec.model.objects.filter(
        by_concept | by_source, person=person, is_erroneous=False,
        value_as_number__isnull=False, value_as_number__gte=lo, value_as_number__lte=hi)


def _daily(person, metric: str) -> dict:
    """``{day: {'total', 'n', 'low', 'provisional'}}`` over both tables."""
    days: dict[date, dict] = {}
    for table in _TABLES:
        spec, qs = _rows(person, table, metric)
        for row in (qs.values(spec.date)
                    .annotate(total=Sum('value_as_number'), n=Count(spec.pk),
                              low=Min('value_as_number'),
                              provisional=Count(spec.pk, filter=Q(confidence__isnull=False)))
                    .order_by()):
            day = days.setdefault(row[spec.date], {'total': 0.0, 'n': 0, 'low': None, 'provisional': 0})
            day['total'] += float(row['total'])
            day['n'] += row['n']
            low = float(row['low'])
            day['low'] = low if day['low'] is None else min(day['low'], low)
            day['provisional'] += row['provisional']
    return days


def _groups(person, metric: str) -> list[dict]:
    """The concept groups holding the metric's rows, the largest first.

    A group is what the OMOP tab lists: a concept, or at concept 0 one source
    value. The same keys that tab opens a group by.
    """
    held = omop_row_pages.device_codes_held(person)
    groups = []
    for table in _TABLES:
        spec, qs = _rows(person, table, metric)
        device = omop_row_pages.is_device_row(spec, held[table])
        for row in (qs.values(spec.concept, spec.source)
                    .annotate(rows=Count(spec.pk), wearable=Count(spec.pk, filter=device))
                    .order_by()):
            groups.append({
                'table': table, 'concept_id': row[spec.concept],
                'source_value': (row[spec.source] or '')
                if row[spec.concept] == omop_row_pages.NO_MATCHING_CONCEPT_ID else None,
                'rows': row['rows'], 'wearable': row['wearable'] > 0,
            })
    # Several source values of one concept are one group on the OMOP tab.
    merged: dict = {}
    for group in groups:
        key = (group['table'], group['concept_id'], group['source_value'])
        into = merged.setdefault(key, {**group, 'rows': 0, 'wearable': False})
        into['rows'] += group['rows']
        into['wearable'] = into['wearable'] or group['wearable']
    result = sorted(merged.values(), key=lambda g: -g['rows'])
    omop_row_pages.name_groups([{'groups': result}])
    return result


def series(person, field: str, anchor: date | None, include_groups: bool) -> dict:
    """The daily values behind *field*, and (for an admin) the groups holding the rows.

    *anchor* is the last day of the 30-day window the stored aggregate covers
    (the record's ``wearable_last_sync_at``); without one the newest day is used.
    """
    metric, label = FIELD_METRICS[field]
    statistic = _statistic(field, metric)
    daily = _daily(person, metric)
    ordered = sorted(daily)
    truncated = len(ordered) > MAX_DAYS
    ordered = ordered[-MAX_DAYS:]
    end = anchor if anchor is not None else (ordered[-1] if ordered else None)
    start = end - timedelta(days=WINDOW_DAYS - 1) if end else None

    days = []
    for day in ordered:
        entry = daily[day]
        value = {'sum': entry['total'], 'mean': entry['total'] / entry['n'],
                 'min': entry['low']}[statistic]
        days.append({
            'date': day.isoformat(),
            'value': round(value, 2),
            'rows': entry['n'],
            'provisional_rows': entry['provisional'],
            'status': ('accepted' if entry['provisional'] == 0
                       else 'provisional' if entry['provisional'] == entry['n'] else 'mixed'),
            'in_window': bool(start and start <= day <= end),
        })
    return {
        'field': field,
        'label': label,
        'metric': metric,
        'statistic': statistic,
        'aggregate': _aggregate(field, metric, label),
        'window': {'start': start.isoformat(), 'end': end.isoformat()} if end else None,
        'days': days,
        'truncated': truncated,
        'groups': _groups(person, metric) if include_groups else None,
    }
