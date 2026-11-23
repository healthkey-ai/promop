"""
Tests for benchmark_trial_eligibility management command.

Covers:
  - Runs end-to-end against a small fixture cohort.
  - Uses the organization-slug cohort selection path.
  - Writes an output JSON file when requested.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.management.commands.benchmark_trial_eligibility import (
    PYTHON_WHITESPACE, _fetch_omop_trial_row,
)
from omop_core.models import Organization, PatientRecord
from omop_core.services.patient_record_service import (
    _get_biomarker_data, _get_laboratory_data, _get_staging_data,
)
from tests.factories import (
    ConditionOccurrenceFactory,
    ConceptFactory,
    MeasurementFactory,
    ObservationFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)


pytestmark = pytest.mark.django_db


def _seed_trial_eligibility_patient(org):
    person = PersonFactory(year_of_birth=1960)
    patient = PatientRecordFactory(
        person=person,
        organization=org,
        patient_age=65,
        gender='M',
        disease='multiple myeloma',
        stage='Stage II',
        ecog_performance_status=1,
        karnofsky_performance_score=90,
        hemoglobin_g_dl=12.4,
        platelet_count_thousand_per_ul=180.0,
        anc_thousand_per_ul=2.1,
        wbc_count_thousand_per_ul=6.3,
        serum_creatinine_mg_dl=1.0,
        creatinine_clearance_ml_min=75.0,
        serum_calcium_mg_dl=9.2,
        bilirubin_total_mg_dl=0.6,
        ast_u_l=18,
        alt_u_l=21,
        albumin_g_dl=4.0,
        her2_status='Negative',
        estrogen_receptor_status='Positive',
        progesterone_receptor_status='Positive',
    )

    ConditionOccurrenceFactory(
        person=person,
        condition_concept=ConceptFactory(
            concept_name='Multiple myeloma',
            concept_code='MM-1',
        ),
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Cancer stage', concept_code='21908-9'),
        measurement_source_value='21908-9',
        value_as_string='Stage II',
    )
    ObservationFactory(
        person=person,
        observation_concept=ConceptFactory(concept_name='ECOG performance status', concept_code='ECOG-1'),
        value_as_number=1,
    )
    ObservationFactory(
        person=person,
        observation_concept=ConceptFactory(concept_name='Karnofsky performance score', concept_code='KPS-1'),
        value_as_number=90,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Hemoglobin', concept_code='718-7'),
        measurement_source_value='718-7',
        value_as_number=12.4,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Platelet count', concept_code='777-3'),
        measurement_source_value='777-3',
        value_as_number=180.0,
        unit_source_value='10*3/uL',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Absolute neutrophil count', concept_code='751-8'),
        measurement_source_value='751-8',
        value_as_number=2.1,
        unit_source_value='10*3/uL',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='White blood cell count', concept_code='6690-2'),
        measurement_source_value='6690-2',
        value_as_number=6.3,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Serum creatinine', concept_code='2160-0'),
        measurement_source_value='2160-0',
        value_as_number=1.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Creatinine clearance', concept_code='2164-2'),
        measurement_source_value='2164-2',
        value_as_number=75.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Serum calcium', concept_code='17861-6'),
        measurement_source_value='17861-6',
        value_as_number=9.2,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Total bilirubin', concept_code='1975-2'),
        measurement_source_value='1975-2',
        value_as_number=0.6,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='AST', concept_code='1920-8'),
        measurement_source_value='1920-8',
        value_as_number=18,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='ALT', concept_code='1742-6'),
        measurement_source_value='1742-6',
        value_as_number=21,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Albumin', concept_code='1751-7'),
        measurement_source_value='1751-7',
        value_as_number=4.0,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='HER2', concept_code='48676-1'),
        measurement_source_value='48676-1',
        value_as_string='Negative',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Estrogen receptor', concept_code='16112-5'),
        measurement_source_value='16112-5',
        value_as_string='Positive',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name='Progesterone receptor', concept_code='16113-3'),
        measurement_source_value='16113-3',
        value_as_string='Positive',
    )

    return person, patient


class TestBenchmarkTrialEligibility:

    def test_runs_for_org_slug(self, capsys):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        _seed_trial_eligibility_patient(org)

        call_command('benchmark_trial_eligibility', org_slugs='synthea-mm')

        out = capsys.readouterr().out
        assert 'patient_record pull:' in out
        assert 'OMOP pull:' in out
        assert '20' in out

    def test_output_file_written(self, tmp_path):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        _seed_trial_eligibility_patient(org)
        output_path = tmp_path / 'results.json'

        call_command(
            'benchmark_trial_eligibility',
            org_slugs='synthea-mm',
            output=str(output_path),
        )

        assert output_path.exists()

    def test_empty_cohort_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command('benchmark_trial_eligibility', person_ids='999999999')


def _measurement(person, code, value, unit):
    MeasurementFactory(
        person=person,
        measurement_source_value=code,
        value_as_number=value,
        unit_source_value=unit,
    )


def test_si_results_are_read_in_their_canonical_units():
    """The raw row must match what the derivation reads (#2438 class)."""
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, '718-7', 135, 'g/L')          # 13.5 g/dL
    _measurement(person, '2160-0', 221, 'umol/L')      # 2.50 mg/dL
    _measurement(person, '17861-6', 2.9, 'mmol/L')     # 11.6 mg/dL
    _measurement(person, '1975-2', 20, 'umol/L')       # 1.2 mg/dL
    _measurement(person, '1751-7', 40, 'g/L')          # 4.0 g/dL
    _measurement(person, '6690-2', 6300, 'cells/uL')   # 6.3 10*3/uL

    row = _fetch_omop_trial_row(person.person_id)

    assert row['hemoglobin_g_dl'] == Decimal('13.5')
    assert row['serum_creatinine_mg_dl'] == Decimal('2.50')
    assert row['serum_calcium_mg_dl'] == Decimal('11.6')
    assert row['bilirubin_total_mg_dl'] == Decimal('1.2')
    assert row['albumin_g_dl'] == Decimal('4.0')
    assert row['wbc_count_thousand_per_ul'] == Decimal('6.3')


