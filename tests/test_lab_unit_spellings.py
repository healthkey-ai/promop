"""Imported labs keep a readable unit when the source spelling is unfamiliar.

#2418 made hemoglobin and the chemistry pairs convert by unit, and read only the
source text when one was present. An import whose text said "gm/dL" lost a
13.2 g/dL hemoglobin entirely, even with UCUM g/dL on unit_concept, which is
where the ETL puts the cleaned unit (#2248). Regression review on #2418.
"""
from datetime import date
from decimal import Decimal

import pytest

from omop_core.services.patient_record_service import _get_laboratory_data
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

DAY = date(2026, 10, 1)


@pytest.fixture
def ucum():
    vocabulary = VocabularyFactory(vocabulary_id='UCUM')
    return lambda code: ConceptFactory(concept_code=code, vocabulary=vocabulary)


def _lab(person, source, value, text, unit_concept=None):
    return MeasurementFactory(person=person, measurement_source_value=source, measurement_date=DAY,
                              value_as_number=value, unit_source_value=text, unit_concept=unit_concept)


@pytest.mark.parametrize('text', ['gm/dL', 'GM/DL', 'g/100mL'])
def test_unambiguous_spellings_of_g_dl_are_read(text):
    person = PersonFactory()
    _lab(person, '718-7', 13.2, text)
    assert _get_laboratory_data(person)['hemoglobin_g_dl'] == Decimal('13.2')


def test_an_unfamiliar_text_defers_to_the_ucum_concept(ucum):
    person = PersonFactory()
    _lab(person, '718-7', 132, 'grams per litre', unit_concept=ucum('g/L'))
    assert _get_laboratory_data(person)['hemoglobin_g_dl'] == Decimal('13.2')


def test_chemistry_defers_to_the_ucum_concept_too(ucum):
    person = PersonFactory()
    _lab(person, '1751-7', 34, 'gram/litre', unit_concept=ucum('g/L'))
    assert _get_laboratory_data(person)['albumin_g_dl'] == Decimal('3.4')


def test_neither_readable_still_blocks(ucum):
    person = PersonFactory()
    _lab(person, '718-7', 13.2, 'mystery', unit_concept=ucum('furlong'))
    assert _get_laboratory_data(person)['hemoglobin_g_dl'] is None


@pytest.mark.parametrize('source, field, stored, sent', [
    ('1975-2', 'bilirubin_total_mg_dl', Decimal('1.2'), '12'),
    ('17861-6', 'serum_calcium_mg_dl', Decimal('9.5'), '95'),
])
def test_a_related_but_unreadable_text_unit_never_borrows_the_concept(ucum, source, field, stored, sent):
    """dev's #2157 stored mg/L results converted to the mg/dL concept and kept
    the sent number; the snapshot restores it to the TEXT's unit (#2249). The
    concept must not then label that number: 12 mg/L bilirubin is 1.2 mg/dL,
    never 12 mg/dL."""
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value=source, measurement_date=DAY,
                       value_as_number=stored, unit_source_value='mg/L',
                       unit_concept=ucum('mg/dL'), value_source_value=sent)
    assert _get_laboratory_data(person).get(field) in (None, stored)


def test_an_mg_per_litre_creatinine_is_not_read_as_mg_per_dl(ucum):
    from omop_core.services.patient_record_service import _get_mm_specific_data
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='2160-0', measurement_date=DAY,
                       value_as_number=Decimal('1.2'), unit_source_value='mg/L',
                       unit_concept=ucum('mg/dL'), value_source_value='12')
    assert _get_mm_specific_data(person).get('meets_crab') is None


@pytest.mark.parametrize('source, field, value, text, concept', [
    ('718-7', 'hemoglobin_g_dl', Decimal('8.1'), 'mmol/L', 'g/dL'),
    ('17861-6', 'serum_calcium_mg_dl', Decimal('95'), 'mg/L', 'mmol/L'),
    ('1975-2', 'bilirubin_total_mg_dl', Decimal('12'), 'mg/L', 'umol/L'),
])
def test_a_known_text_unit_of_another_property_is_never_relabelled(ucum, source, field, value, text, concept):
    """A cross-property text/concept pair is left in the text's unit by the
    snapshot; reading it under the concept gave 8.1 g/dL hemoglobin (true ~13)
    and 380.8 mg/dL calcium (true 9.5). Round-2 review on #2418."""
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value=source, measurement_date=DAY,
                       value_as_number=value, unit_source_value=text, unit_concept=ucum(concept))
    assert _get_laboratory_data(person).get(field) is None
