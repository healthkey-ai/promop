"""A pending source pair remembers the day it was entered (#2426).

The day lets derivation retire the pair once a newer imported result exists,
while a same-day result, an unusable one or a legacy pair without a day leaves
the patient's answer in place.
"""
from datetime import date, timedelta

import pytest

from omop_core.models import PatientRecord, Person
from omop_core.services.source_pair_dates import (
    SOURCE_PAIR_CANONICALS,
    forget_dates,
    forget_unedited_dates,
    pair_sources_in,
    remember_dates,
    stored_dates,
    superseded_sources,
)

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 11)


def _record():
    person = Person.objects.create(person_id=99242)
    return PatientRecord.objects.create(person=person)


def test_every_lab_pair_is_in_the_inventory():
    sources = set(SOURCE_PAIR_CANONICALS)
    assert {
        'hemoglobin_level', 'serum_creatinine_level', 'serum_bilirubin_level_direct',
        'albumin_level', 'serum_calcium_level', 'serum_bilirubin_level_total',
        'absolute_neutrophile_count', 'absolute_lymphocyte_count', 'platelet_count',
    } <= sources
    # The unit half of a pair resolves to its source, never to another pair.
    assert pair_sources_in({'hemoglobin_level_units'}) == {'hemoglobin_level'}
    assert pair_sources_in({'albumin_level_units'}) == {'albumin_level'}
    assert pair_sources_in({'sodium_meq_l'}) == set()


def test_dates_round_trip_and_ignore_unknown_pairs():
    record = _record()
    entered = TODAY - timedelta(days=3)

    assert remember_dates(record, {'hemoglobin_level': entered})
    assert stored_dates(record) == {'hemoglobin_level': entered}
    # A field that is not a pair is never recorded.
    assert not remember_dates(record, {'sodium_meq_l': entered})
    assert stored_dates(record) == {'hemoglobin_level': entered}


def test_an_unreadable_stored_day_is_ignored():
    record = _record()
    record.pending_source_pair_dates = {
        'hemoglobin_level': 'not-a-date',
        'albumin_level': '2026-10-01',
    }

    assert stored_dates(record) == {'albumin_level': date(2026, 10, 1)}


def test_a_pair_is_retired_only_by_a_strictly_newer_usable_result():
    record = _record()
    entered = TODAY - timedelta(days=2)
    remember_dates(record, {
        'hemoglobin_level': entered,
        'albumin_level': entered,
        'platelet_count': entered,
    })
    pending = set(record.pending_source_pair_dates) | {'hemoglobin_level_units'}

    superseded = superseded_sources(
        pending,
        stored_dates(record),
        {
            # newer, usable
            'hemoglobin_g_dl': TODAY,
            # same day
            'albumin_g_dl': entered,
            # newer but the extractor selected no value
            'platelet_count_thousand_per_ul': None,
        },
        {
            'hemoglobin_g_dl': 8.0,
            'albumin_g_dl': 3.5,
            'platelet_count_thousand_per_ul': None,
        },
    )

    assert superseded == {'hemoglobin_level'}


def test_a_legacy_pair_without_a_day_is_never_retired():
    superseded = superseded_sources(
        {'albumin_level', 'albumin_level_units'},
        {},
        {'albumin_g_dl': TODAY},
        {'albumin_g_dl': 4.0},
    )

    assert superseded == set()


def test_a_pair_that_is_no_longer_pending_forgets_its_day():
    record = _record()
    remember_dates(record, {
        'hemoglobin_level': TODAY,
        'albumin_level': TODAY,
    })

    # The still-pending pair keeps its day; the retired one loses it.
    assert forget_unedited_dates(record, {'hemoglobin_level', 'hemoglobin_level_units'})
    assert stored_dates(record) == {'hemoglobin_level': TODAY}

    assert forget_dates(record, {'hemoglobin_level'})
    assert stored_dates(record) == {}
