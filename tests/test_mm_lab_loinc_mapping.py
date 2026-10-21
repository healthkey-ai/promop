"""Myeloma disease burden labs: LOINC spellings, units and SLiM derivation."""

from decimal import Decimal

import pytest

from omop_core.models import Concept, Measurement, Person
from omop_core.services.patient_record_service import (
    _get_laboratory_data,
    _get_mm_specific_data,
    refresh_patient_record,
)
from tests.factories import (
    ConceptFactory,
    MeasurementFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _loinc(code: str, name: str = 'Lab') -> Concept:
    return ConceptFactory(
        concept_code=code, concept_name=name,
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )


def _measure(
    person: Person,
    code: str,
    value: float,
    source_value: str | None = None,
    date: str = '2024-01-15',
    unit: str | None = None,
    at: str | None = None,
) -> Measurement:
    """A Measurement coded the way an EHR export codes it.

    The LOINC lives on the concept and source_value carries the lab's display
    name, which is the shape the old source_value-only filters could not see.
    """
    return MeasurementFactory(
        person=person,
        measurement_concept=_loinc(code),
        measurement_source_value=source_value if source_value is not None else f'Lab {code}',
        measurement_date=date,
        measurement_datetime=at,
        value_as_number=value,
        unit_source_value=unit,
    )


# ---------------------------------------------------------------------------
# Lab projection — real-world codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('code', ['36916-5', '80515-0'])
def test_real_world_kappa_codes_project_to_kappa_flc(code):
    person = PersonFactory()
    _measure(
        person, code, 1.87, source_value='Free Kappa Light Chains', unit='mg/L',
    )

    assert float(_get_laboratory_data(person)['kappa_flc']) == pytest.approx(1.87)


@pytest.mark.parametrize('code', ['33944-0', '80516-8'])
def test_real_world_lambda_codes_project_to_lambda_flc(code):
    person = PersonFactory()
    _measure(
        person, code, 0.92, source_value='Free Lambda Light Chains', unit='mg/L',
    )

    assert float(_get_laboratory_data(person)['lambda_flc']) == pytest.approx(0.92)


def test_seeded_kappa_and_real_lambda_codes_are_not_confused():
    """One character apart and opposite analytes."""
    person = PersonFactory()
    _measure(person, '33944-8', 5.0, unit='mg/L')
    _measure(person, '33944-0', 7.0, unit='mg/L')

    data = _get_laboratory_data(person)

    assert float(data['kappa_flc']) == pytest.approx(5.0)
    assert float(data['lambda_flc']) == pytest.approx(7.0)


@pytest.mark.parametrize('code', ['48378-4', '80517-6', '104546-7'])
def test_free_light_chain_ratio_codes_project(code):
    person = PersonFactory()
    _measure(person, code, 2.034)

    assert float(_get_laboratory_data(person)['free_light_chain_ratio']) == pytest.approx(2.034)


def test_marrow_plasma_cells_use_the_real_marrow_code():
    person = PersonFactory()
    _measure(person, '11118-7', 4.2, source_value='Plasma cells, bone marrow')

    assert float(_get_laboratory_data(person)['clonal_plasma_cells']) == pytest.approx(4.2)


def test_ankle_radiograph_no_longer_projects_into_clonal_plasma_cells():
    """26098-4 is "XR Ankle - left Views", it must not reach a marrow field."""
    person = PersonFactory()
    _measure(person, '26098-4', 2.0, source_value='XR Ankle - left Views')

    assert _get_laboratory_data(person).get('clonal_plasma_cells') is None


def test_second_m_protein_spep_code_projects():
    person = PersonFactory()
    _measure(person, '33358-3', 1.4)

    assert float(_get_laboratory_data(person)['monoclonal_protein_serum']) == pytest.approx(1.4)


def test_fractional_results_survive_the_projection():
    """These were IntegerField, which truncated 1.87 to 1."""
    person = PersonFactory()
    _measure(person, '36916-5', 1.87, unit='mg/L')
    _measure(person, '33944-0', 0.42, unit='mg/L')
    _measure(person, '11118-7', 4.2)

    data = _get_laboratory_data(person)

    assert float(data['kappa_flc']) == pytest.approx(1.87)
    assert float(data['lambda_flc']) == pytest.approx(0.42)
    assert float(data['clonal_plasma_cells']) == pytest.approx(4.2)


def test_flc_is_projected_in_canonical_mg_per_litre():
    """Without conversion the field means two things 10x apart."""
    person = PersonFactory()
    _measure(person, '36916-5', 3.0, unit='mg/dL')

    assert float(_get_laboratory_data(person)['kappa_flc']) == pytest.approx(30.0)


def test_flc_in_canonical_units_is_left_alone():
    person = PersonFactory()
    _measure(person, '36916-5', 30.0, unit='mg/L')

    assert float(_get_laboratory_data(person)['kappa_flc']) == pytest.approx(30.0)


@pytest.mark.parametrize(
    'kappa_mg_dl,lambda_mg_dl,expected_ratio',
    [('10.23', '6.2', '1.65'), ('3.38', '13.0', '0.26')],
)
def test_flc_projection_preserves_exact_boundary_ratios(
    kappa_mg_dl, lambda_mg_dl, expected_ratio,
):
    """mg/dL -> mg/L must not turn an inclusive boundary into a float outlier."""
    person = PersonFactory()
    _measure(person, '36916-5', Decimal(kappa_mg_dl), unit='mg/dL')
    _measure(person, '33944-0', Decimal(lambda_mg_dl), unit='mg/dL')

    data = _get_laboratory_data(person)

    assert data['kappa_flc'] / data['lambda_flc'] == Decimal(expected_ratio)


def test_flc_with_an_unknown_unit_is_not_projected_as_mg_l():
    """An unknown unit cannot stand in for mg/L and satisfy the absolute threshold."""
    person = PersonFactory()
    _measure(person, '36916-5', 42.0, unit=None)

    assert 'kappa_flc' not in _get_laboratory_data(person)


def test_latest_unconvertible_flc_blocks_older_canonical_row():
    """An unknown latest unit must not resurrect an older mg/L result."""
    person = PersonFactory()
    _measure(person, '36916-5', 150.0, date='2023-01-01', unit='mg/L')
    _measure(person, '36916-5', 10.0, date='2024-06-01', unit=None)
    _measure(person, '33944-0', 10.0, date='2024-06-01', unit='mg/L')

    data = _get_laboratory_data(person)

    assert 'kappa_flc' not in data
    assert data['lambda_flc'] == Decimal('10.0')


def test_newer_display_name_flc_beats_older_unconvertible_loinc():
    """Mixed representations must still choose the newest convertible result."""
    person = PersonFactory()
    _measure(person, '36916-5', 150.0, date='2023-01-01', unit='mg/L')
    concept = ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='Kappa free light chains',
        measurement_date='2024-06-01',
        value_as_number=Decimal('10.0'), unit_source_value='mg/dL',
    )
    _measure(person, '33944-0', 10.0, date='2024-06-01', unit='mg/L')

    data = _get_laboratory_data(person)

    assert data['kappa_flc'] == Decimal('100.0')


