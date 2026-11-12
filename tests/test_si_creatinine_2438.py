"""Serum creatinine reported in umol/L derives in mg/dL (#2438).

The snapshot holds each value in its ``unit_source_value`` unit (#2249), so the
lab projection must convert an SI creatinine result before it lands in
``serum_creatinine_mg_dl``: 80 umol/L is a normal 0.90 mg/dL, not 80 mg/dL.
"""

from decimal import Decimal

import pytest

from omop_core.models import Concept, Measurement, Person
from omop_core.services.patient_record_service import (
    _get_laboratory_data,
    refresh_patient_record,
)
from tests.factories import (
    ConceptFactory,
    MeasurementFactory,
    PersonFactory,
    VocabularyFactory,
)


def _loinc(code: str) -> Concept:
    return ConceptFactory(
        concept_code=code, concept_name='Creatinine',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )


def _ucum(code: str) -> Concept:
    return ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code=code)


def _creatinine(
    person: Person,
    value,
    unit: str | None = None,
    *,
    code: str = '2160-0',
    date: str = '2024-01-15',
    unit_concept: Concept | None = None,
) -> Measurement:
    return MeasurementFactory(
        person=person,
        measurement_concept=_loinc(code),
        measurement_source_value=f'Lab {code}',
        measurement_date=date,
        value_as_number=value,
        unit_source_value=unit,
        unit_concept=unit_concept,
    )


# ---------------------------------------------------------------------------
# The conversion and its unit rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, unit, ucum, expected', [
    (80, 'umol/L', None, Decimal('0.90')),
    (80, 'µmol/L', None, Decimal('0.90')),
    (80, 'micromoles/liter', None, Decimal('0.90')),
    (221, 'umol/L', None, Decimal('2.50')),
    (1.1, 'mg/dL', None, Decimal('1.10')),
    (1.1, 'MG/DL', None, Decimal('1.10')),
    (1.1, None, None, Decimal('1.10')),          # no unit at all: mg/dL
    (1.1, '  ', None, Decimal('1.10')),
    (80, None, 'umol/L', Decimal('0.90')),       # concept only
    (80, 'UMOL/LTR', 'umol/L', Decimal('0.90')),  # unreadable text defers to UCUM
    (80, 'umol/L', 'mg/dL', Decimal('0.90')),    # readable text wins over the concept
    (1.1, 'mg', None, None),                     # unreadable, nothing to defer to
    (1.1, 'per day', None, None),
    (11, 'mg/L', None, None),                    # known unit the converter cannot read
    (11, 'mg/L', 'mg/dL', None),                 # ... does not defer to the concept
    (0.08, 'mmol/L', 'umol/L', None),
    (1.1, None, 'mmol/L', None),                 # a concept it cannot read
    (None, 'umol/L', None, None),
])
def test_creatinine_to_mg_dl_reads_the_reported_unit(value, unit, ucum, expected):
    from omop_core.services.clinical_units import creatinine_to_mg_dl
    assert creatinine_to_mg_dl(value, unit, ucum) == expected


# ---------------------------------------------------------------------------
# The lab projection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('code', ['2160-0', '38483-4'])
def test_si_creatinine_is_projected_in_mg_dl(code):
    person = PersonFactory()
    _creatinine(person, 80, 'umol/L', code=code)

    data = _get_laboratory_data(person)

    assert data['serum_creatinine_mg_dl'] == pytest.approx(0.90)
    assert data['creatinine_mg_dl'] == pytest.approx(0.90)


@pytest.mark.django_db
def test_mg_dl_creatinine_is_unchanged():
    person = PersonFactory()
    _creatinine(person, 1.1, 'mg/dL')

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == pytest.approx(1.1)


@pytest.mark.django_db
def test_creatinine_without_a_unit_is_read_as_mg_dl():
    person = PersonFactory()
    _creatinine(person, 1.1)

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == pytest.approx(1.1)


@pytest.mark.django_db
def test_concept_only_umol_l_creatinine_converts():
    person = PersonFactory()
    _creatinine(person, 80, unit_concept=_ucum('umol/L'))

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == pytest.approx(0.90)


@pytest.mark.django_db
def test_a_non_ucum_unit_concept_is_not_a_unit():
    """Only a UCUM concept names a unit, as measurement_count_unit and #2249's
    reading have it: a concept from another vocabulary is ignored, so an empty
    text reads as mg/dL."""
    person = PersonFactory()
    snomed = ConceptFactory(vocabulary__vocabulary_id='SNOMED', concept_code='umol/L')
    _creatinine(person, 1.1, unit_concept=snomed)

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == pytest.approx(1.1)


@pytest.mark.django_db
@pytest.mark.parametrize('unit', ['mg', 'mg/L'])
def test_unreadable_newest_creatinine_is_unknown_and_older_is_not_resurrected(unit):
    person = PersonFactory()
    _creatinine(person, 1.0, 'mg/dL', date='2024-01-01')
    _creatinine(person, 3.4, unit, date='2024-06-01')

    data = _get_laboratory_data(person)

    assert data.get('serum_creatinine_mg_dl') is None
    assert data.get('creatinine_mg_dl') is None


