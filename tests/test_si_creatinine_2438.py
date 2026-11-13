"""Serum creatinine reported in umol/L derives in mg/dL on cb-v2 (#2438).

The snapshot holds each value in its ``unit_source_value`` unit (#2249), and
2160-0/38483-4 went through the generic LOINC cast: 80 umol/L, a normal
0.90 mg/dL, became ``serum_creatinine_mg_dl = 80``. ``derive_meets_crab``, the
renal adequacy rule and the matcher read that column.

cb-v2 has a creatinine source pair (#2318), so creatinine joins hemoglobin and
the chemistry pairs in the chronological pass: the newest result across coded,
display-name and legacy rows owns the field; an unreadable newest unit leaves
it unknown. The pair keeps the number and unit the lab reported, because eGFR
reads the pair (``creatinine_units``). Ported from dev's PR #2443.
"""
from datetime import date
from decimal import Decimal

import pytest

from omop_core.models import Concept, Measurement, Person
from omop_core.services.patient_record_service import (
    _get_laboratory_data,
    derive_meets_crab,
    refresh_patient_record,
)
from tests.factories import (
    ConceptFactory,
    MeasurementFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _loinc(code: str) -> Concept:
    return ConceptFactory(
        concept_code=code, concept_name='Creatinine',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )


def _ucum(code: str) -> Concept:
    return ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code=code)


def _no_match() -> Concept:
    return ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )


def _creatinine(
    person: Person,
    value,
    unit: str | None = None,
    *,
    code: str = '2160-0',
    day: str = '2024-01-15',
    unit_concept: Concept | None = None,
) -> Measurement:
    return MeasurementFactory(
        person=person,
        measurement_concept=_loinc(code),
        measurement_source_value=f'Lab {code}',
        measurement_date=day,
        value_as_number=value,
        unit_source_value=unit,
        unit_concept=unit_concept,
    )


# ---------------------------------------------------------------------------
# The lab projection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('code', ['2160-0', '38483-4'])
@pytest.mark.parametrize('unit', [
    'umol/L', 'µmol/L', 'μmol/L', 'UMOL/L', 'micromol/L', 'micromoles/liter',
])
def test_si_creatinine_is_projected_in_mg_dl(code, unit):
    person = PersonFactory()
    _creatinine(person, 80, unit, code=code)

    data = _get_laboratory_data(person)

    assert data['serum_creatinine_mg_dl'] == Decimal('0.90')
    assert data['creatinine_mg_dl'] == Decimal('0.90')
    # The pair is what the lab reported; eGFR reads it unrounded.
    assert data['serum_creatinine_level'] == Decimal('80')
    assert data['serum_creatinine_level_units'] == 'UMOL/L'


def test_mg_dl_creatinine_is_unchanged():
    person = PersonFactory()
    _creatinine(person, 1.1, 'mg/dL')

    data = _get_laboratory_data(person)

    assert data['serum_creatinine_mg_dl'] == Decimal('1.10')
    assert data['serum_creatinine_level_units'] == 'MG/DL'


def test_creatinine_without_a_unit_is_read_as_mg_dl():
    person = PersonFactory()
    _creatinine(person, 1.1)

    data = _get_laboratory_data(person)

    assert data['serum_creatinine_mg_dl'] == Decimal('1.10')
    assert data['serum_creatinine_level_units'] == 'MG/DL'


def test_concept_only_umol_l_creatinine_converts():
    person = PersonFactory()
    _creatinine(person, 80, unit_concept=_ucum('umol/L'))

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == Decimal('0.90')


def test_an_unreadable_text_defers_to_the_ucum_concept():
    person = PersonFactory()
    _creatinine(person, 80, 'UMOL/LTR', unit_concept=_ucum('umol/L'))

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == Decimal('0.90')


@pytest.mark.parametrize('unit', ['mg', 'mg/L', 'mmol/L', 'zorkles'])
def test_unreadable_newest_creatinine_is_unknown_and_older_is_not_resurrected(unit):
    person = PersonFactory()
    _creatinine(person, 1.0, 'mg/dL', day='2024-01-01')
    _creatinine(person, 3.4, unit, day='2024-06-01')

    data = _get_laboratory_data(person)

    assert data.get('serum_creatinine_mg_dl') is None
    assert data.get('creatinine_mg_dl') is None
    assert data.get('serum_creatinine_level') is None


def test_a_creatinine_beyond_its_column_is_unknown():
    """5000 mg/dL does not fit ``serum_creatinine_mg_dl`` (999.99): no value,
    rather than a number every "> 2" threshold accepts."""
    person = PersonFactory()
    _creatinine(person, 5000, 'mg/dL')

    assert _get_laboratory_data(person).get('serum_creatinine_mg_dl') is None


