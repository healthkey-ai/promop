"""Chemistry imports are converted to their canonical unit (#2318).

Albumin, serum calcium and total bilirubin arrive from labs in either the
conventional or the SI scale. OMOP stores the number in a fixed-unit canonical
column, so the projection must convert; otherwise a `40 g/L` albumin is read as
`40` g/dL and an SI bilirubin is off by 17x. The source pair keeps what the lab
reported, and the canonical is the normalised read-model.
"""
from datetime import date
from decimal import Decimal

import pytest

from omop_core.models import FieldConceptMapping, Measurement
from omop_core.services.chemistry_units import (
    PAIRS,
    canonical_from_pair,
    canonical_in_range,
    normalize_unit,
)
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('field,value,unit,expected', [
    # Golden numbers written independently of the registry.
    ('albumin_g_dl', 40, 'g/L', Decimal('4.0')),
    ('albumin_g_dl', 4.2, 'g/dL', Decimal('4.2')),
    ('serum_calcium_mg_dl', 2.50, 'mmol/L', Decimal('10.0')),
    ('serum_calcium_mg_dl', 10.0, 'mg/dL', Decimal('10.0')),
    ('bilirubin_total_mg_dl', 17.1, 'umol/L', Decimal('1.0')),
    ('bilirubin_total_mg_dl', 1.2, 'mg/dL', Decimal('1.2')),
])
def test_the_source_units_normalise_to_the_canonical_column(field, value, unit, expected):
    recognised, canonical = canonical_from_pair(field, value, unit)

    assert recognised
    assert canonical == expected


@pytest.mark.parametrize('unit', ['umol/L', 'µmol/L', 'μmol/L', 'UMOL/L', ' umol/l '])
def test_micro_spellings_are_one_unit(unit):
    assert normalize_unit('bilirubin_total_mg_dl', unit) == 'UMOL/L'
    assert canonical_from_pair('bilirubin_total_mg_dl', 17.1, unit) == (True, Decimal('1.0'))


@pytest.mark.parametrize('field,unit', [
    ('albumin_g_dl', 'mg/dL'),
    ('albumin_g_dl', 'mg/L'),
    ('serum_calcium_mg_dl', 'mmol/dL'),
    ('serum_calcium_mg_dl', 'mEq/L'),
    ('bilirubin_total_mg_dl', 'mg/L'),
])
def test_an_unknown_unit_is_refused_not_guessed(field, unit):
    assert canonical_from_pair(field, 10, unit) == (False, None)


def test_a_missing_unit_is_the_established_canonical_scale():
    # The display-name mappings name mass/volume; an unqualified import has
    # always carried the canonical scale, so it is the identity, not a guess.
    assert canonical_from_pair('albumin_g_dl', 4.2, None) == (True, Decimal('4.2'))
    assert canonical_from_pair('albumin_g_dl', 4.2, '') == (True, Decimal('4.2'))


def test_a_clear_is_recognised_whatever_the_unit_says():
    assert canonical_from_pair('albumin_g_dl', None, 'mg/L') == (True, None)


@pytest.mark.parametrize('field,value,unit', [
    ('albumin_g_dl', 'bad', 'g/L'),
    ('albumin_g_dl', True, 'g/L'),
    ('serum_calcium_mg_dl', float('nan'), 'mmol/L'),
    ('bilirubin_total_mg_dl', float('inf'), 'umol/L'),
])
def test_a_non_numeric_or_non_finite_value_is_not_a_fact(field, value, unit):
    assert canonical_from_pair(field, value, unit) == (False, None)


def test_the_canonical_column_range():
    # All three canonical columns are Decimal(5, 1) -> at most 999.9.
    assert canonical_in_range('albumin_g_dl', Decimal('999.9'))
    assert not canonical_in_range('albumin_g_dl', Decimal('1000.0'))
    assert canonical_in_range('albumin_g_dl', None)


@pytest.mark.parametrize('field,code,value,unit,expected', [
    ('albumin_g_dl', '1751-7', 40, 'g/L', Decimal('4.0')),
    ('serum_calcium_mg_dl', '17861-6', 2.50, 'mmol/L', Decimal('10.0')),
    ('serum_calcium_mg_dl', '49765-1', 10.0, 'mg/dL', Decimal('10.0')),
    ('bilirubin_total_mg_dl', '1975-2', 17.1, 'umol/L', Decimal('1.0')),
])
def test_a_loinc_import_is_projected_in_the_canonical_unit(
    field, code, value, unit, expected,
):
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=code),
        measurement_source_value=code,
        value_as_number=value,
        unit_source_value=unit,
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) == expected


