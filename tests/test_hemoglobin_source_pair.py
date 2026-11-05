"""Hemoglobin's source pair and its normalised read-model (#2318).

The patient records the number in the unit their lab used; `hemoglobin_g_dl`
is the derived g/dL value that matching and CRAB read. The pair is the write
target, so an editor never has to guess the conversion and the chosen unit is
not overwritten by the canonical copy (the old alias direction).
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord, Person
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.api.serializers import PatientRecordSerializer
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

PERSON_ID = 99001


def _record():
    person = Person.objects.create(person_id=PERSON_ID)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    return record


def _patch(record, data):
    serializer = PatientRecordSerializer(record, data=data, partial=True)
    return serializer


@pytest.fixture
def api(settings):
    """A staff editor, the LOINC/UCUM concepts and the patient-reported type."""
    settings.CELERY_BROKER_URL = ''
    type_vocab = VocabularyFactory(
        vocabulary_id='Type Concept', vocabulary_name='Type Concept',
    )
    ConceptFactory(
        concept_id=32865, concept_code='PT-SELF-REPORT',
        concept_name='Patient self-report', vocabulary=type_vocab,
    )
    ConceptFactory(
        concept_code='718-7', vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    unit = ConceptFactory(
        concept_code='g/dL', vocabulary=VocabularyFactory(vocabulary_id='UCUM'),
    )
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='hemoglobin-pair@example.test', is_staff=True),
    )
    return client, unit


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
    assert descriptor['hemoglobin_g_dl']['value_kind'] == 'number'
    assert descriptor['hemoglobin_g_dl']['inputs'] == ['hemoglobin_level']
    # The server still accepts a canonical import from an older client; the
    # flag is what keeps that path open without advertising it to editors.
    assert descriptor['hemoglobin_g_dl']['normalised_read_model'] is True


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
    # The retired pair must not leave its unit behind as a pending edit.
    assert set(record.user_edited_fields or []) == set()


def _edit(api, record, data):
    client, _unit = api
    response = client.patch(
        f'/api/patient-info/{record.person.person_id}/', data, format='json',
    )
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    return response


def _projected(record):
    return Measurement.objects.get(
        person=record.person, measurement_source_value='718-7',
    )


def test_the_pair_projects_the_normalised_value_with_its_unit(api):
    record = _record()
    response = _edit(api, record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L',
    })

    # The record keeps what the reader chose; the response exposes the
    # normalised read-model beside it.
    assert float(record.hemoglobin_level) == 120
    assert record.hemoglobin_level_units == 'G/L'
    assert float(record.hemoglobin_g_dl) == 12.0
    assert Decimal(str(response.data['hemoglobin_g_dl'])) == Decimal('12.0')

    row = _projected(record)
    assert row.value_as_number == Decimal('12.0')
    assert row.unit_source_value == 'g/dL'
    assert row.unit_concept_id == api[1].pk


def test_a_g_per_dl_pair_projects_its_own_number(api):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 11, 'hemoglobin_level_units': 'g/dL',
    })

    assert float(record.hemoglobin_g_dl) == 11.0
    assert _projected(record).value_as_number == Decimal('11.0')


def test_two_refreshes_keep_the_pair_and_one_today_fact(api):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L',
    })

    for _ in range(2):
        record = refresh_patient_record(record.person)
        record.refresh_from_db()
        assert float(record.hemoglobin_level) == 120
        assert record.hemoglobin_level_units == 'G/L'
        assert float(record.hemoglobin_g_dl) == 12.0
        # The chosen unit is the patient's declaration and derivation cannot
        # reproduce it from a g/dL fact, so the pair stays protected.
        assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
            record.user_edited_fields or [])

    assert Measurement.objects.filter(
        person=record.person, measurement_source_value='718-7',
    ).count() == 1


def test_a_second_pair_edit_reuses_todays_fact(api):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L',
    })
    _edit(api, record, {
        'hemoglobin_level': 11, 'hemoglobin_level_units': 'g/dL',
    })

    assert float(record.hemoglobin_level) == 11
    assert record.hemoglobin_level_units == 'G/DL'
    assert float(record.hemoglobin_g_dl) == 11.0
    rows = Measurement.objects.filter(
        person=record.person, measurement_source_value='718-7',
    )
    assert rows.count() == 1
    assert rows.get().value_as_number == Decimal('11.0')
    assert rows.get().unit_source_value == 'g/dL'


def test_a_refresh_without_omop_backing_keeps_the_canonical_from_the_pair():
    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    refreshed = refresh_patient_record(record.person)

    # No LOINC concept/measurement exists, so the projection never landed.
    # The pair must still supply the read-model matching sees.
    assert float(refreshed.hemoglobin_level) == 120
    assert refreshed.hemoglobin_level_units == 'G/L'
    assert float(refreshed.hemoglobin_g_dl) == 12.0
    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        refreshed.user_edited_fields or [])


def test_a_pair_edit_moves_the_crab_verdict(api):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 8.4, 'hemoglobin_level_units': 'g/dL',
    })
    assert record.meets_crab is True

    _edit(api, record, {
        'hemoglobin_level': 13, 'hemoglobin_level_units': 'g/dL',
    })
    # The canonical is derived from the pair, not typed as its own edit, but
    # the verdict must still follow the new value.
    assert record.meets_crab is None


def test_the_shared_form_normalises_the_source_pair(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person, {'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L'},
    )

    assert {'hemoglobin_level', 'hemoglobin_level_units', 'hemoglobin_g_dl'} <= set(
        result.applied)
    assert float(saved.hemoglobin_level) == 120
    assert saved.hemoglobin_level_units == 'G/L'
    assert float(saved.hemoglobin_g_dl) == 12.0
    row = _projected(saved)
    assert row.value_as_number == Decimal('12.0')
    assert row.unit_source_value == 'g/dL'


def test_the_shared_form_refuses_a_unit_without_its_value(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person, {'hemoglobin_level_units': 'g/L'},
    )

    assert result.rejected.get('hemoglobin_level_units')
    assert not result.applied
    assert saved.hemoglobin_level is None


def test_the_shared_form_refuses_an_unknown_unit(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person,
        {'hemoglobin_level': 120, 'hemoglobin_level_units': 'mmol/L'},
    )

    assert result.rejected.get('hemoglobin_level_units')
    assert not result.applied
    assert saved.hemoglobin_level is None
    assert saved.hemoglobin_g_dl is None


def test_a_canonical_write_retires_a_pending_pair(api):
    from omop_core.services.field_write_service import apply_field_writes

    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    apply_field_writes(record.person, {'hemoglobin_g_dl': 13})

    record.refresh_from_db()
    assert float(record.hemoglobin_g_dl) == 13.0
    assert float(record.hemoglobin_level) == 13.0
    assert record.hemoglobin_level_units == 'G/DL'
    assert not ({'hemoglobin_level', 'hemoglobin_level_units'}
                & set(record.user_edited_fields or []))


def test_clearing_the_pair_clears_the_projected_fact(api):
    from omop_core.services.omop_projection import CLEAR_VALUE

    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L',
    })
    _edit(api, record, {
        'hemoglobin_level': None, 'hemoglobin_level_units': 'g/L',
    })

    assert record.hemoglobin_level is None
    assert record.hemoglobin_g_dl is None
    row = _projected(record)
    assert row.value_as_number is None
    assert row.value_source_value == CLEAR_VALUE


def test_the_unit_label_is_case_insensitive(api):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/l',
    })

    assert record.hemoglobin_level_units == 'G/L'
    assert float(record.hemoglobin_g_dl) == 12.0


@pytest.mark.parametrize('value,unit', [(12, 'G/DL'), (120, 'G/L')])
def test_the_pair_stays_pending_for_both_units(api, value, unit):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': value, 'hemoglobin_level_units': unit,
    })
    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        record.user_edited_fields or [])

    record = refresh_patient_record(record.person)

    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        record.user_edited_fields or [])



def test_the_shared_form_cannot_write_a_withheld_pair(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    record.hemoglobin_level = 12
    record.hemoglobin_level_units = 'G/DL'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    saved, result = apply_patient_form_patch(
        record.person,
        {'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L'},
        allowed_fields={'hemoglobin_g_dl'},
    )

    # The host withheld the source; the canonical must not carry the pair's
    # normalised value and the unit must not change either.
    assert 'hemoglobin_level' in result.rejected
    assert not result.applied
    saved.refresh_from_db()
    assert float(saved.hemoglobin_level) == 12
    assert saved.hemoglobin_level_units == 'G/DL'
    assert float(saved.hemoglobin_g_dl) == 12


def test_a_caller_read_only_canonical_cannot_be_written_through_the_form(api):
    from omop_core.services.patient_form import apply_patient_form_patch
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    record = _record()
    record.hemoglobin_g_dl = 12
    record.save()
    descriptor = build_writable_field_descriptor()
    descriptor['hemoglobin_g_dl'].update(
        writable=True, read_only_for_caller=True,
    )

    saved, result = apply_patient_form_patch(
        record.person, {'hemoglobin_g_dl': 13}, descriptor=descriptor,
    )

    assert 'hemoglobin_g_dl' in result.rejected
    assert not result.applied
    saved.refresh_from_db()
    assert float(saved.hemoglobin_g_dl) == 12


def test_a_caller_read_only_canonical_cannot_be_written_by_the_applier(api):
    from omop_core.services.field_write_service import apply_field_writes
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    record = _record()
    descriptor = build_writable_field_descriptor()
    descriptor['hemoglobin_g_dl'].update(
        writable=True, read_only_for_caller=True,
    )

    result = apply_field_writes(
        record.person, {'hemoglobin_g_dl': 13}, descriptor=descriptor,
    )

    assert 'hemoglobin_g_dl' in result.rejected
    assert not Measurement.objects.filter(person=record.person).exists()


def test_a_canonical_write_retires_the_pair_without_a_refresh(api):
    from omop_core.services.field_write_service import apply_field_writes

    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    apply_field_writes(record.person, {'hemoglobin_g_dl': 13}, refresh=False)

    record.refresh_from_db()
    assert not ({'hemoglobin_level', 'hemoglobin_level_units'}
                & set(record.user_edited_fields or []))
    # The caller owns the refresh; when it runs, the canonical wins and the
    # old source pair is replaced rather than preserved beside it.
    record = refresh_patient_record(record.person)
    assert float(record.hemoglobin_level) == 13.0
    assert record.hemoglobin_level_units == 'G/DL'
    assert float(record.hemoglobin_g_dl) == 13.0


def test_the_projector_refuses_a_caller_read_only_canonical(api):
    from omop_core.services.omop_projection import projection_for_descriptor
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    entry = build_writable_field_descriptor()['hemoglobin_g_dl']
    entry.update(writable=True, read_only_for_caller=True)

    assert projection_for_descriptor(entry) is None


def test_the_shared_form_cannot_change_a_caller_read_only_unit(api):
    from omop_core.services.patient_form import apply_patient_form_patch
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    record = _record()
    record.hemoglobin_level = 12
    record.hemoglobin_level_units = 'G/DL'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()
    descriptor = build_writable_field_descriptor()
    descriptor['hemoglobin_level_units'].update(
        writable=True, read_only_for_caller=True,
    )

    saved, result = apply_patient_form_patch(
        record.person,
        {'hemoglobin_level': 120, 'hemoglobin_level_units': 'g/L'},
        descriptor=descriptor,
    )

    # The unit is the companion half of the pair: a caller who may not change
    # it must not be able to save the number in a different unit either.
    assert 'hemoglobin_level_units' in result.rejected
    assert not result.applied
    saved.refresh_from_db()
    assert float(saved.hemoglobin_level) == 12
    assert saved.hemoglobin_level_units == 'G/DL'
    assert float(saved.hemoglobin_g_dl) == 12


def test_a_caller_read_only_unit_cannot_be_initialised(api):
    from omop_core.services.patient_form import apply_patient_form_patch
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    record = _record()
    # The column defaults to G/DL, so clear it: this is the path where the
    # value would have to initialise a unit the caller may not choose.
    record.hemoglobin_level_units = None
    record.save()
    descriptor = build_writable_field_descriptor()
    descriptor['hemoglobin_level_units'].update(
        writable=True, read_only_for_caller=True,
    )

    saved, result = apply_patient_form_patch(
        record.person, {'hemoglobin_level': 12}, descriptor=descriptor,
    )

    # The record has no unit, so writing the value would have to initialise
    # one the caller may not choose.
    assert 'hemoglobin_level_units' in result.rejected
    assert not result.applied
    saved.refresh_from_db()
    assert saved.hemoglobin_level is None
    assert saved.hemoglobin_level_units is None
    assert saved.hemoglobin_g_dl is None


def _read_only_unit_descriptor(api):
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    descriptor = build_writable_field_descriptor()
    descriptor['hemoglobin_level_units'].update(
        writable=True, read_only_for_caller=True,
    )
    return descriptor


def test_a_caller_read_only_unit_can_be_used_as_stored(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = []
    record.save()

    saved, result = apply_patient_form_patch(
        record.person, {'hemoglobin_level': 130},
        descriptor=_read_only_unit_descriptor(api),
    )

    # The value may change in the unit the record already uses; the unit
    # itself is not rewritten, but it has to stay protected with its value.
    assert 'hemoglobin_level' in result.applied
    assert 'hemoglobin_level_units' not in result.applied
    saved.refresh_from_db()
    assert float(saved.hemoglobin_level) == 130
    assert saved.hemoglobin_level_units == 'G/L'
    assert float(saved.hemoglobin_g_dl) == 13.0

    refreshed = refresh_patient_record(saved.person)
    assert float(refreshed.hemoglobin_level) == 130
    assert refreshed.hemoglobin_level_units == 'G/L'
    assert float(refreshed.hemoglobin_g_dl) == 13.0


def test_a_caller_read_only_unit_is_not_rewritten_when_echoed(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    record.hemoglobin_level = 120
    record.hemoglobin_level_units = 'G/L'
    record.hemoglobin_g_dl = 12
    record.user_edited_fields = []
    record.save()

    saved, result = apply_patient_form_patch(
        record.person,
        {'hemoglobin_level': 130, 'hemoglobin_level_units': 'g/L'},
        descriptor=_read_only_unit_descriptor(api),
    )

    assert 'hemoglobin_level' in result.applied
    assert 'hemoglobin_level_units' not in result.applied
    saved.refresh_from_db()
    # The stored code stays the code, not the echoed display label, and the
    # value is normalised in the stored unit.
    assert saved.hemoglobin_level_units == 'G/L'
    assert float(saved.hemoglobin_g_dl) == 13.0


def test_the_canonical_helpers_match_the_stored_column():
    from omop_core.services.hemoglobin_units import CANONICAL_LIMIT, CANONICAL_QUANTUM

    field = PatientRecord._meta.get_field('hemoglobin_g_dl')
    assert CANONICAL_QUANTUM == Decimal(1).scaleb(-field.decimal_places)
    assert CANONICAL_LIMIT == (
        Decimal(10) ** (field.max_digits - field.decimal_places) - CANONICAL_QUANTUM
    )


def test_the_derived_canonical_already_has_the_stored_precision():
    from omop_core.services.hemoglobin_units import canonical_from_pair

    # 99.99 g/L is 9.999 g/dL. The column stores one decimal, so the value the
    # caller projects must be that same 10.0, not a more precise number that
    # the database would round separately.
    recognised, canonical = canonical_from_pair(Decimal('99.99'), 'G/L')
    assert recognised is True
    assert canonical == Decimal('10.0')

    record = _record()
    serializer = _patch(record, {
        'hemoglobin_level': Decimal('99.99'), 'hemoglobin_level_units': 'g/L',
    })
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data['hemoglobin_g_dl'] == Decimal('10.0')
    serializer.save()

    record.refresh_from_db()
    assert record.hemoglobin_g_dl == Decimal('10.0')


def test_the_projected_fact_carries_the_same_precision(api):
    record = _record()
    _edit(api, record, {
        'hemoglobin_level': 99.99, 'hemoglobin_level_units': 'g/L',
    })

    assert record.hemoglobin_g_dl == Decimal('10.0')
    assert _projected(record).value_as_number == Decimal('10.0')


def test_a_pair_outside_the_canonical_range_is_refused():
    record = _record()
    serializer = _patch(record, {
        'hemoglobin_level': 2_000_000, 'hemoglobin_level_units': 'g/L',
    })

    assert not serializer.is_valid()
    assert 'hemoglobin_level' in serializer.errors


def test_the_shared_form_refuses_a_pair_outside_the_canonical_range(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person,
        {'hemoglobin_level': 2_000_000, 'hemoglobin_level_units': 'g/L'},
    )

    assert result.rejected.get('hemoglobin_level')
    assert not result.applied
    assert saved.hemoglobin_level is None
    assert saved.hemoglobin_g_dl is None


def test_recompute_does_not_derive_an_unstorable_canonical():
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.hemoglobin_level = Decimal('2000000')
    record.hemoglobin_level_units = 'G/L'
    record.user_edited_fields = ['hemoglobin_level', 'hemoglobin_level_units']
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('2000000')
    assert record.hemoglobin_g_dl is None
