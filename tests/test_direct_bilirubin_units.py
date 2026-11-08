"""Direct bilirubin: the stored pair and its normalised mg/dL mirror (#2318).

Direct bilirubin has no canonical column. `serum_bilirubin_level_direct` plus
its unit are the fact the matcher converts, and the unitless
`serum_bilirubin_level` alias mirrors the normalised mg/dL value.
"""
from datetime import date
from decimal import Decimal

import pytest

from omop_core.models import Measurement
from omop_core.services import direct_bilirubin_units as units
from omop_core.services.omop_projection import project_single_value
from omop_core.services.patient_record_service import (
    recompute_patient_record_fields,
    refresh_patient_record,
)
from omop_core.services.write_descriptor import (
    build_writable_field_descriptor, get_serializer_read_only_fields,
)
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

LOINC_DIRECT = '1968-7'


@pytest.mark.parametrize('value,unit,expected', [
    (Decimal('17.10'), 'umol/L', Decimal('1.00')),
    (Decimal('10'), 'UMOL/L', Decimal('0.58')),
    (Decimal('1.2'), 'mg/dL', Decimal('1.20')),
])
def test_the_alias_is_the_normalised_mg_dl_value(value, unit, expected):
    recognised, alias = units.alias_from_pair(value, unit)

    assert recognised
    assert alias == expected


@pytest.mark.parametrize('unit', ['umol/L', 'µmol/L', 'μmol/L', ' UMOL/L '])
def test_micro_spellings_are_one_unit(unit):
    assert units.normalize_unit(unit) == 'UMOL/L'


def test_a_missing_unit_is_the_documented_mass_volume_default():
    assert units.alias_from_pair(Decimal('1.2'), None) == (True, Decimal('1.20'))
    assert units.alias_from_pair(Decimal('1.2'), '') == (True, Decimal('1.20'))


@pytest.mark.parametrize('unit', ['mg/L', 'g/dL', 'mEq/L'])
def test_an_explicit_unsupported_unit_has_no_alias(unit):
    assert units.alias_from_pair(Decimal('1.2'), unit) == (False, None)


def test_a_clear_is_recognised():
    assert units.alias_from_pair(None, 'mg/L') == (True, None)


@pytest.mark.parametrize('value', ['bad', True, float('nan'), float('inf'), -1, Decimal('100000000')])
def test_an_invalid_pair_value_is_not_a_fact(value):
    assert units.pair_value(value) == (False, None)


@pytest.mark.parametrize('value', ['1e100', Decimal('1e100'), Decimal('-0.001')])
def test_the_alias_refuses_an_out_of_range_source(value):
    assert units.alias_from_pair(value, 'MG/DL') == (False, None)


def test_the_pair_is_quantised_to_the_column_precision():
    recognised, value = units.pair_value(Decimal('1.234'))

    assert recognised
    assert value == Decimal('1.23')


def test_the_descriptor_exposes_the_pair_and_a_read_only_alias():
    descriptor = build_writable_field_descriptor()

    pair = descriptor[units.PAIR_FIELD]
    assert pair['writable'] is True
    assert pair['projection']['unit_from'] == units.UNIT_FIELD
    assert pair['projection']['unit_by_code']['UMOL/L']['label'] == 'umol/L'

    unit = descriptor[units.UNIT_FIELD]
    assert unit['qualifies'] == units.PAIR_FIELD

    alias = descriptor[units.ALIAS_FIELD]
    assert alias['writable'] is False
    assert alias['inputs'] == [units.PAIR_FIELD, units.UNIT_FIELD]


def test_an_import_keeps_the_reported_pair_and_normalises_the_alias():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=10,
        unit_source_value='umol/L',
    )

    record = refresh_patient_record(person)

    assert record.serum_bilirubin_level_direct == Decimal('10')
    assert record.serum_bilirubin_level_direct_units == 'UMOL/L'
    assert record.serum_bilirubin_level == Decimal('0.58')


def test_a_unitless_import_uses_the_mass_volume_default():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=1.2,
    )

    record = refresh_patient_record(person)

    assert record.serum_bilirubin_level_direct == Decimal('1.20')
    assert record.serum_bilirubin_level_direct_units == 'MG/DL'
    assert record.serum_bilirubin_level == Decimal('1.20')


def test_a_ucum_unit_concept_is_used_when_the_source_text_is_absent():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=10,
        unit_concept=ConceptFactory(
            vocabulary=VocabularyFactory(vocabulary_id='UCUM'),
            concept_code='umol/L'),
    )

    record = refresh_patient_record(person)

    assert record.serum_bilirubin_level_direct_units == 'UMOL/L'
    assert record.serum_bilirubin_level == Decimal('0.58')


