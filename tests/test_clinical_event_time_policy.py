from datetime import date, datetime

from omop_core.management.commands.bulk_import_fhir_bundle import (
    _observation_effective_time,
)


def test_date_only_observation_keeps_datetime_unknown():
    effective_date, effective_datetime = _observation_effective_time({
        'effectiveDateTime': '2024-02-03',
    })

    assert effective_date == date(2024, 2, 3)
    assert effective_datetime is None


def test_observation_period_start_is_a_defensible_effective_date():
    effective_date, effective_datetime = _observation_effective_time({
        'effectivePeriod': {'start': '2024-02-03T15:45:00Z'},
    })

    assert effective_date == date(2024, 2, 3)
    assert effective_datetime == datetime.fromisoformat('2024-02-03T15:45:00+00:00')


def test_undated_observation_is_rejected_instead_of_stamped_at_import_time():
    assert _observation_effective_time({'valueQuantity': {'value': 7}}) == (None, None)