@pytest.mark.parametrize('source_value,field,value,unit,expected', [
    ('Albumin', 'albumin_g_dl', 40, 'g/L', Decimal('4.0')),
    ('Serum Calcium', 'serum_calcium_mg_dl', 2.50, 'mmol/L', Decimal('10.0')),
    (
        'Bilirubin.total [Mass/volume] in Serum or Plasma',
        'bilirubin_total_mg_dl', 17.1, 'umol/L', Decimal('1.0'),
    ),
])
def test_a_display_name_import_is_projected_in_the_canonical_unit(
    source_value, field, value, unit, expected,
):
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept_id=0,
        measurement_source_value=source_value,
        value_as_number=value,
        unit_source_value=unit,
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) == expected


@pytest.mark.parametrize('field,code,older,unit', [
    ('albumin_g_dl', '1751-7', 40, 'g/L'),
    ('serum_calcium_mg_dl', '17861-6', 2.50, 'mmol/L'),
    ('bilirubin_total_mg_dl', '1975-2', 17.1, 'umol/L'),
])
def test_the_newest_unknown_unit_does_not_fall_back_to_an_older_result(
    field, code, older, unit,
):
    person = PersonFactory()
    concept = ConceptFactory(concept_code=code)
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2024, 1, 1), value_as_number=older, unit_source_value=unit,
    )
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2024, 1, 2), value_as_number=100, unit_source_value='unknown',
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) is None


@pytest.mark.parametrize('field,code,source_value,older,unit', [
    ('albumin_g_dl', '1751-7', 'Albumin', 40, 'g/L'),
    ('serum_calcium_mg_dl', '17861-6', 'Serum Calcium', 2.50, 'mmol/L'),
    (
        'bilirubin_total_mg_dl', '1975-2',
        'Bilirubin.total [Mass/volume] in Serum or Plasma', 17.1, 'umol/L',
    ),
])
def test_a_newer_unconvertible_display_row_supersedes_the_coded_result(
    field, code, source_value, older, unit,
):
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code=code),
        measurement_source_value=code, measurement_date=date(2024, 1, 1),
        value_as_number=older, unit_source_value=unit,
    )
    MeasurementFactory(
        person=person, measurement_concept_id=0, measurement_source_value=source_value,
        measurement_date=date(2024, 1, 2), value_as_number=100,
        unit_source_value='unknown',
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) is None
    # The import is read-only input; conversion never rewrites the OMOP row.
    row = Measurement.objects.get(
        person=person, measurement_date=date(2024, 1, 2),
    )
    assert row.value_as_number == 100
    assert row.unit_source_value == 'unknown'


def test_the_registry_names_all_three_pairs():
    assert set(PAIRS) == {'albumin_g_dl', 'serum_calcium_mg_dl', 'bilirubin_total_mg_dl'}


@pytest.mark.parametrize('code,unit,value,field,expected', [
    ('1751-7', 'g/L', 40, 'albumin_g_dl', Decimal('4.0')),
    ('17861-6', 'mmol/L', 2.50, 'serum_calcium_mg_dl', Decimal('10.0')),
    ('1975-2', 'umol/L', 17.1, 'bilirubin_total_mg_dl', Decimal('1.0')),
])
def test_a_ucum_unit_concept_is_used_only_when_source_unit_absent(
    code, unit, value, field, expected,
):
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=code),
        measurement_source_value=code,
        value_as_number=value,
        unit_concept=ConceptFactory(
            vocabulary=VocabularyFactory(vocabulary_id='UCUM'), concept_code=unit),
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) == expected


def test_a_legacy_pair_stores_the_reviewed_unit_code_when_only_a_concept_has_it():
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='albumin'),
        value_as_number=40,
        unit_concept=ConceptFactory(
            vocabulary=VocabularyFactory(vocabulary_id='UCUM'), concept_code='g/L'),
    )

    record = refresh_patient_record(person)

    # The legacy fact keeps the reported number and the reviewed unit code;
    # the canonical is the normalised read-model beside it.
    assert record.albumin_level == Decimal('40')
    assert record.albumin_level_units == 'G/L'
    assert record.albumin_g_dl == Decimal('4.0')


@pytest.mark.parametrize('field,code,source_value,value,unit,expected', [
    ('albumin_g_dl', '1751-7', 'Albumin', 40, 'g/L', Decimal('4.0')),
    ('serum_calcium_mg_dl', '17861-6', 'Serum Calcium', 2.50, 'mmol/L', Decimal('10.0')),
    (
        'bilirubin_total_mg_dl', '1975-2',
        'Bilirubin.total [Mass/volume] in Serum or Plasma', 17.1, 'umol/L',
        Decimal('1.0'),
    ),
])
def test_a_newer_supported_display_row_beats_an_older_unsupported_coded_row(
    field, code, source_value, value, unit, expected,
):
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code=code),
        measurement_source_value=code, measurement_date=date(2024, 1, 1),
        value_as_number=100, unit_source_value='unknown',
    )
    MeasurementFactory(
        person=person, measurement_concept_id=0,
        measurement_source_value=source_value, measurement_date=date(2024, 1, 2),
        value_as_number=value, unit_source_value=unit,
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) == expected