def test_an_unreadable_newest_unit_is_no_evidence():
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, '2160-0', 1.0, 'mg/dL')
    _measurement(person, '2160-0', 221, 'units-unknown')

    assert _fetch_omop_trial_row(person.person_id)['serum_creatinine_mg_dl'] is None


def test_a_unitless_row_reads_in_its_field_default_unit():
    """No unit at all falls to the field default, and that default is the
    units module's constant: pinning it here keeps a wrong default (a display
    label instead of the stored code, or another module's) from silently
    emptying the benchmark column."""
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, '2160-0', 1.0, None)
    _measurement(person, '718-7', 12.4, None)

    row = _fetch_omop_trial_row(person.person_id)

    assert row['serum_creatinine_mg_dl'] == Decimal('1.00')
    assert row['hemoglobin_g_dl'] == Decimal('12.40')


def _ucum(code):
    VocabularyFactory(vocabulary_id='UCUM')
    return ConceptFactory(vocabulary_id='UCUM', concept_code=code)


@pytest.mark.parametrize('code,value,field', [
    ('718-7', '-1', 'hemoglobin_g_dl'),
    ('718-7', '10000', 'hemoglobin_g_dl'),
    ('2160-0', '1000', 'serum_creatinine_mg_dl'),
    ('1751-7', '-1', 'albumin_g_dl'),
])
def test_an_out_of_range_pair_is_no_evidence(code, value, field):
    """The canonical columns have limits; the derivation rejects a value the
    column cannot hold, and the benchmark must not report it either (#2475)."""
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, code, Decimal(value), None)

    assert _fetch_omop_trial_row(person.person_id)[field] is None


def test_a_raw_value_above_the_column_limit_survives_its_conversion():
    """The range gate applies to the canonical, not to the raw row: 1000 umol/L
    is a valid 11.31 mg/dL creatinine even though the raw number exceeds the
    mg/dL column's limit."""
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, '38483-4', 1000, 'umol/L')

    row = _fetch_omop_trial_row(person.person_id)

    assert row['serum_creatinine_mg_dl'] == Decimal('11.31')
    assert _get_laboratory_data(person)['serum_creatinine_mg_dl'] == Decimal('11.31')


