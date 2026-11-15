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
    pair_fields_match,
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


def _pair_record(**values):
    from types import SimpleNamespace
    return SimpleNamespace(**values)


def test_a_stored_unit_spelling_matches_its_canonical_code():
    record = _pair_record(
        hemoglobin_level=120, hemoglobin_level_units='g/dl')
    submitted = {'hemoglobin_level': 120, 'hemoglobin_level_units': 'G/DL'}

    assert pair_fields_match(
        {'hemoglobin_level', 'hemoglobin_level_units'}, submitted, record)


def test_an_unknown_stored_unit_is_not_an_echo():
    record = _pair_record(
        hemoglobin_level=120, hemoglobin_level_units='bananas')
    submitted = {'hemoglobin_level': 120, 'hemoglobin_level_units': 'G/DL'}

    assert not pair_fields_match(
        {'hemoglobin_level', 'hemoglobin_level_units'}, submitted, record)


def test_two_unknown_spellings_are_not_evidence_of_the_same_unit():
    record = _pair_record(
        hemoglobin_level=120, hemoglobin_level_units='bananas')
    submitted = {'hemoglobin_level': 120, 'hemoglobin_level_units': 'plantains'}

    assert not pair_fields_match(
        {'hemoglobin_level', 'hemoglobin_level_units'}, submitted, record)


def test_plain_equality_still_covers_none_and_a_shared_unknown():
    record = _pair_record(
        hemoglobin_level=None, hemoglobin_level_units=None)
    fields = {'hemoglobin_level', 'hemoglobin_level_units'}

    assert pair_fields_match(
        fields, {'hemoglobin_level': None, 'hemoglobin_level_units': None}, record)
    assert pair_fields_match(
        fields, {'hemoglobin_level': None}, record)


def test_a_missing_unit_falls_back_to_the_stored_one():
    record = _pair_record(
        absolute_neutrophile_count=1550,
        absolute_neutrophile_count_units='K/UL')

    # The shared form lists the companion unit without writing it.
    assert pair_fields_match(
        {'absolute_neutrophile_count', 'absolute_neutrophile_count_units'},
        {'absolute_neutrophile_count': 1550}, record)


def test_a_missing_or_empty_stored_unit_is_not_an_echo():
    fields = {'hemoglobin_level', 'hemoglobin_level_units'}
    submitted = {'hemoglobin_level': 120, 'hemoglobin_level_units': 'G/DL'}

    for stored in (None, ''):
        record = _pair_record(
            hemoglobin_level=120, hemoglobin_level_units=stored)
        assert not pair_fields_match(fields, submitted, record)


def test_a_float_source_value_compares_exactly():
    # ALC is a FloatField: the value re-read from the column must not read as
    # an authored edit because of a type mismatch.
    fields = {'absolute_lymphocyte_count'}
    record = _pair_record(absolute_lymphocyte_count=1.5)

    assert pair_fields_match(
        fields, {'absolute_lymphocyte_count': 1.5}, record)
    assert not pair_fields_match(
        fields, {'absolute_lymphocyte_count': 1.6}, record)


def test_a_changed_number_is_never_an_echo():
    record = _pair_record(
        hemoglobin_level=120, hemoglobin_level_units='G/DL')

    assert not pair_fields_match(
        {'hemoglobin_level', 'hemoglobin_level_units'},
        {'hemoglobin_level': 130, 'hemoglobin_level_units': 'G/DL'}, record)


def test_count_units_normalize_to_their_storage_codes():
    fields = {'absolute_neutrophile_count', 'absolute_neutrophile_count_units'}
    record = _pair_record(
        absolute_neutrophile_count=1550,
        absolute_neutrophile_count_units='K/UL')

    assert pair_fields_match(
        fields,
        {'absolute_neutrophile_count': 1550,
         'absolute_neutrophile_count_units': '10*3/uL'}, record)
    # Uppercase G/L is the legacy giga-count spelling; lowercase g/L is a mass
    # concentration and must never be read as the same unit.
    record.absolute_neutrophile_count_units = 'G/L'
    assert not pair_fields_match(
        fields,
        {'absolute_neutrophile_count': 1550,
         'absolute_neutrophile_count_units': 'g/L'}, record)


def test_each_chemistry_pair_uses_its_own_unit_codes():
    record = _pair_record(
        albumin_level=40, albumin_level_units='g/l',
        serum_bilirubin_level_total=17.1,
        serum_bilirubin_level_total_units='umol/l')

    assert pair_fields_match(
        {'albumin_level', 'albumin_level_units'},
        {'albumin_level': 40, 'albumin_level_units': 'G/L'}, record)
    assert pair_fields_match(
        {'serum_bilirubin_level_total', 'serum_bilirubin_level_total_units'},
        {'serum_bilirubin_level_total': 17.1,
         'serum_bilirubin_level_total_units': 'UMOL/L'}, record)
    # The bilirubin unit is not valid for albumin's canonical.
    assert not pair_fields_match(
        {'serum_bilirubin_level_total', 'serum_bilirubin_level_total_units'},
        {'serum_bilirubin_level_total': 17.1,
         'serum_bilirubin_level_total_units': 'G/L'}, record)


def test_direct_bilirubin_and_creatinine_normalize_their_units():
    record = _pair_record(
        serum_creatinine_level=80.0,
        serum_creatinine_level_units='µmol/L',
        serum_bilirubin_level_direct=3.4,
        serum_bilirubin_level_direct_units='µmol/L')

    assert pair_fields_match(
        {'serum_creatinine_level', 'serum_creatinine_level_units'},
        {'serum_creatinine_level': 80.0,
         'serum_creatinine_level_units': 'UMOL/L'}, record)
    assert pair_fields_match(
        {'serum_bilirubin_level_direct',
         'serum_bilirubin_level_direct_units'},
        {'serum_bilirubin_level_direct': 3.4,
         'serum_bilirubin_level_direct_units': 'UMOL/L'}, record)


def test_a_real_unit_change_is_never_an_echo():
    record = _pair_record(
        hemoglobin_level=120, hemoglobin_level_units='g/dl')

    assert not pair_fields_match(
        {'hemoglobin_level', 'hemoglobin_level_units'},
        {'hemoglobin_level': 120, 'hemoglobin_level_units': 'G/L'}, record)