@pytest.mark.parametrize('field,pair,unit,code,source_unit', [
    ('albumin_g_dl', 'albumin_level', 'albumin_level_units', '1751-7', 'g/L'),
    ('serum_calcium_mg_dl', 'serum_calcium_level', 'serum_calcium_level_units',
     '17861-6', 'mmol/L'),
    ('bilirubin_total_mg_dl', 'serum_bilirubin_level_total',
     'serum_bilirubin_level_total_units', '1975-2', 'umol/L'),
])
def test_a_newer_unsupported_coded_row_clears_an_older_legacy_pair(
    field, pair, unit, code, source_unit,
):
    names = {
        'albumin_level': 'albumin',
        'serum_calcium_level': 'calcium',
        'serum_bilirubin_level_total': 'bilirubin total',
    }
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_name=names[pair]),
        measurement_date=date(2024, 1, 1), value_as_number=40,
        unit_source_value=source_unit,
    )
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code=code),
        measurement_source_value=code, measurement_date=date(2024, 1, 2),
        value_as_number=40, unit_source_value='unknown',
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) is None
    assert getattr(record, pair) is None
    assert getattr(record, unit) is None


@pytest.mark.parametrize('field,pair,unit,code,source_unit', [
    ('albumin_g_dl', 'albumin_level', 'albumin_level_units', '1751-7', 'g/L'),
    ('serum_calcium_mg_dl', 'serum_calcium_level', 'serum_calcium_level_units',
     '17861-6', 'mmol/L'),
    ('bilirubin_total_mg_dl', 'serum_bilirubin_level_total',
     'serum_bilirubin_level_total_units', '1975-2', 'umol/L'),
])
def test_a_newer_unsupported_legacy_row_clears_an_older_coded_result(
    field, pair, unit, code, source_unit,
):
    names = {
        'albumin_level': 'albumin',
        'serum_calcium_level': 'calcium',
        'serum_bilirubin_level_total': 'bilirubin total',
    }
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code=code),
        measurement_source_value=code, measurement_date=date(2024, 1, 1),
        value_as_number=40, unit_source_value=source_unit,
    )
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_name=names[pair]),
        measurement_date=date(2024, 1, 2), value_as_number=40,
        unit_source_value='unknown',
    )

    record = refresh_patient_record(person)

    assert getattr(record, field) is None
    assert getattr(record, pair) is None
    assert getattr(record, unit) is None


def test_a_same_day_legacy_correction_wins_by_measurement_id():
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_id=990001,
        measurement_concept=ConceptFactory(concept_code='1751-7'),
        measurement_source_value='1751-7', measurement_date=date(2024, 1, 1),
        value_as_number=40, unit_source_value='g/L',
    )
    MeasurementFactory(
        person=person, measurement_id=990002,
        measurement_concept=ConceptFactory(concept_name='albumin'),
        measurement_date=date(2024, 1, 1), value_as_number=50,
        unit_source_value='g/L',
    )

    record = refresh_patient_record(person)

    assert record.albumin_g_dl == Decimal('5.0')
    assert record.albumin_level == Decimal('50')
    assert record.albumin_level_units == 'G/L'


@pytest.mark.parametrize('value', [40, 4.2])
def test_a_non_ucum_unit_concept_is_not_read_as_the_canonical_scale(value):
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='1751-7'),
        measurement_source_value='1751-7',
        value_as_number=value,
        unit_concept=ConceptFactory(
            vocabulary=VocabularyFactory(vocabulary_id='LOCAL'),
            concept_code='mg/L'),
    )

    record = refresh_patient_record(person)

    assert record.albumin_g_dl is None




def test_an_approved_mapping_cannot_restore_a_rejected_chemistry_unit():
    person = PersonFactory()
    concept = ConceptFactory(concept_code='1751-7')
    FieldConceptMapping.objects.create(
        field_name='albumin_g_dl', concept=concept, vocabulary_id='LOINC',
        concept_code='1751-7', status='approved', omop_table='measurement',
        source_value='1751-7', value_kind='number',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='1751-7', value_as_number=40,
        unit_source_value='mg/L',
    )

    record = refresh_patient_record(person)

    # The mapped extractor must not write the unconverted number after the
    # chemistry pass rejected it.
    assert record.albumin_g_dl is None
    assert record.albumin_level is None
    assert record.albumin_level_units is None


def test_an_approved_mapping_cannot_restore_an_older_result_after_a_rejection():
    person = PersonFactory()
    concept = ConceptFactory(concept_code='1751-7')
    FieldConceptMapping.objects.create(
        field_name='albumin_g_dl', concept=concept, vocabulary_id='LOINC',
        concept_code='1751-7', status='approved', omop_table='measurement',
        source_value='1751-7', value_kind='number',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='1751-7', measurement_date=date(2024, 1, 1),
        value_as_number=40, unit_source_value='g/L',
    )
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_name='albumin'),
        measurement_date=date(2024, 1, 2), value_as_number=40,
        unit_source_value='unknown',
    )

    record = refresh_patient_record(person)

    assert record.albumin_g_dl is None
    assert record.albumin_level is None