def test_flc_pair_requires_the_same_lab_event():
    """Kappa from one draw and lambda from another cannot form a ratio."""
    person = PersonFactory()
    _measure(person, '36916-5', 150.0, date='2023-01-01', unit='mg/L')
    _measure(person, '33944-0', 75.0, date='2024-06-01', unit='mg/L')

    data = _get_laboratory_data(person)

    assert data['_flc_pair'] is None
    assert data['kappa_flc'] == Decimal('150.0')
    assert data['lambda_flc'] == Decimal('75.0')


def test_flc_pair_uses_the_same_lab_event():
    person = PersonFactory()
    _measure(person, '36916-5', 150.0, date='2024-06-01', unit='mg/L')
    _measure(person, '33944-0', 75.0, date='2024-06-01', unit='mg/L')

    assert _get_laboratory_data(person)['_flc_pair'] == (
        Decimal('150.0'), Decimal('75.0'),
    )


def test_flc_pair_uses_collection_time_not_row_order():
    """The newest draw wins even when its rows were inserted first."""
    person = PersonFactory()
    # Evening draw is inserted first, so the morning draw has the higher IDs.
    _measure(person, '36916-5', 2.0, date='2024-06-01', unit='mg/L',
             at='2024-06-01T20:00:00+00:00')
    _measure(person, '33944-0', 1.0, date='2024-06-01', unit='mg/L',
             at='2024-06-01T20:00:00+00:00')
    _measure(person, '36916-5', 300.0, date='2024-06-01', unit='mg/L',
             at='2024-06-01T08:00:00+00:00')
    _measure(person, '33944-0', 1.0, date='2024-06-01', unit='mg/L',
             at='2024-06-01T08:00:00+00:00')

    assert _get_laboratory_data(person)['_flc_pair'] == (
        Decimal('2.0'), Decimal('1.0'),
    )