def test_the_benchmark_reads_the_five_pairs_exactly_as_the_derivation_does():
    """One stored row per pair, read by both surfaces: the benchmark's own
    conversion must not drift from the derivation's (#2475)."""
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, '718-7', 135, 'g/L')
    _measurement(person, '2160-0', 221, 'umol/L')
    _measurement(person, '17861-6', 2.9, 'mmol/L')
    _measurement(person, '1975-2', 20, 'umol/L')
    _measurement(person, '1751-7', 40, 'g/L')

    row = _fetch_omop_trial_row(person.person_id)
    lab = _get_laboratory_data(person)

    for field in ('hemoglobin_g_dl', 'serum_creatinine_mg_dl',
                  'serum_calcium_mg_dl', 'bilirubin_total_mg_dl',
                  'albumin_g_dl'):
        assert row[field] == lab[field], field


def test_the_benchmark_and_the_derivation_reject_the_same_out_of_range_value():
    person = PersonFactory(year_of_birth=1960)
    _measurement(person, '1751-7', -1, 'g/L')

    assert _fetch_omop_trial_row(person.person_id)['albumin_g_dl'] is None
    assert _get_laboratory_data(person)['albumin_g_dl'] is None


def test_a_loinc_concept_wins_over_a_loinc_shaped_source_value():
    """The derivation names a row by its LOINC-shaped concept code and only
    defers to the source value otherwise; a molar 59260-0 row with a 718-7
    source value is not g/dL hemoglobin (#2475)."""
    person = PersonFactory(year_of_birth=1960)
    molar = ConceptFactory(
        concept_code='59260-0', concept_name='Hemoglobin [Moles/volume]',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'))
    MeasurementFactory(
        person=person, measurement_concept=molar,
        measurement_source_value='718-7', value_as_number=Decimal('8'))

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] is None
    assert _get_laboratory_data(person).get('hemoglobin_g_dl') is None


def test_a_source_only_row_still_names_its_measurement():
    """The positive control for the precedence rule: a non-LOINC concept defers
    to a LOINC-shaped source value, as before."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='718-7',
        value_as_number=Decimal('12.4'))

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] == Decimal('12.4')


def test_a_row_converted_on_write_is_read_from_the_number_as_sent():
    """The stored value is in UCUM's unit; the sent number proves the source."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='718-7',
        value_as_number=13.5, unit_source_value='g/L',
        unit_concept=_ucum('g/dL'), value_source_value='135',
    )

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] == Decimal('13.5')


def test_a_known_but_unsupported_source_unit_is_no_evidence():
    """mmol/L haemoglobin is known and is not g/dL; the UCUM concept must not
    silently replace the number's own unit."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='718-7',
        value_as_number=8.1, unit_source_value='mmol/L',
        unit_concept=_ucum('g/dL'),
    )

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] is None


def test_a_readable_lab_spelling_resolves_without_a_unit_concept():
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='718-7',
        value_as_number=13.5, unit_source_value='Grams/Deciliter',
    )

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] == Decimal('13.5')


def test_a_converted_count_is_read_from_the_number_as_sent():
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='6690-2',
        value_as_number=6.3, unit_source_value='cells/uL',
        unit_concept=_ucum('10*3/uL'), value_source_value='6300',
    )

    assert _fetch_omop_trial_row(person.person_id)['wbc_count_thousand_per_ul'] == Decimal('6.3')


def test_a_non_ucum_unit_concept_still_names_the_unit():
    """A local vocabulary concept is not a reason to fall back to the default."""
    VocabularyFactory(vocabulary_id='HK-Labs')
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='718-7',
        value_as_number=135,
        unit_concept=ConceptFactory(vocabulary_id='HK-Labs', concept_code='g/L'),
    )

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] == Decimal('13.5')


def test_an_empty_unit_concept_code_is_not_a_missing_unit():
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='718-7',
        value_as_number=135,
        unit_concept=_ucum(''),
    )

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] is None


def test_an_unproven_converted_count_is_no_evidence():
    """Without the sent number, a stored count in another unit cannot be read."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_source_value='6690-2',
        value_as_number=6.3, unit_source_value='cells/uL',
        unit_concept=_ucum('10*3/uL'),
    )

    assert _fetch_omop_trial_row(person.person_id)['wbc_count_thousand_per_ul'] is None


