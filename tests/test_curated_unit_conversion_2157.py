"""Write-time unit conversion: only to a curated unit, only when it stores exactly (#2157).

A converted value, or either range bound, that Decimal(15, 5) cannot hold
exactly leaves the row as sent: its numbers and its unit_concept, so the
value is never relabelled with a unit it is not in.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.mapping.code_resolution import repoint_clinical_rows
from omop_core.models import Measurement, SourceCodeAxisVariant
from omop_core.services.canonical_units import normalize_measurement_units
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory
from tests.test_canonical_units import TestVariantAwareNormalization, _sccm, _ucum_concept

pytestmark = pytest.mark.django_db


def _row(source_value, value, unit, low=None, high=None, concept=None):
    return MeasurementFactory(
        person=PersonFactory(), measurement_concept=concept or ConceptFactory(),
        measurement_source_value=source_value, value_as_number=Decimal(value),
        unit_source_value=unit, range_low=low and Decimal(low), range_high=high and Decimal(high),
    )


def _normalize(row, mapping):
    return normalize_measurement_units(Measurement.objects.filter(pk=row.pk), mapping)


def _unchanged(row):
    before = (row.value_as_number, row.range_low, row.range_high, row.unit_concept_id)
    row.refresh_from_db()
    return (row.value_as_number, row.range_low, row.range_high, row.unit_concept_id) == before


# --- the exact-storage check --------------------------------------------------

def test_a_count_that_would_overflow_is_left_as_sent():
    """The issue's platelets: 246 10*3/uL is 2.46e11 /L."""
    row = _row('PLT', '246', '10*3/uL')
    mapping = _sccm('PLT', row.measurement_concept, destination_unit_concept=_ucum_concept('/L'))

    assert _normalize(row, mapping) == 0
    assert _unchanged(row)


def test_a_value_that_would_round_to_zero_is_left_as_sent():
    """2 ng/mL is 0.000002 g/L, which Decimal(15, 5) stores as 0."""
    row = _row('VITD', '2', 'ng/mL')
    mapping = _sccm('VITD', row.measurement_concept, destination_unit_concept=_ucum_concept('g/L'))

    assert _normalize(row, mapping) == 0
    assert _unchanged(row)


def test_a_value_that_would_lose_digits_is_left_as_sent():
    row = _row('VITD', '123.45678', 'ng/mL')
    mapping = _sccm('VITD', row.measurement_concept, destination_unit_concept=_ucum_concept('ug/mL'))

    assert _normalize(row, mapping) == 0
    assert _unchanged(row)


@pytest.mark.parametrize('low,high', [('0.25', '900'), ('100', '0.25')])
def test_a_range_bound_that_would_not_fit_leaves_the_whole_row_as_sent(low, high):
    """500 ng/mL is 0.05 mg/dL, but 0.25 ng/mL would be 0.000025 mg/dL."""
    row = _row('FERR', '500', 'ng/mL', low=low, high=high)
    mapping = _sccm('FERR', row.measurement_concept, destination_unit_concept=_ucum_concept('mg/dL'))

    assert _normalize(row, mapping) == 0
    assert _unchanged(row)


def test_a_conversion_that_fits_is_written_with_its_unit():
    row = _row('PLT', '246', '10*3/uL', low='150', high='400')
    giga = _ucum_concept('10*9/L')
    mapping = _sccm('PLT', row.measurement_concept, destination_unit_concept=giga)

    assert _normalize(row, mapping) == 1
    row.refresh_from_db()
    assert (row.value_as_number, row.range_low, row.range_high) == (Decimal('246'), Decimal('150'), Decimal('400'))
    assert row.unit_concept_id == giga.concept_id
    assert row.unit_source_value == '10*3/uL'


def test_one_row_that_does_not_fit_does_not_stop_the_rest():
    concept = ConceptFactory()
    small, fits = _row('VITD', '2', 'ng/mL', concept=concept), _row('VITD', '30', 'ng/mL', concept=concept)
    mapping = _sccm('VITD', concept, destination_unit_concept=_ucum_concept('g/L'))

    assert normalize_measurement_units(Measurement.objects.filter(pk__in=[small.pk, fits.pk]), mapping) == 1
    assert _unchanged(small)
    fits.refresh_from_db()
    assert fits.value_as_number == Decimal('0.00003')


def test_more_rows_than_one_batch_are_all_converted():
    """Converted rows leave the query; paging must not skip the ones after them."""
    concept = ConceptFactory()
    rows = [_row('GLU', str(100 + i), 'umol/L', concept=concept) for i in range(1003)]
    mapping = _sccm('GLU', concept, destination_unit_concept=_ucum_concept('mmol/L'))

    assert normalize_measurement_units(Measurement.objects.filter(pk__in=[r.pk for r in rows]), mapping) == 1003


# --- repoint --------------------------------------------------------------------