@pytest.mark.django_db
def test_unreadable_loinc_creatinine_is_not_filled_from_the_display_name_path():
    """The display-name fallback must not resurrect a field the LOINC path left unknown."""
    person = PersonFactory()
    no_match = ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    MeasurementFactory(
        person=person, measurement_concept=no_match,
        measurement_source_value='Creatinine', measurement_date='2024-01-01',
        value_as_number=1.0, unit_source_value='mg/dL',
    )
    _creatinine(person, 3.4, 'mg', date='2024-06-01')

    assert _get_laboratory_data(person).get('serum_creatinine_mg_dl') is None


@pytest.mark.django_db
def test_display_name_si_creatinine_is_converted():
    person = PersonFactory()
    no_match = ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    MeasurementFactory(
        person=person, measurement_concept=no_match,
        measurement_source_value='Creatinine',
        value_as_number=80, unit_source_value='umol/L',
    )

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == pytest.approx(0.90)


@pytest.mark.django_db
def test_unreadable_display_name_creatinine_is_unknown():
    person = PersonFactory()
    no_match = ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    MeasurementFactory(
        person=person, measurement_concept=no_match,
        measurement_source_value='Creatinine', measurement_date='2024-01-01',
        value_as_number=1.0, unit_source_value='mg/dL',
    )
    MeasurementFactory(
        person=person, measurement_concept=no_match,
        measurement_source_value='Creatinine', measurement_date='2024-06-01',
        value_as_number=3.4, unit_source_value='mg',
    )

    assert _get_laboratory_data(person).get('serum_creatinine_mg_dl') is None


@pytest.mark.django_db
def test_the_newest_display_name_spelling_owns_the_field():
    """An older row under another spelling must not mask a newer unreadable one."""
    person = PersonFactory()
    no_match = ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    MeasurementFactory(
        person=person, measurement_concept=no_match,
        measurement_source_value='Creatinine [Mass/volume] in Serum or Plasma',
        measurement_date='2024-01-01', value_as_number=1.0,
        unit_source_value='mg/dL',
    )
    MeasurementFactory(
        person=person, measurement_concept=no_match,
        measurement_source_value='Creatinine', measurement_date='2024-06-01',
        value_as_number=3.4, unit_source_value='mg',
    )

    assert _get_laboratory_data(person).get('serum_creatinine_mg_dl') is None


# ---------------------------------------------------------------------------
# Derivation: what reads the canonical value
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('value, stored, adequate', [
    (80, Decimal('0.90'), True),
    (221, Decimal('2.50'), False),
])
def test_renal_adequacy_reads_si_creatinine_in_mg_dl(value, stored, adequate):
    """dev's consumer of the canonical value; `derive_meets_crab` exists only on cb-v2."""
    person = PersonFactory()
    _creatinine(person, value, 'umol/L')

    record = refresh_patient_record(person)
    record.refresh_from_db()

    assert record.serum_creatinine_mg_dl == stored
    assert record.creatinine_mg_dl == stored
    assert record.renal_adequacy_status is adequate


# ---------------------------------------------------------------------------
# Through refresh, with the approved 2160-0 mapping the curation fixture ships
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('newest_value, newest_unit', [
    (3.4, 'mg'), (90, 'mg/L'), (90, 'micromol/L'),
])
def test_a_blocked_newest_result_stays_unknown_through_refresh(newest_value, newest_unit):
    """The extractor blocks an unreadable newest unit; the approved mapping
    (concept + source_value '2160-0', the shape omop_write_service and the FHIR
    upload write) must not then refill the field with that row's raw number."""
    from datetime import date as day
    from omop_core.models import FieldConceptMapping, PatientRecord
    from omop_core.services.patient_record_service import refresh_patient_record
    person = PersonFactory()
    PatientRecord.objects.get_or_create(person=person)
    concept = _loinc('2160-0')
    FieldConceptMapping.objects.create(
        field_name='serum_creatinine_mg_dl', concept=concept, vocabulary_id='LOINC',
        concept_code='2160-0', status='approved', omop_table='measurement',
        source_value='2160-0', value_kind='number',
    )
    MeasurementFactory(person=person, measurement_concept=concept, measurement_source_value='2160-0',
                       measurement_date=day(2024, 1, 1), value_as_number=1.0, unit_source_value='mg/dL')
    MeasurementFactory(person=person, measurement_concept=concept, measurement_source_value='2160-0',
                       measurement_date=day(2024, 6, 1), value_as_number=newest_value,
                       unit_source_value=newest_unit)

    record = refresh_patient_record(person)

    record.refresh_from_db()
    assert record.serum_creatinine_mg_dl is None
    assert record.creatinine_mg_dl is None