def test_a_curated_alias_code_is_read_through_its_own_concept(lab_field_mappings):
    """The benchmark takes each field's codes from the curation tables, so an
    approved alias is read through its concept even when the row's source value
    is blank (#2475 review)."""
    from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping

    person = PersonFactory(year_of_birth=1960)
    mapping = FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl')
    alias = ConceptFactory(
        concept_code='30313-1', concept_name='Hemoglobin [Mass/volume] in Arterial blood')
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code=alias.concept_code,
        target_concept_id=mapping.concept_id, status='approved',
        omop_table='measurement', origin='curator')
    MeasurementFactory(
        person=person, measurement_concept=alias,
        measurement_source_value=None, value_as_number=Decimal('12.2'))

    assert _fetch_omop_trial_row(person.person_id)['hemoglobin_g_dl'] == Decimal('12.2')
    assert _get_laboratory_data(person)['hemoglobin_g_dl'] == Decimal('12.2')


def test_a_newer_approved_count_alias_is_the_one_that_counts(lab_field_mappings):
    """Platelets and ANC select the newest row across every routed code, as the
    derivation's blood-count projection does (#2475)."""
    from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping

    person = PersonFactory(year_of_birth=1960)
    mapping = FieldConceptMapping.objects.get(field_name='anc_thousand_per_ul')
    alias = ConceptFactory(concept_code='26499-4', concept_name='Neutrophils in Blood')
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code=alias.concept_code,
        target_concept_id=mapping.concept_id, status='approved',
        omop_table='measurement', origin='curator')
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='751-8'),
        measurement_source_value='751-8', measurement_date='2024-01-01',
        value_as_number=Decimal('2.1'))
    MeasurementFactory(
        person=person, measurement_concept=alias,
        measurement_source_value='26499-4', measurement_date='2024-02-01',
        value_as_number=Decimal('1500'), unit_source_value='cells/uL')

    row = _fetch_omop_trial_row(person.person_id)

    assert row['anc_thousand_per_ul'] == Decimal('1.5')
    assert _get_laboratory_data(person)['anc_thousand_per_ul'] == Decimal('1.5')


def test_a_number_field_reads_its_own_concept_not_another_code_source():
    """The number subquery names a row like `_measurement_code` (#2708): a
    creatinine concept with an AST source value is not AST."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='2160-0'),
        measurement_source_value='1920-8', value_as_number=Decimal('1.1'))

    assert _fetch_omop_trial_row(person.person_id)['ast_u_l'] is None
    assert _get_laboratory_data(person).get('ast_u_l') is None


def test_the_stage_field_keeps_the_staging_extractors_source_fallback():
    """`_get_staging_data` reads concept or source value, so the benchmark must
    agree for stage; the receptors read the concept code only, as
    `_first_by_code` does."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='21905-5'),
        measurement_source_value='21908-9', value_as_string='Stage II')

    row = _fetch_omop_trial_row(person.person_id)

    assert row['stage'] == 'Stage II'
    assert _get_staging_data(person)['stage'] == 'Stage II'


def test_a_number_field_reads_a_curated_code_and_an_approved_alias(lab_field_mappings):
    """The number builders take the field's curated codes, so a positive result
    and an approved alias both survive (#2708 review)."""
    from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping

    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='1920-8'),
        measurement_source_value='1920-8', measurement_date='2024-01-01',
        value_as_number=Decimal('41'))
    mapping = FieldConceptMapping.objects.get(field_name='ast_u_l')
    alias = ConceptFactory(concept_code='30239-9', concept_name='Aspartate aminotransferase')
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code=alias.concept_code,
        target_concept_id=mapping.concept_id, status='approved',
        omop_table='measurement', origin='curator')
    MeasurementFactory(
        person=person, measurement_concept=alias, measurement_source_value='30239-9',
        measurement_date='2024-02-01', value_as_number=Decimal('55'))

    assert _fetch_omop_trial_row(person.person_id)['ast_u_l'] == 55