def test_a_non_ucum_unit_concept_blocks_the_pair_and_alias():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=10,
        unit_concept=ConceptFactory(
            vocabulary=VocabularyFactory(vocabulary_id='LOCAL'),
            concept_code='mg/L'),
    )

    record = refresh_patient_record(person)

    assert record.serum_bilirubin_level_direct is None
    assert record.serum_bilirubin_level_direct_units is None
    assert record.serum_bilirubin_level is None


def test_the_newest_unsupported_result_blocks_an_older_convertible_one():
    person = PersonFactory()
    concept = ConceptFactory(concept_code=LOINC_DIRECT)
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value=LOINC_DIRECT, measurement_date=date(2024, 1, 1),
        value_as_number=10, unit_source_value='umol/L',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value=LOINC_DIRECT, measurement_date=date(2024, 1, 2),
        value_as_number=10, unit_source_value='unknown',
    )

    record = refresh_patient_record(person)

    assert record.serum_bilirubin_level_direct is None
    assert record.serum_bilirubin_level_direct_units is None
    assert record.serum_bilirubin_level is None


def test_recompute_normalises_the_alias_from_the_pair():
    record = PatientRecordFactory()
    record.serum_bilirubin_level_direct = Decimal('10')
    record.serum_bilirubin_level_direct_units = 'UMOL/L'
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.serum_bilirubin_level == Decimal('0.58')


def test_a_pending_alias_is_not_silently_reinterpreted():
    record = PatientRecordFactory()
    record.serum_bilirubin_level_direct = Decimal('10')
    record.serum_bilirubin_level_direct_units = 'UMOL/L'
    record.serum_bilirubin_level = Decimal('0.70')
    record.user_edited_fields = ['serum_bilirubin_level']
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.serum_bilirubin_level == Decimal('0.70')


def test_projection_carries_the_reported_unit_not_the_recipe_default():
    person = PersonFactory()
    from omop_core.models import PatientRecord
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record.serum_bilirubin_level_direct = Decimal('10')
    record.serum_bilirubin_level_direct_units = 'UMOL/L'
    record.save()
    type_concept = ConceptFactory(concept_id=32817, concept_name='EHR')
    concept = ConceptFactory(concept_id=1968007, concept_code=LOINC_DIRECT)
    projection = {
        'omop_table': 'measurement', 'concept_id': concept.concept_id,
        'source_value': LOINC_DIRECT, 'type_concept_id': type_concept.concept_id,
        'unit_from': units.UNIT_FIELD,
        'unit_by_code': {
            'MG/DL': {'label': 'mg/dL', 'concept_id': None},
            'UMOL/L': {'label': 'umol/L', 'concept_id': None},
        },
    }

    assert project_single_value(
        person, units.PAIR_FIELD, Decimal('10'), projection,
        fact_date=date(2026, 1, 1),
    )

    row = Measurement.objects.get(person=person)
    assert row.value_as_number == Decimal('10')
    assert row.unit_source_value == 'umol/L'
    assert row.unit_concept_id is None


def test_projection_without_a_usable_unit_stays_pending():
    person = PersonFactory()
    from omop_core.models import PatientRecord
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record.serum_bilirubin_level_direct = Decimal('10')
    record.save()
    # The column defaults to MG/DL; bypass it to model a legacy row with no
    # usable unit.
    type(record).objects.filter(pk=record.pk).update(
        **{units.UNIT_FIELD: None})
    type_concept = ConceptFactory(concept_id=32817, concept_name='EHR')
    concept = ConceptFactory(concept_id=1968008, concept_code=LOINC_DIRECT)
    projection = {
        'omop_table': 'measurement', 'concept_id': concept.concept_id,
        'source_value': LOINC_DIRECT, 'type_concept_id': type_concept.concept_id,
        'unit_from': units.UNIT_FIELD,
        'unit_by_code': {'MG/DL': {'label': 'mg/dL', 'concept_id': None}},
    }

    assert not project_single_value(
        person, units.PAIR_FIELD, Decimal('10'), projection,
        fact_date=date(2026, 1, 1),
    )

    assert not Measurement.objects.filter(person=person).exists()


def test_the_alias_is_not_a_client_write_target():
    assert units.ALIAS_FIELD in get_serializer_read_only_fields()
    assert build_writable_field_descriptor()[units.ALIAS_FIELD]['writable'] is False


