"""A patient's measurement and observation rows, grouped by concept and read a page at a time.

The OMOP tab used to dump every row of both tables, and a wearable adds one
row per metric per day, so a patient can hold thousands of rows for one
concept. This module serves the two reads that replace the dump:

* :func:`grouped` -- one entry per concept (name, code, vocabulary, number of
  rows, latest and earliest moment), and, for rows at concept 0 (nothing has
  answered for their code yet), one entry per source value. A fixed number of
  queries, whatever the number of rows.
* :func:`read_page` -- the rows of one group, newest first, a keyset page at a
  time. Also what the Source Codes tab reads to open a wearable code.

Nothing here decides who may read a patient: the callers do, before they get
this far. Nothing is logged: a value is never put in a log line.

Order is **date, then datetime, then id, all descending, datetimes before
date-only rows of the same day**. A row's date is always set and its datetime
often is not (a wearable stores only the day), so the date decides the day and
the datetime the order inside it. A row with no datetime is never given one.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime

from django.db.models import BooleanField, Case, CharField, Count, F, IntegerField, Max, Min, Q, Value, When
from django.db.models.functions import Coalesce

from omop_core.models import Concept, Measurement, Observation, PatientSourceCode
from omop_core.services.mappings import WEARABLE_TYPE_CONCEPT_ID
from omop_core.services.source_vocabularies import WEARABLE_DEVICE_VOCABULARIES

PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# Groups returned per list; the grouped query is limited in SQL, so an unusual
# patient costs a truncated list, never an unbounded answer.
MAX_GROUPS = 500

NO_MATCHING_CONCEPT_ID = 0


@dataclass(frozen=True)
class Spec:
    model: type
    pk: str
    concept: str        # the column, as ``concept_id`` holds it
    type: str
    source: str
    date: str
    datetime: str


SPECS = {
    'measurements': Spec(Measurement, 'measurement_id', 'measurement_concept_id',
                         'measurement_type_concept_id', 'measurement_source_value', 'measurement_date', 'measurement_datetime'),
    'observations': Spec(Observation, 'observation_id', 'observation_concept_id',
                         'observation_type_concept_id', 'observation_source_value', 'observation_date', 'observation_datetime'),
}


def status_of(concept_id, confidence) -> str:
    """What a row is: the same three kinds the Source Codes tab names.

    ``confidence`` is null on an accepted row and a percent on a provisional
    one; an unresolved row sits at concept 0 and carries none.
    """
    if confidence is not None:
        return 'provisional'
    return 'unresolved' if concept_id == NO_MATCHING_CONCEPT_ID else 'accepted'


# ---- cursor -------------------------------------------------------------------------

def encode_cursor(row_date, row_datetime, row_id) -> str:
    raw = json.dumps([row_date.isoformat(),
                      row_datetime.isoformat() if row_datetime else None, row_id])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')


def decode_cursor(text):
    """``(date, datetime or None, id)``; ValueError for anything this module did not issue."""
    try:
        raw = json.loads(base64.urlsafe_b64decode(text + '=' * (-len(text) % 4)))
        day, moment, row_id = raw
        if not isinstance(day, str) or not isinstance(row_id, int) or isinstance(row_id, bool):
            raise ValueError
        moment = None if moment is None else datetime.fromisoformat(moment)
        if moment is not None and moment.tzinfo is None:
            raise ValueError
        return date.fromisoformat(day), moment, row_id
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValueError('The cursor is not one this endpoint issued.') from None


# ---- the rows of one group ----------------------------------------------------------

def read_page(qs, spec: Spec, cursor=None, limit: int = PAGE_SIZE) -> dict:
    """``{'rows': [...], 'next_cursor': str | None}``, newest first.

    *qs* is the rows to read (already narrowed to one group and one patient).
    Keyset, not offset: the cost of a page does not grow with how far down the
    reader is, and a row written meanwhile cannot repeat or skip a neighbour.
    """
    if cursor is not None:
        day, moment, row_id = cursor
        same_day = Q(**{spec.date: day})
        if moment is None:
            after = Q(**{f'{spec.datetime}__isnull': True, f'{spec.pk}__lt': row_id})
        else:
            # Descending with the datetimes first: a smaller datetime, a day-only
            # row, or the same moment with a smaller id.
            after = (Q(**{f'{spec.datetime}__lt': moment})
                     | Q(**{f'{spec.datetime}__isnull': True})
                     | Q(**{spec.datetime: moment, f'{spec.pk}__lt': row_id}))
        qs = qs.filter(Q(**{f'{spec.date}__lt': day}) | (same_day & after))

    fetched = list(
        qs.order_by(F(spec.date).desc(), F(spec.datetime).desc(nulls_last=True), F(spec.pk).desc())
        .values(spec.pk, spec.date, spec.datetime, 'value_as_number', 'value_as_string',
                'unit_source_value', 'unit_concept__concept_code', spec.source, spec.concept,
                'confidence', 'is_erroneous')[:limit + 1])
    page = fetched[:limit]
    rows = [{
        'id': row[spec.pk],
        'date': row[spec.date].isoformat(),
        'datetime': row[spec.datetime].isoformat() if row[spec.datetime] else None,
        'value_as_number': float(row['value_as_number']) if row['value_as_number'] is not None else None,
        'value_as_string': row['value_as_string'],
        'unit': row['unit_source_value'] or row['unit_concept__concept_code'] or None,
        'source_value': row[spec.source],
        'concept_id': row[spec.concept],
        'status': status_of(row[spec.concept], row['confidence']),
        'confidence': row['confidence'],
        'is_erroneous': row['is_erroneous'],
    } for row in page]
    next_cursor = None
    if len(fetched) > limit:
        last = page[-1]
        next_cursor = encode_cursor(last[spec.date], last[spec.datetime], last[spec.pk])
    return {'rows': rows, 'next_cursor': next_cursor}


def is_device_row(spec: Spec, held) -> Q:
    """The rows that are a device's: the importer's, under a code the inventory holds as one."""
    if not held:
        return Q(pk__in=[])
    return Q(**{spec.type: WEARABLE_TYPE_CONCEPT_ID, f'{spec.source}__in': held})


def group_rows(person, spec: Spec, concept_id: int, source_value: str | None,
               wearable: bool | None = None, held=()):
    """The patient's rows of one group: a concept, or at concept 0 one source value.

    At concept 0 a source value's rows are two groups when a device and a
    hospital both use the spelling (``spo2``): *wearable* says which one, and
    ``None`` reads both.
    """
    qs = spec.model.objects.filter(person=person, **{spec.concept: concept_id})
    if concept_id == NO_MATCHING_CONCEPT_ID:
        empty = Q(**{f'{spec.source}__isnull': True}) | Q(**{spec.source: ''})
        qs = qs.filter(empty if not source_value else Q(**{spec.source: source_value}))
        if wearable is not None:
            qs = qs.filter(is_device_row(spec, held)) if wearable else qs.exclude(is_device_row(spec, held))
    return qs


# ---- the groups ---------------------------------------------------------------------

def _moment(day, moment, *, latest: bool):
    """The datetime to show beside a day, or None when the day has none.

    The extreme datetime is only the extreme *moment* when it falls on the
    extreme day: a row that stores only a later day outranks it, and then the
    day is all there is to show.
    """
    if moment is None:
        return None
    return moment.isoformat() if (moment.date() >= day if latest else moment.date() <= day) else None


def device_codes_held(person) -> dict:
    """``{table key: {source value as a row stores it}}``: the device codes the patient's inventory lists.

    The rule ``wearable_rows.describe`` counts by: a row is a device's when it
    is the importer's (type concept 32865) and its code is one the inventory
    holds under a device vocabulary. One query for both tables.
    """
    held = {key: set() for key in SPECS}
    for table, value in PatientSourceCode.objects.filter(
            person=person, source_vocabulary_id__in=WEARABLE_DEVICE_VOCABULARIES,
            omop_table__in=('measurement', 'observation'),
    ).values_list('omop_table', 'source_value'):
        spec = SPECS[f'{table}s']
        held[f'{table}s'].add(value[:spec.model._meta.get_field(spec.source).max_length or 50])
    return held


def _group_query(person, spec: Spec, held, *, resolved: bool):
    qs = spec.model.objects.filter(person=person)
    qs = (qs.exclude(**{spec.concept: NO_MATCHING_CONCEPT_ID}) if resolved
          else qs.filter(**{spec.concept: NO_MATCHING_CONCEPT_ID}))
    device = is_device_row(spec, held)
    qs = qs.annotate(
        group_source=Case(
            When(**{spec.concept: NO_MATCHING_CONCEPT_ID},
                 then=Coalesce(F(spec.source), Value(''))),
            default=Value(''), output_field=CharField()),
        # A device's rows and a hospital's of the same spelling are two groups
        # of unresolved rows, so the mark is the group's, not a guess.
        group_wearable=(Value(False, output_field=BooleanField()) if resolved or not held else
                        Case(When(device, then=Value(True)), default=Value(False),
                             output_field=BooleanField())))
    return (
        qs.values(spec.concept, 'group_source', 'group_wearable')
        .annotate(rows=Count(spec.pk),
                  provisional=Count(spec.pk, filter=Q(confidence__isnull=False)),
                  with_time=Count(spec.pk, filter=Q(**{f'{spec.datetime}__isnull': False})),
                  # The importer's rows under a code the inventory holds as a device's: a
                  # hospital's code of the same spelling is not, its rows carry another type.
                  wearable=(Count(spec.pk, filter=device) if held
                            else Value(0, output_field=IntegerField())),
                  latest_date=Max(spec.date), earliest_date=Min(spec.date),
                  latest_datetime=Max(spec.datetime), earliest_datetime=Min(spec.datetime))
        .order_by('-latest_date', spec.concept, 'group_source', 'group_wearable')[:MAX_GROUPS + 1])


def grouped(person, spec: Spec, held) -> dict:
    """The table as groups: concepts, then the unresolved codes. Three queries.

    *held* is :func:`device_codes_held` for this table; a group is a wearable's
    (``wearable``) when it holds rows of the importer's under such a code.

    ``{'count', 'groups': [...], 'unresolved_groups': [...], 'truncated'}``.
    Concept names are filled in by :func:`name_groups`, once for both tables.
    """
    total = spec.model.objects.filter(person=person).count()
    lists, truncated = {}, False
    for name, resolved in (('groups', True), ('unresolved_groups', False)):
        fetched = list(_group_query(person, spec, held, resolved=resolved))
        truncated = truncated or len(fetched) > MAX_GROUPS
        lists[name] = [{
            'concept_id': row[spec.concept],
            'source_value': row['group_source'] if not resolved else None,
            'count': row['rows'],
            'provisional_rows': row['provisional'],
            'rows_with_time': row['with_time'],
            'wearable': row['wearable'] > 0 if resolved else row['group_wearable'],
            'wearable_rows': row['wearable'],
            'latest_date': row['latest_date'].isoformat(),
            'latest_datetime': _moment(row['latest_date'], row['latest_datetime'], latest=True),
            'earliest_date': row['earliest_date'].isoformat(),
            'earliest_datetime': _moment(row['earliest_date'], row['earliest_datetime'], latest=False),
        } for row in fetched[:MAX_GROUPS]]
    return {'count': total, 'truncated': truncated, **lists}


def name_groups(tables) -> None:
    """Add ``concept_name``, ``concept_code`` and ``vocabulary_id`` to every group, in place. One query."""
    ids = {group['concept_id'] for table in tables for group in table['groups']}
    names = {c.concept_id: c for c in Concept.objects.filter(concept_id__in=ids).only(
        'concept_id', 'concept_name', 'concept_code', 'vocabulary_id')} if ids else {}
    for table in tables:
        for group in table['groups']:
            concept = names.get(group['concept_id'])
            group['concept_name'] = concept.concept_name if concept else None
            group['concept_code'] = concept.concept_code if concept else None
            group['vocabulary_id'] = concept.vocabulary_id if concept else None