@pytest.mark.skip(reason="cb-v2 excludes write-time normalization (docs/canonical_units_architecture.md)")
def test_repoint_moves_a_row_whose_conversion_would_overflow():
    """The concept change stands; the value and unit stay as sent."""
    old = ConceptFactory()
    row = _row('PLT', '246', '10*3/uL', concept=old)
    new = ConceptFactory()
    mapping = _sccm('PLT', new, destination_unit_concept=_ucum_concept('/L'))

    result = repoint_clinical_rows(mapping=mapping, old_concept_id=old.concept_id, new_concept_id=new.concept_id)

    assert result['rows_updated'] == 1
    assert result['rows_normalized'] == 0
    unit_before = row.unit_concept_id
    row.refresh_from_db()
    assert row.measurement_concept_id == new.concept_id
    assert row.value_as_number == Decimal('246')
    assert row.unit_concept_id == unit_before


def test_repoint_survives_a_failed_conversion(monkeypatch):
    """A database error in the conversion is rolled back to its savepoint, not the move."""
    from django.db import connection
    from omop_core.services import canonical_units

    def fail(*args, **kwargs):
        with connection.cursor() as cur:
            cur.execute('SELECT 250000000000::numeric(15, 5)')

    monkeypatch.setattr(canonical_units, 'normalize_measurement_units', fail)
    old, new = ConceptFactory(), ConceptFactory()
    row = _row('GLU', '500', 'umol/L', concept=old)
    mapping = _sccm('GLU', new, destination_unit_concept=_ucum_concept('mmol/L'))

    result = repoint_clinical_rows(mapping=mapping, old_concept_id=old.concept_id, new_concept_id=new.concept_id)

    assert result['rows_updated'] == 1
    row.refresh_from_db()
    assert row.measurement_concept_id == new.concept_id


# --- bulk create ------------------------------------------------------------------

@pytest.fixture
def post(db):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='units-2157@example.org', password='test', is_staff=True))
    person = PersonFactory()
    PatientRecordFactory(person=person)
    type_concept = ConceptFactory()

    def post(concept, source_value, value, unit, **extra):
        body = [{
            'person': person.person_id, 'measurement_concept': concept.concept_id,
            'measurement_date': '2023-04-14', 'measurement_type_concept': type_concept.concept_id,
            'measurement_source_value': source_value, 'value_as_number': value,
            'unit_source_value': unit, **extra,
        }]
        resp = client.post('/api/v1/measurements/?skip_refresh=true&upsert=false', body, format='json')
        assert resp.status_code == 201, resp.content
        return Measurement.objects.get(pk=resp.json()['ids'][0])
    return post


def _loinc(code, name, prop):
    return TestVariantAwareNormalization()._loinc_concept(code, name, prop)


def test_bulk_platelets_with_no_curated_unit_are_stored_as_sent(post):
    """The issue's Example 1: was a 400 for the whole chunk."""
    concept = _loinc('777-3', 'Platelets [#/volume] in Blood', 'Number Concentration')
    _sccm('Platelet Count', concept)

    row = post(concept, 'Platelet Count', '246.0', '10*3/uL')

    assert row.value_as_number == Decimal('246')


def test_bulk_row_with_no_mapping_is_not_converted_to_a_base_unit(post):
    concept = _loinc('1989-3', 'Vitamin D [Mass/volume]', 'Mass Concentration')

    row = post(concept, 'Vitamin D', '2', 'ng/mL')

    assert row.value_as_number == Decimal('2')


def test_bulk_row_that_would_overflow_its_curated_unit_is_stored_as_sent(post):
    concept = ConceptFactory()
    _sccm('PLT', concept, destination_unit_concept=_ucum_concept('/L'))
    sent_unit = _ucum_concept('10*3/uL')

    row = post(concept, 'PLT', '246', '10*3/uL', unit_concept=sent_unit.concept_id)

    assert row.value_as_number == Decimal('246')
    assert row.unit_concept_id == sent_unit.concept_id


@pytest.mark.skip(reason="cb-v2 excludes write-time normalization (docs/canonical_units_architecture.md)")
def test_bulk_row_is_converted_to_its_curated_unit(post):
    concept = ConceptFactory()
    mg_dl = _ucum_concept('mg/dL')
    _sccm('ALB', concept, destination_unit_concept=mg_dl)

    row = post(concept, 'ALB', '4.2', 'g/dL', range_low='3.5', range_high='5')

    assert (row.value_as_number, row.range_low, row.range_high) == (Decimal('4200'), Decimal('3500'), Decimal('5000'))
    assert row.unit_concept_id == mg_dl.concept_id


@pytest.mark.skip(reason="cb-v2 excludes write-time normalization (docs/canonical_units_architecture.md)")
def test_bulk_row_in_a_variant_unit_is_not_converted(post):
    """g/L converts to mg/dL, but the variant owns g/L and has no curated unit."""
    concept = ConceptFactory()
    mapping = _sccm('GLU', concept, destination_unit_concept=_ucum_concept('mg/dL'))
    mapping.axis_variant_count = 2
    mapping.save(update_fields=['axis_variant_count'])
    SourceCodeAxisVariant.objects.create(
        mapping=mapping, axis_key='property=MCnc|scale_type=Qn',
        axis_values={'property': 'MCnc', 'scale_type': 'Qn'}, units=['g/L'],
        status='approved', target_concept=ConceptFactory(),
    )

    variant_row = post(concept, 'GLU', '1', 'g/L')
    parent_row = post(concept, 'GLU', '1', 'g/dL')

    assert variant_row.value_as_number == Decimal('1')
    assert parent_row.value_as_number == Decimal('1000')
