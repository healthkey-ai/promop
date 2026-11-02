"""Death Date through the shared federated form (#5602).

The native PATCH already writes a dated ``patient-record:death_date``
assertion; the shared form blocked the field instead. These tests pin the
shared path to the same contract: record first, dated assertion as a
best-effort side effect, append rather than overwrite across days, and
clearing recorded as an assertion rather than a silent delete.
"""
from datetime import date, timedelta

import pytest

from omop_core.models import Observation
from omop_core.services.patient_form import apply_patient_form_patch
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

SOURCE = 'patient-record:death_date'
DEATH = date(2020, 1, 1)


def _person_with_record():
    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    return person, record


def test_death_date_is_saved_and_asserted():
    person, record = _person_with_record()

    record, result = apply_patient_form_patch(person, {'death_date': DEATH.isoformat()})

    assert 'death_date' in result.applied
    record.refresh_from_db()
    assert record.death_date == DEATH
    assertion = Observation.objects.get(person=person, observation_source_value=SOURCE)
    assert assertion.value_as_string == DEATH.isoformat()
    assert refresh_patient_record(person).death_date == DEATH


def test_a_correction_on_another_day_appends_history(monkeypatch):
    # Freeze the clock for BOTH calls: the assertion date is the day the fact
    # was recorded, and the validator uses the same clock. A death date is in
    # the past on both days.
    clock = {'today': date(2026, 1, 1)}
    monkeypatch.setattr('django.utils.timezone.localdate', lambda: clock['today'])

    person, _ = _person_with_record()
    first = date(2025, 12, 1)
    apply_patient_form_patch(person, {'death_date': first.isoformat()})

    # A later day is a new assertion, not an edit of the earlier fact.
    clock['today'] = date(2026, 1, 2)
    corrected = date(2025, 12, 2)
    apply_patient_form_patch(person, {'death_date': corrected.isoformat()})

    rows = Observation.objects.filter(
        person=person, observation_source_value=SOURCE,
    ).order_by('observation_date', 'observation_id')
    assert [row.value_as_string for row in rows] == [first.isoformat(), corrected.isoformat()]
    assert refresh_patient_record(person).death_date == corrected


def test_clearing_writes_a_cleared_assertion():
    person, record = _person_with_record()
    apply_patient_form_patch(person, {'death_date': DEATH.isoformat()})

    record, result = apply_patient_form_patch(person, {'death_date': None})

    assert 'death_date' in result.applied
    record.refresh_from_db()
    assert record.death_date is None
    assertion = Observation.objects.get(person=person, observation_source_value=SOURCE)
    assert assertion.value_source_value == 'PatientRecord:cleared'
    assert refresh_patient_record(person).death_date is None


def test_a_future_death_date_is_rejected():
    person, record = _person_with_record()

    with pytest.raises(ValueError, match='cannot be in the future'):
        apply_patient_form_patch(
            person, {'death_date': (date.today() + timedelta(days=1)).isoformat()})

    record.refresh_from_db()
    assert record.death_date is None
    assert not Observation.objects.filter(person=person, observation_source_value=SOURCE).exists()


def test_a_death_date_before_birth_is_rejected():
    person, record = _person_with_record()
    record.date_of_birth = date(2020, 6, 1)
    record.save(update_fields=['date_of_birth'])

    with pytest.raises(ValueError, match='cannot precede date of birth'):
        apply_patient_form_patch(person, {'death_date': DEATH.isoformat()})

    record.refresh_from_db()
    assert record.death_date is None


def test_the_shared_descriptor_projects_the_dated_assertion():
    descriptors = build_writable_field_descriptor()
    entry = descriptors['death_date']
    assert entry['writable'] is True
    assert entry['projection']['source_value'] == SOURCE
    assert entry['projection']['best_effort'] is True


def test_a_host_that_withholds_the_field_cannot_write_it():
    person, record = _person_with_record()

    _, result = apply_patient_form_patch(
        person, {'death_date': DEATH.isoformat()}, allowed_fields=set())

    assert 'death_date' in result.rejected
    record.refresh_from_db()
    assert record.death_date is None