def test_stage_reads_a_concept_row_before_a_newer_source_row():
    """`_get_staging_data` visits concept-coded rows first, so the benchmark
    must not let a newer source-only row win (#2708 review)."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='21908-9'),
        measurement_source_value='21908-9', measurement_date='2020-01-01',
        value_as_string='Stage I')
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='21905-5'),
        measurement_source_value='21908-9', measurement_date='2026-01-01',
        value_as_string='Stage IV')

    assert _fetch_omop_trial_row(person.person_id)['stage'] == 'Stage I'
    assert _get_staging_data(person)['stage'] == 'Stage I'


def test_an_entered_in_error_row_is_not_a_benchmark_fact():
    """The derivation skips entered-in-error rows, so every measurement builder
    must too (the pair builder already did)."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='1920-8'),
        measurement_source_value='1920-8', value_as_number=Decimal('88'),
        is_erroneous=True)

    assert _fetch_omop_trial_row(person.person_id)['ast_u_l'] is None
    assert _get_laboratory_data(person).get('ast_u_l') is None


def test_stage_reads_the_r_iss_code_the_derivation_prefers():
    """R-ISS is the MM stage the record derives; only the source value carries
    it, so the benchmark must read that code too."""
    person = PersonFactory(year_of_birth=1960)
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='21905-5'),
        measurement_source_value='21908-9-riss', value_as_string='Stage III')

    assert _fetch_omop_trial_row(person.person_id)['stage'] == 'Stage III'
    assert _get_staging_data(person)['stage'] == 'Stage III'


# --- stage and receptors follow their extractors (#2720 review) --------------
#
# Each case seeds rows for one person and asserts the benchmark and the
# extractor agree on the value given. A row is (table, concept code, source
# value, date, answer columns).

_ISS, _RISS, _T = '21908-9', '21908-9-riss', '21905-5'


def _seed_rows(person, rows):
    for table, code, source, when, values in rows:
        values = dict(values)
        if 'answer_concept' in values:
            values['value_as_concept'] = ConceptFactory(
                concept_code=f'ANS-{when}', concept_name=values.pop('answer_concept'))
        if table == 'm':
            MeasurementFactory(
                person=person, measurement_concept=ConceptFactory(concept_code=code),
                measurement_source_value=source, measurement_date=when, **values)
        else:
            ObservationFactory(
                person=person, observation_concept=ConceptFactory(concept_code=code),
                observation_source_value=source, observation_date=when, **values)


