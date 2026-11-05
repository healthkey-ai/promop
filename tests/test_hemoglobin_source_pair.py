"""Hemoglobin's source pair and its normalised read-model (#2318).

The patient records the number in the unit their lab used; `hemoglobin_g_dl`
is the derived g/dL value that matching and CRAB read. The pair is the write
target, so an editor never has to guess the conversion and the chosen unit is
not overwritten by the canonical copy (the old alias direction).
"""
import pytest

from omop_core.models import PatientRecord, Person
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.api.serializers import PatientRecordSerializer

pytestmark = pytest.mark.django_db

PERSON_ID = 99001


def _record():
    person = Person.objects.create(person_id=PERSON_ID)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    return record


def _patch(record, data):
    serializer = PatientRecordSerializer(record, data=data, partial=True)
    return serializer


def test_the_descriptor_points_the_editor_at_the_source_pair():
    descriptor = build_writable_field_descriptor()

    assert descriptor['hemoglobin_level']['kind'] == 'direct'
    assert descriptor['hemoglobin_level']['writable'] is True
    assert descriptor['hemoglobin_level']['target'] == 'patient_record'
    assert descriptor['hemoglobin_level']['unit_options'] == [
        {'value': 'G/DL', 'label': 'g/dL'},
        {'value': 'G/L', 'label': 'g/L'},
    ]
    assert descriptor['hemoglobin_level_units']['kind'] == 'selectable'
    assert descriptor['hemoglobin_level_units']['qualifies'] == 'hemoglobin_level'
    # The canonical is a read-model now, not a second place to type a number.
    assert descriptor['hemoglobin_g_dl']['kind'] == 'computed'
    assert descriptor['hemoglobin_g_dl']['writable'] is False
    assert descriptor['hemoglobin_g_dl']['inputs'] == ['hemoglobin_level']


def test_a_pair_in_g_per_l_is_stored_and_normalised_to_g_per_dl():
    record = _record()
    serializer = _patch(record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L',
    })
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    record.refresh_from_db()
    assert float(record.hemoglobin_level) == 120
    assert record.hemoglobin_level_units == 'G/L'
    assert float(record.hemoglobin_g_dl) == 12.0


def test_a_pair_in_g_per_dl_keeps_the_number():
    record = _record()
    serializer = _patch(record, {
        'hemoglobin_level': 12, 'hemoglobin_level_units': 'g/dL',
    })
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    record.refresh_from_db()
    assert float(record.hemoglobin_g_dl) == 12.0


def test_the_same_number_in_another_unit_is_a_different_answer():
    record = _record()
    first = _patch(record, {
        'hemoglobin_level': 12, 'hemoglobin_level_units': 'g/dL',
    })
    assert first.is_valid(), first.errors
    first.save()

    second = _patch(record, {
        'hemoglobin_level': 12, 'hemoglobin_level_units': 'g/L',
    })
    assert second.is_valid(), second.errors
    second.save()

    record.refresh_from_db()
    assert float(record.hemoglobin_g_dl) == 1.2


def test_a_missing_unit_defaults_to_the_stored_one():
    record = _record()
    record.hemoglobin_level_units = 'G/L'
    record.save()

    serializer = _patch(record, {'hemoglobin_level': 150})
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    record.refresh_from_db()
    assert float(record.hemoglobin_g_dl) == 15.0


def test_a_unit_without_its_value_is_refused():
    record = _record()
    serializer = _patch(record, {'hemoglobin_level_units': 'g/L'})

    assert not serializer.is_valid()
    assert 'hemoglobin_level_units' in serializer.errors


def test_an_unknown_unit_is_refused():
    record = _record()
    serializer = _patch(record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'mmol/L',
    })

    assert not serializer.is_valid()
    assert 'hemoglobin_level_units' in serializer.errors


def test_recompute_keeps_the_source_pair_while_it_is_pending():
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert float(record.hemoglobin_level) == 120
    assert record.hemoglobin_level_units == 'G/L'
    assert float(record.hemoglobin_g_dl) == 12


def test_a_new_canonical_import_retires_the_pending_pair():
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    record.hemoglobin_g_dl = 13
    recompute_patient_record_fields(record, changed_fields=('hemoglobin_g_dl',))

    record.refresh_from_db()
    assert float(record.hemoglobin_level) == 13
    assert record.hemoglobin_level_units == 'G/DL'
    assert float(record.hemoglobin_g_dl) == 13