def test_refresh_does_not_pair_flc_from_different_events():
    person = PersonFactory()
    _measure(person, '33358-3', 0.1)
    _measure(person, '32730-5', 50.0)
    _measure(person, '36916-5', 150.0, date='2023-01-01', unit='mg/L')
    _measure(person, '33944-0', 75.0, date='2024-06-01', unit='mg/L')

    patient = refresh_patient_record(person)

    assert patient.measurable_disease_imwg is None


def test_refresh_uses_a_same_event_flc_pair():
    person = PersonFactory()
    _measure(person, '33358-3', 0.1)
    _measure(person, '32730-5', 50.0)
    _measure(person, '36916-5', 150.0, date='2024-06-01', unit='mg/L')
    _measure(person, '33944-0', 75.0, date='2024-06-01', unit='mg/L')

    patient = refresh_patient_record(person)

    assert patient.measurable_disease_imwg is True


def test_display_name_flc_values_are_converted_to_mg_l():
    """The legacy/generator display-name path must apply the same unit conversion."""
    person = PersonFactory()
    concept = ConceptFactory(
        concept_code='0', concept_name='No matching concept',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='Kappa free light chains',
        value_as_number=Decimal('10.23'), unit_source_value='mg/dL',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='Lambda free light chains',
        value_as_number=Decimal('6.2'), unit_source_value='mg/dL',
    )

    data = _get_laboratory_data(person)

    assert data['kappa_flc'] == Decimal('102.3')
    assert data['lambda_flc'] == Decimal('62.0')
    assert data['kappa_flc'] / data['lambda_flc'] == Decimal('1.65')


# ---------------------------------------------------------------------------
# SLiM criteria
# ---------------------------------------------------------------------------

