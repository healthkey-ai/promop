"""Refractory Status and Relapse Count are explicit overrides (#5605/#5604).

They are inferred from therapy-line outcomes, but an explicit answer must
survive a refresh. The native serializer keeps it in
``PatientRecord.therapy_overrides``; the shared form must use the same contract
rather than writing the derived columns directly.
"""
import pytest

from omop_core.services.patient_form import apply_patient_form_patch, patient_form_descriptors
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.treatment_catalog import REFRACTORY_STATUSES
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def test_descriptor_advertises_the_overrides():
    descriptors = patient_form_descriptors(build_writable_field_descriptor())

    assert descriptors['refractory_status']['writable'] is True
    assert descriptors['relapse_count']['writable'] is True
    assert [option['value'] for option in descriptors['refractory_status']['options']] == REFRACTORY_STATUSES


def test_refractory_and_relapse_are_stored_as_overrides_and_survive_refresh():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(
        person, {'refractory_status': 'Primary Refractory', 'relapse_count': 2},
    )

    assert 'refractory_status' in result.applied
    assert 'relapse_count' in result.applied
    record.refresh_from_db()
    assert record.treatment_refractory_status == 'Primary Refractory'
    assert record.relapse_count == 2
    assert record.therapy_overrides['treatment_refractory_status'] == 'Primary Refractory'
    assert record.therapy_overrides['relapse_count'] == 2

    refresh_patient_record(person)

    record.refresh_from_db()
    assert record.treatment_refractory_status == 'Primary Refractory'
    assert record.relapse_count == 2


def test_clearing_removes_the_override():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    apply_patient_form_patch(
        person, {'treatment_refractory_status': 'Multi-Refractory', 'relapse_count': 3},
    )

    record, _ = apply_patient_form_patch(
        person, {'refractory_status': '', 'relapse_count': None},
    )

    record.refresh_from_db()
    assert 'treatment_refractory_status' not in (record.therapy_overrides or {})
    assert 'relapse_count' not in (record.therapy_overrides or {})


def test_invalid_values_are_rejected():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    _, result = apply_patient_form_patch(
        person, {'refractory_status': 'Nope', 'relapse_count': 2.5},
    )

    assert 'refractory_status' in result.rejected
    assert 'relapse_count' in result.rejected


def test_zero_is_an_explicit_answer_not_a_clear():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(person, {'relapse_count': 0})

    assert 'relapse_count' in result.applied
    record.refresh_from_db()
    assert record.relapse_count == 0
    assert record.therapy_overrides['relapse_count'] == 0
    assert refresh_patient_record(person).relapse_count == 0

    record, _ = apply_patient_form_patch(person, {'relapse_count': None})
    record.refresh_from_db()
    assert 'relapse_count' not in (record.therapy_overrides or {})


def test_a_correction_replaces_the_previous_override():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    apply_patient_form_patch(person, {'refractory_status': 'Primary Refractory'})
    apply_patient_form_patch(person, {'refractory_status': 'Secondary Refractory'})

    record = refresh_patient_record(person)
    assert record.treatment_refractory_status == 'Secondary Refractory'
    assert record.therapy_overrides['treatment_refractory_status'] == 'Secondary Refractory'


def test_negative_relapse_count_is_rejected():
    person = PersonFactory()
    PatientRecordFactory(person=person)

    _, result = apply_patient_form_patch(person, {'relapse_count': -1})

    assert 'relapse_count' in result.rejected


@pytest.mark.parametrize('value', REFRACTORY_STATUSES)
def test_every_catalog_choice_is_stored_verbatim(value):
    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, _ = apply_patient_form_patch(person, {'refractory_status': value})

    record.refresh_from_db()
    assert record.therapy_overrides['treatment_refractory_status'] == value
    assert refresh_patient_record(person).treatment_refractory_status == value


def test_override_edits_are_projected_and_revised():
    from omop_core.models import Observation

    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    apply_patient_form_patch(person, {'relapse_count': 2, 'refractory_status': 'Primary Refractory'})

    count_row = Observation.objects.get(
        person=person, observation_source_value='patient-record:relapse_count')
    status_row = Observation.objects.get(
        person=person, observation_source_value='patient-record:treatment_refractory_status')
    assert count_row.value_as_number == 2
    assert status_row.value_as_string == 'Primary Refractory'

    apply_patient_form_patch(person, {'relapse_count': 3})
    count_row.refresh_from_db()
    assert count_row.value_as_number == 3
    assert record.revisions.filter(
        field='relapse_count', old_value='2', new_value='3').exists()

    apply_patient_form_patch(person, {'relapse_count': None})
    count_row.refresh_from_db()
    assert count_row.value_source_value == 'PatientRecord:cleared'
    assert count_row.value_as_number is None