_STAGE_CASES = {
    # The reported regression: an older concept-coded row without an answer
    # must not hide a newer source-coded one.
    'empty concept row': ([
        ('m', _ISS, _ISS, '2020-01-01', {}),
        ('m', _T, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
    ], 'Stage IV'),
    'boolean concept row': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_as_string': 'true'}),
        ('m', _T, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
    ], 'Stage IV'),
    # The string is chosen before the row is judged, so a padded boolean
    # rejects the row although its answer concept is a stage.
    'padded boolean beside a concept answer': ([
        ('m', _ISS, _ISS, '2020-01-01',
         {'value_as_string': ' Yes ', 'answer_concept': 'Stage II'}),
        ('m', _T, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
    ], 'Stage IV'),
    'whitespace string beside a concept answer': ([
        ('m', _ISS, _ISS, '2020-01-01',
         {'value_as_string': '   ', 'answer_concept': 'Stage II'}),
    ], None),
    'empty string falls through to the concept': ([
        ('m', _ISS, _ISS, '2020-01-01',
         {'value_as_string': '', 'answer_concept': 'Stage 4 (qualifier value)'}),
    ], 'Stage IV'),
    'number is truncated': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_as_number': Decimal('2.8')}),
    ], '2'),
    'source value is not an answer': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_source_value': 'Stage IV'}),
    ], None),
    # Stripped as `str.strip()` strips, whatever the database locale.
    'NBSP-only string': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_as_string': '\xa0'}),
        ('m', _T, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
    ], 'Stage IV'),
    'boolean padded with NEL': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_as_string': 'true\x85'}),
        ('m', _T, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
    ], 'Stage IV'),
    'label is normalised': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_as_string': ' stage 3a '}),
    ], 'Stage IIIA'),
    # Reported as finding 3: R-ISS in any table before ISS.
    'R-ISS observation over ISS measurement': ([
        ('m', _ISS, _ISS, '2026-01-01', {'value_as_string': 'Stage I'}),
        ('o', _T, _RISS, '2020-01-01', {'value_as_string': 'Stage III'}),
    ], 'Stage III'),
    'entered-in-error observation': ([
        ('o', _ISS, _ISS, '2020-01-01',
         {'value_as_string': 'Stage II', 'is_erroneous': True}),
    ], None),
    'provisional measurement': ([
        ('m', _ISS, _ISS, '2020-01-01', {'value_as_string': 'Stage II', 'confidence': 80}),
    ], None),
    'observation concept row before a newer source row': ([
        ('o', _ISS, _ISS, '2020-01-01', {'value_as_string': 'Stage I'}),
        ('o', _T, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
    ], 'Stage I'),
    'measurement before observation': ([
        ('o', _ISS, _ISS, '2026-01-01', {'value_as_string': 'Stage IV'}),
        ('m', _T, _ISS, '2020-01-01', {'value_as_string': 'Stage I'}),
    ], 'Stage I'),
}


@pytest.mark.parametrize('rows, expected', _STAGE_CASES.values(), ids=_STAGE_CASES)
def test_stage_agrees_with_the_staging_extractor(rows, expected):
    person = PersonFactory(year_of_birth=1960)
    _seed_rows(person, rows)

    assert _get_staging_data(person).get('stage') == expected
    assert _fetch_omop_trial_row(person.person_id)['stage'] == expected


_HER2 = '48676-1'

_RECEPTOR_CASES = {
    # Reported as finding 2: `_first_by_code` reads the concept code only.
    'source-only row': ([
        ('m', 'LOCAL-HER2', _HER2, '2020-01-01', {'value_as_string': 'Positive'}),
    ], None),
    'answer concept before string': ([
        ('m', _HER2, _HER2, '2020-01-01',
         {'answer_concept': 'Negative', 'value_as_string': 'Positive'}),
    ], 'Negative'),
    'empty string falls through to the source value': ([
        ('m', _HER2, _HER2, '2020-01-01',
         {'value_as_string': '', 'value_source_value': 'Equivocal (IHC 2+)'}),
    ], 'Equivocal'),
    # Unlike stage, the newest row wins even without an answer.
    'empty newest row over an older positive': ([
        ('m', _HER2, _HER2, '2020-01-01', {'value_as_string': 'Positive'}),
        ('m', _HER2, _HER2, '2026-01-01', {}),
    ], None),
    'provisional row': ([
        ('m', _HER2, _HER2, '2020-01-01', {'value_as_string': 'Positive', 'confidence': 80}),
    ], None),
}


@pytest.mark.parametrize('rows, expected', _RECEPTOR_CASES.values(), ids=_RECEPTOR_CASES)
def test_her2_agrees_with_the_biomarker_extractor(rows, expected):
    person = PersonFactory(year_of_birth=1960)
    _seed_rows(person, rows)

    assert _get_biomarker_data(person).get('her2_status') == expected
    assert _fetch_omop_trial_row(person.person_id)['her2_status'] == expected


def test_the_strip_class_is_every_character_python_strips():
    """The SQL strip spells out `str.isspace`; a character above the range it
    scans would be stripped by the extractor and kept by the benchmark."""
    import sys

    assert PYTHON_WHITESPACE == ''.join(
        c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace())