def test_the_native_serializer_validates_and_normalises_the_pair():
    from patient_portal.api.serializers import PatientRecordSerializer

    record = PatientRecordFactory()
    serializer = PatientRecordSerializer(
        record, data={
            units.PAIR_FIELD: '34.20',
            units.UNIT_FIELD: 'umol/L',
        }, partial=True,
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data[units.PAIR_FIELD] == Decimal('34.20')
    assert serializer.validated_data[units.UNIT_FIELD] == 'UMOL/L'


def test_the_native_serializer_refuses_a_unit_on_its_own():
    from patient_portal.api.serializers import PatientRecordSerializer

    record = PatientRecordFactory()
    serializer = PatientRecordSerializer(
        record, data={units.UNIT_FIELD: 'umol/L'}, partial=True)

    assert not serializer.is_valid()
    assert units.UNIT_FIELD in serializer.errors


def test_the_native_serializer_refuses_an_unsupported_unit():
    from patient_portal.api.serializers import PatientRecordSerializer

    record = PatientRecordFactory()
    serializer = PatientRecordSerializer(
        record, data={
            units.PAIR_FIELD: '1.2', units.UNIT_FIELD: 'mg/L',
        }, partial=True)

    assert not serializer.is_valid()
    assert units.UNIT_FIELD in serializer.errors


def test_the_shared_form_writes_the_pair_and_normalises_the_alias():
    from omop_core.services.patient_form import apply_patient_form_patch

    ConceptFactory(
        concept_id=1968007, concept_code=LOINC_DIRECT,
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'))
    person = PersonFactory()
    saved, result = apply_patient_form_patch(
        person, {units.PAIR_FIELD: '34.2', units.UNIT_FIELD: 'umol/L'})

    assert {units.PAIR_FIELD, units.UNIT_FIELD} <= set(result.applied)
    assert saved.serum_bilirubin_level_direct == Decimal('34.20')
    assert saved.serum_bilirubin_level_direct_units == 'UMOL/L'
    assert saved.serum_bilirubin_level == Decimal('2.00')


def test_the_shared_form_refuses_a_unit_on_its_own():
    from omop_core.services.patient_form import apply_patient_form_patch

    person = PersonFactory()
    _saved, result = apply_patient_form_patch(
        person, {units.UNIT_FIELD: 'umol/L'})

    assert result.rejected.get(units.UNIT_FIELD)
    assert not result.applied


def test_a_pending_value_keeps_its_unit_across_a_refresh():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=1,
        unit_source_value='mg/dL',
    )
    from omop_core.models import PatientRecord
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record.serum_bilirubin_level_direct = Decimal('17.10')
    record.serum_bilirubin_level_direct_units = 'UMOL/L'
    record.user_edited_fields = [units.PAIR_FIELD]
    record.save()

    record = refresh_patient_record(person)

    assert record.serum_bilirubin_level_direct == Decimal('17.10')
    assert record.serum_bilirubin_level_direct_units == 'UMOL/L'
    assert record.serum_bilirubin_level == Decimal('1.00')
    assert units.PAIR_FIELD in (record.user_edited_fields or [])


def test_a_pending_pair_is_confirmed_only_when_number_and_unit_match():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=17.10,
        unit_source_value='umol/L',
    )
    from omop_core.models import PatientRecord
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record.serum_bilirubin_level_direct = Decimal('17.10')
    record.serum_bilirubin_level_direct_units = 'UMOL/L'
    record.user_edited_fields = [units.PAIR_FIELD]
    record.save()

    record = refresh_patient_record(person)

    assert units.PAIR_FIELD not in (record.user_edited_fields or [])
    assert record.serum_bilirubin_level_direct_units == 'UMOL/L'
    assert record.serum_bilirubin_level == Decimal('1.00')


def test_the_same_number_in_another_unit_does_not_confirm_the_pair():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=LOINC_DIRECT),
        measurement_source_value=LOINC_DIRECT,
        value_as_number=17.10,
        unit_source_value='mg/dL',
    )
    from omop_core.models import PatientRecord
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record.serum_bilirubin_level_direct = Decimal('17.10')
    record.serum_bilirubin_level_direct_units = 'UMOL/L'
    record.user_edited_fields = [units.PAIR_FIELD]
    record.save()

    record = refresh_patient_record(person)

    assert units.PAIR_FIELD in (record.user_edited_fields or [])
    assert record.serum_bilirubin_level_direct_units == 'UMOL/L'
    assert record.serum_bilirubin_level == Decimal('1.00')