def test_unreadable_loinc_creatinine_is_not_filled_from_the_display_name_path():
    """The display-name row is older; it must not resurrect a field the newer
    LOINC row left unknown."""
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=_no_match(),
        measurement_source_value='Creatinine', measurement_date='2024-01-01',
        value_as_number=1.0, unit_source_value='mg/dL',
    )
    _creatinine(person, 3.4, 'mg', day='2024-06-01')

    assert _get_laboratory_data(person).get('serum_creatinine_mg_dl') is None


def test_display_name_si_creatinine_is_converted():
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=_no_match(),
        measurement_source_value='Creatinine',
        value_as_number=80, unit_source_value='umol/L',
    )

    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == Decimal('0.90')


def test_the_newest_display_name_spelling_owns_the_field():
    """An older row under another spelling must not mask a newer unreadable one."""
    person = PersonFactory()
    no_match = _no_match()
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


def test_a_newer_coded_result_beats_an_older_legacy_concept_name_row():
    """The legacy concept-name row ('Creatinine in serum') used to write the
    pair on its own, beside a newer coded result's canonical."""
    person = PersonFactory()
    legacy = ConceptFactory(concept_code='LEGACY-CREAT', concept_name='Creatinine in serum')
    MeasurementFactory(
        person=person, measurement_concept=legacy, measurement_source_value='legacy',
        measurement_date='2024-01-01', value_as_number=3.0, unit_source_value='mg/dL',
    )
    _creatinine(person, 80, 'umol/L', day='2024-06-01')

    data = _get_laboratory_data(person)

    assert data['serum_creatinine_mg_dl'] == Decimal('0.90')
    assert data['serum_creatinine_level'] == Decimal('80')
    assert data['serum_creatinine_level_units'] == 'UMOL/L'


def test_the_extractor_reports_a_legacy_row_in_an_unsupported_unit_as_sent():
    """#475's rule for historic concept-name facts, as hemoglobin has it: the
    extractor reports the number and unit as sent, the canonical unknown. The
    refresh does not keep that pair yet (#2519), so this is extractor-only."""
    person = PersonFactory()
    legacy = ConceptFactory(concept_code='LEGACY-CREAT2', concept_name='Creatinine')
    MeasurementFactory(
        person=person, measurement_concept=legacy, measurement_source_value='legacy',
        measurement_date='2024-01-01', value_as_number=12, unit_source_value='mg/L',
    )

    data = _get_laboratory_data(person)

    assert data['serum_creatinine_mg_dl'] is None
    assert data['serum_creatinine_level'] == Decimal('12')
    assert data['serum_creatinine_level_units'] == 'mg/L'


# ---------------------------------------------------------------------------
# What reads the canonical value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, stored, adequate, crab', [
    (80, Decimal('0.90'), True, None),
    (221, Decimal('2.50'), False, True),
])
def test_refresh_stores_mg_dl_and_its_readers_follow(value, stored, adequate, crab):
    person = PersonFactory()
    _creatinine(person, value, 'umol/L')

    record = refresh_patient_record(person)
    record.refresh_from_db()

    assert record.serum_creatinine_mg_dl == stored
    assert record.creatinine_mg_dl == stored
    assert record.serum_creatinine_level == Decimal(str(value))
    assert record.serum_creatinine_level_units == 'UMOL/L'
    assert record.renal_adequacy_status is adequate
    # The edit path re-derives CRAB from this column: a normal SI result is
    # no renal evidence, an abnormal one is.
    assert derive_meets_crab(record) is crab


@pytest.mark.parametrize('newest_value, newest_unit', [
    (3.4, 'mg'), (90, 'mg/L'), (90, 'zorkles'),
])
def test_a_blocked_newest_result_stays_unknown_through_refresh(newest_value, newest_unit):
    """The extractor blocks an unreadable newest unit; the approved mapping
    (concept + source_value '2160-0', the shape omop_write_service and the FHIR
    upload write) must not then refill the field with that row's raw number."""
    from omop_core.models import FieldConceptMapping, PatientRecord
    person = PersonFactory()
    PatientRecord.objects.get_or_create(person=person)
    concept = _loinc('2160-0')
    FieldConceptMapping.objects.create(
        field_name='serum_creatinine_mg_dl', concept=concept, vocabulary_id='LOINC',
        concept_code='2160-0', status='approved', omop_table='measurement',
        source_value='2160-0', value_kind='number',
    )
    MeasurementFactory(person=person, measurement_concept=concept, measurement_source_value='2160-0',
                       measurement_date=date(2024, 1, 1), value_as_number=1.0, unit_source_value='mg/dL')
    MeasurementFactory(person=person, measurement_concept=concept, measurement_source_value='2160-0',
                       measurement_date=date(2024, 6, 1), value_as_number=newest_value,
                       unit_source_value=newest_unit)

    record = refresh_patient_record(person)

    record.refresh_from_db()
    assert record.serum_creatinine_mg_dl is None
    assert record.creatinine_mg_dl is None