def test_slim_sixty_criterion_fires_on_ehr_coded_marrow_result():
    """The old filter matched seeded codes, so EHR rows never satisfied SLiM."""
    person = PersonFactory()
    _measure(person, '11118-7', 65.0, source_value='Plasma cells, bone marrow')

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_slim_sixty_criterion_does_not_fire_below_threshold():
    person = PersonFactory()
    _measure(person, '11118-7', 12.0)

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_light_chain_criterion_needs_both_halves():
    """IMWG 2014: ratio >= 100 and involved chain >= 100 mg/L."""
    person = PersonFactory()
    _measure(person, '48378-4', 140.0)
    _measure(person, '36916-5', 300.0, unit='mg/L')
    _measure(person, '33944-0', 2.0, unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_slim_does_not_fire_on_a_high_ratio_with_a_low_involved_chain():
    """Both chains low, so the ratio reaches 100 without the absolute burden."""
    person = PersonFactory()
    _measure(person, '48378-4', 140.0)
    _measure(person, '36916-5', 30.0, unit='mg/L')
    _measure(person, '33944-0', 0.2, unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_converts_mg_dl_before_applying_the_threshold():
    """30 mg/dL is 300 mg/L, so the same number passes or fails on the unit."""
    person = PersonFactory()
    _measure(person, '36916-5', 30.0, unit='mg/dL')
    _measure(person, '33944-0', 0.2, unit='mg/dL')

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_slim_is_unproven_when_the_involved_chain_unit_is_unknown():
    """An unconvertible result leaves an absolute threshold unproven."""
    person = PersonFactory()
    _measure(person, '36916-5', 300.0, unit=None)
    _measure(person, '33944-0', 2.0, unit=None)

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_is_unproven_on_a_bare_ratio_with_no_chain_results():
    """A ratio carries no concentration, so it cannot establish the threshold."""
    person = PersonFactory()
    _measure(person, '48378-4', 140.0)

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_fires_on_an_inverse_ratio_when_lambda_is_involved():
    person = PersonFactory()
    _measure(person, '48378-4', 0.004)
    _measure(person, '36916-5', 1.0, unit='mg/L')
    _measure(person, '33944-0', 300.0, unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_slim_does_not_fire_on_a_normal_ratio():
    person = PersonFactory()
    _measure(person, '48378-4', 1.2)
    _measure(person, '36916-5', 300.0, unit='mg/L')
    _measure(person, '33944-0', 250.0, unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_falls_back_to_dividing_two_separate_results():
    person = PersonFactory()
    _measure(person, '36916-5', 300.0, unit='mg/L')
    _measure(person, '33944-0', 1.0, unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_slim_uses_decimal_arithmetic_at_the_ratio_boundary():
    """Float division turns an exact 100 ratio into 99.99999999999999."""
    person = PersonFactory()
    _measure(person, '36916-5', Decimal('10.01300'), date='2024-06-01', unit='mg/dL')
    _measure(person, '33944-0', Decimal('0.10013'), date='2024-06-01', unit='mg/dL')

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_slim_preserves_a_reported_ratio_from_the_same_event():
    """A rounded reported ratio is more authoritative than dividing the chains."""
    person = PersonFactory()
    _measure(person, '36916-5', 150.0, date='2024-06-01', unit='mg/L')
    _measure(person, '33944-0', 1.5, date='2024-06-01', unit='mg/L')
    _measure(person, '48378-4', 99.9, date='2024-06-01', unit=None)

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_flc_pair_does_not_cross_unrelated_event_domains():
    """measurement_event_id is qualified by the referenced field concept."""
    person = PersonFactory()
    domain_a = ConceptFactory()
    domain_b = ConceptFactory()
    MeasurementFactory(
        person=person, measurement_concept=_loinc('36916-5'),
        measurement_source_value='Lab 36916-5',
        measurement_date='2024-06-01',
        value_as_number=Decimal('150.0'), unit_source_value='mg/L',
        measurement_event_id=7, meas_event_field_concept=domain_a,
    )
    MeasurementFactory(
        person=person, measurement_concept=_loinc('33944-0'),
        measurement_source_value='Lab 33944-0',
        measurement_date='2024-06-01',
        value_as_number=Decimal('75.0'), unit_source_value='mg/L',
        measurement_event_id=7, meas_event_field_concept=domain_b,
    )

    assert _get_laboratory_data(person)['_flc_pair'] is None


def test_slim_does_not_combine_flc_from_different_lab_events():
    """A high ratio built from two draws is not a SLiM light-chain criterion."""
    person = PersonFactory()
    _measure(person, '36916-5', 300.0, date='2023-01-01', unit='mg/L')
    _measure(person, '33944-0', 1.0, date='2024-06-01', unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_uses_the_latest_flc_pair_not_an_arbitrary_row():
    person = PersonFactory()
    _measure(person, '36916-5', 300.0, date='2023-01-01', unit='mg/L')
    _measure(person, '33944-0', 1.0, date='2023-01-01', unit='mg/L')
    _measure(person, '36916-5', 2.0, date='2024-06-01', unit='mg/L')
    _measure(person, '33944-0', 1.5, date='2024-06-01', unit='mg/L')

    assert _get_mm_specific_data(person)['meets_slim'] is False


def test_slim_still_reads_generator_display_names():
    """The FHIR bundle generator writes display names, not codes."""
    person = PersonFactory()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='0', concept_name='No matching concept'),
        measurement_source_value='Clonal plasma cells in bone marrow (%)',
        value_as_number=70.0,
    )

    assert _get_mm_specific_data(person)['meets_slim'] is True


def test_mm_measurement_lookups_use_shared_prefetch():
    """After the snapshot refactor (#541), _get_mm_specific_data builds its own
    OmopSnapshot when called without one.  The snapshot fetches all non-erroneous
    measurements for the person in a single query and filters in Python.

    This test verifies:
      1. The function issues exactly one measurement SELECT (the snapshot query).
      2. The query count stays bounded regardless of how many codes are involved.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    person = PersonFactory()
    _measure(person, '11118-7', 65.0)
    for i in range(30):
        _measure(person, '718-7', float(i), source_value='Hemoglobin')

    with CaptureQueriesContext(connection) as ctx:
        _get_mm_specific_data(person)

    measurement_queries = [
        q['sql'] for q in ctx.captured_queries if 'FROM "measurement"' in q['sql']
    ]
    assert measurement_queries, 'expected _get_mm_specific_data to read measurements'

    # The snapshot prefetches all measurements in one query; no per-code queries.
    assert len(measurement_queries) == 1, (
        f'expected exactly 1 measurement query (snapshot), got {len(measurement_queries)}'
    )


# ---------------------------------------------------------------------------
# Derived-field contract
# ---------------------------------------------------------------------------

def test_free_light_chain_ratio_is_registered_as_omop_derived():
    """Otherwise it never clears on delete and stays writable through the API."""
    from omop_core.services.patient_record_service import (
        PATIENT_RECORD_OMOP_MAPPED_FIELDS,
        _OMOP_DERIVED_FIELDS,
    )

    assert 'free_light_chain_ratio' in _OMOP_DERIVED_FIELDS
    assert 'free_light_chain_ratio' in PATIENT_RECORD_OMOP_MAPPED_FIELDS


def test_free_light_chain_ratio_has_provenance():
    from omop_core.services.provenance_registry import get_registry

    entry = get_registry()['free_light_chain_ratio']

    assert entry.omop_table == 'Measurement'
    assert '48378-4' in entry.concept_codes