def test_an_oversized_relapse_count_is_rejected_without_a_database_error():
    person = PersonFactory()
    record = PatientRecordFactory(person=person)

    _, result = apply_patient_form_patch(person, {'relapse_count': 2147483648})

    assert 'relapse_count' in result.rejected
    record.refresh_from_db()
    assert 'relapse_count' not in (record.therapy_overrides or {})


def test_an_override_only_write_recomputes_dependents(monkeypatch):
    from omop_core.services import patient_form as form

    calls = []
    real = form.recompute_patient_record_fields

    def wrapped(record, **kwargs):
        calls.append(set(kwargs.get('changed_fields') or ()))
        return real(record, **kwargs)

    monkeypatch.setattr(form, 'recompute_patient_record_fields', wrapped)
    person = PersonFactory()
    PatientRecordFactory(person=person)

    apply_patient_form_patch(person, {'relapse_count': 4})

    assert calls, 'override-only write skipped the recomputation'
    assert 'relapse_count' in calls[-1]


def test_a_projected_override_clears_the_pending_marker():
    person = PersonFactory()
    record = PatientRecordFactory(person=person)

    apply_patient_form_patch(person, {'relapse_count': 2})
    record.refresh_from_db()
    assert 'relapse_count' not in (record.user_edited_fields or [])
    assert record.therapy_overrides['relapse_count'] == 2
    assert refresh_patient_record(person).relapse_count == 2

    apply_patient_form_patch(person, {'relapse_count': None})
    record.refresh_from_db()
    assert 'relapse_count' not in (record.user_edited_fields or [])


def test_an_alias_only_host_still_projects_the_canonical_fact():
    from omop_core.models import Observation

    person = PersonFactory()
    PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(
        person, {'refractory_status': 'Primary Refractory'},
        allowed_fields={'refractory_status'})

    assert 'refractory_status' in result.applied
    row = Observation.objects.get(
        person=person, observation_source_value='patient-record:treatment_refractory_status')
    assert row.value_as_string == 'Primary Refractory'


def test_a_cleared_override_revision_uses_the_recomputed_value(monkeypatch):
    from omop_core.services import patient_form as form

    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    apply_patient_form_patch(person, {'relapse_count': 2})

    def fake_recompute(record, **kwargs):
        record.relapse_count = 99
        record.save(update_fields=['relapse_count'])
        return record

    monkeypatch.setattr(form, 'recompute_patient_record_fields', fake_recompute)
    apply_patient_form_patch(person, {'relapse_count': None})

    revision = record.revisions.get(
        field='relapse_count', old_value='2')
    assert revision.new_value == '99'


def test_an_explicit_override_survives_recomputation(monkeypatch):
    from omop_core.services import patient_form as form

    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    apply_patient_form_patch(person, {'relapse_count': 2})

    def fake_recompute(record, **kwargs):
        record.relapse_count = 99
        record.save(update_fields=['relapse_count'])
        return record

    monkeypatch.setattr(form, 'recompute_patient_record_fields', fake_recompute)
    apply_patient_form_patch(person, {'relapse_count': 3})

    record.refresh_from_db()
    assert record.relapse_count == 3
    assert record.therapy_overrides['relapse_count'] == 3


def test_an_excluded_canonical_field_stays_unwritable_through_an_alias():
    descriptors = patient_form_descriptors(
        readable_fields={'creatinine_clearance_rate'})
    canonical = descriptors.get('creatinine_clearance_ml_min')
    assert canonical is None or canonical['writable'] is False

    person = PersonFactory()
    PatientRecordFactory(person=person)
    record, result = apply_patient_form_patch(
        person, {'creatinine_clearance_ml_min': 42},
        allowed_fields={'creatinine_clearance_rate'})

    assert 'creatinine_clearance_ml_min' in result.rejected
    record.refresh_from_db()
    assert record.creatinine_clearance_ml_min != 42


def test_an_empty_descriptor_rejects_clinical_writes():
    # An explicitly empty permission descriptor must not fall back to the
    # deployment-wide descriptor: {} means "nothing is allowed".
    person = PersonFactory()
    record = PatientRecordFactory(person=person)

    record, result = apply_patient_form_patch(
        person, {'relapse_count': 2}, descriptor={})

    assert 'relapse_count' in result.rejected
    record.refresh_from_db()
    assert 'relapse_count' not in (record.therapy_overrides or {})
