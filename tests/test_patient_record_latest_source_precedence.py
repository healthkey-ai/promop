"""Latest dated source wins in PatientRecord projection (#1907).

These cover the sibling extractors of the assertion fix in #1905/#1906:
they iterated newest-first snapshot lists but assigned on every match, so
the oldest row won. Every case has an older and a newer fact, and the
expectation is always the newer one.
"""

from datetime import date

import pytest

from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConceptFactory,
    ConditionOccurrenceFactory,
    MeasurementFactory,
    ObservationFactory,
    PatientRecordFactory,
    PersonFactory,
)

pytestmark = pytest.mark.django_db

OLD = date(2026, 1, 1)
NEW = date(2026, 2, 1)


def _refresh(person):
    return refresh_patient_record(person)


# ---------------------------------------------------------------------------
# Infection (Measurement rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('code', 'field', 'no_field'),
    [
        ('5221-7', 'hiv_status', 'no_hiv_status'),
        ('7917-8', 'hiv_status', 'no_hiv_status'),
        ('5195-3', 'hepatitis_b_status', 'no_hepatitis_b_status'),
        ('5196-1', 'hepatitis_c_status', 'no_hepatitis_c_status'),
    ],
)
@pytest.mark.parametrize(
    ('old_value', 'new_value', 'expected', 'expected_no'),
    [
        ('positive', 'negative', False, True),
        ('negative', 'positive', True, False),
    ],
)
def test_infection_latest_result_wins(
    code, field, no_field, old_value, new_value, expected, expected_no,
):
    person = PersonFactory()
    concept = ConceptFactory(concept_code=code, concept_name=f'Infection {code}')
    with suppress_patient_record_refresh():
        PatientRecordFactory(
            person=person,
            **{field: not expected, no_field: expected},
        )
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_source_value=code, measurement_date=OLD,
            value_as_string=old_value,
        )
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_source_value=code, measurement_date=NEW,
            value_as_string=new_value,
        )

    record = _refresh(person)

    assert getattr(record, field) is expected
    assert getattr(record, no_field) is expected_no


def test_infection_union_does_not_let_stale_source_only_row_win():
    """The code/source union is grouped, so a stale source-only row is not "last"."""
    person = PersonFactory()
    coded = ConceptFactory(concept_code='5221-7', concept_name='HIV coded')
    source_only = ConceptFactory(concept_code='LOCAL-HIV', concept_name='HIV local')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, hiv_status=False, no_hiv_status=True)
        MeasurementFactory(
            person=person, measurement_concept=source_only,
            measurement_source_value='7917-8', measurement_date=OLD,
            value_as_string='negative',
        )
        MeasurementFactory(
            person=person, measurement_concept=coded,
            measurement_source_value='5221-7', measurement_date=NEW,
            value_as_string='positive',
        )

    record = _refresh(person)

    assert record.hiv_status is True
    assert record.no_hiv_status is False


def test_infection_newest_unusable_keeps_latest_recognized():
    person = PersonFactory()
    concept = ConceptFactory(concept_code='5221-7', concept_name='HIV')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, hiv_status=False, no_hiv_status=True)
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_source_value='5221-7', measurement_date=date(2026, 1, 1),
            value_as_string='negative',
        )
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_source_value='5221-7', measurement_date=date(2026, 2, 1),
            value_as_string='positive',
        )
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_source_value='5221-7', measurement_date=date(2026, 3, 1),
            value_as_string='indeterminate',
        )

    record = _refresh(person)

    assert record.hiv_status is True
    assert record.no_hiv_status is False


def test_infection_all_unusable_leaves_fields_unknown():
    person = PersonFactory()
    concept = ConceptFactory(concept_code='5221-7', concept_name='HIV')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        for day in (date(2026, 1, 1), date(2026, 2, 1)):
            MeasurementFactory(
                person=person, measurement_concept=concept,
                measurement_source_value='5221-7', measurement_date=day,
                value_as_string='indeterminate',
            )

    record = _refresh(person)

    assert record.hiv_status is None
    assert record.no_hiv_status is None


# ---------------------------------------------------------------------------
# Tobacco (Observation rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('old_answer', 'new_answer', 'expected', 'expected_details'),
    [
        ('LA18978-9', 'LA18976-3', False, 'Current smoker'),
        ('LA18976-3', 'LA18978-9', True, 'Never smoker'),
    ],
)
def test_tobacco_qa_latest_answer_and_details(
    old_answer, new_answer, expected, expected_details,
):
    person = PersonFactory()
    qa = ConceptFactory(concept_code='72166-2', concept_name='Tobacco smoking status')
    answers = {
        'LA18978-9': ConceptFactory(concept_code='LA18978-9', concept_name='Never smoker'),
        'LA18976-3': ConceptFactory(concept_code='LA18976-3', concept_name='Current smoker'),
    }
    with suppress_patient_record_refresh():
        PatientRecordFactory(
            person=person,
            no_tobacco_use_status=not expected,
            tobacco_use_details='stale',
        )
        ObservationFactory(
            person=person, observation_concept=qa,
            observation_date=OLD, value_as_concept=answers[old_answer],
        )
        ObservationFactory(
            person=person, observation_concept=qa,
            observation_date=NEW, value_as_concept=answers[new_answer],
        )

    record = _refresh(person)

    assert record.no_tobacco_use_status is expected
    assert record.tobacco_use_details == expected_details


@pytest.mark.parametrize(
    ('old_code', 'new_code', 'expected', 'expected_details'),
    [
        ('77176002', '266919005', True, 'Never smoker'),
        ('266919005', '77176002', False, 'Current smoker'),
    ],
)
def test_tobacco_legacy_latest_across_codes(
    old_code, new_code, expected, expected_details,
):
    person = PersonFactory()
    old_concept = ConceptFactory(concept_code=old_code, concept_name=f'Old {old_code}')
    new_concept = ConceptFactory(concept_code=new_code, concept_name=f'New {new_code}')
    with suppress_patient_record_refresh():
        PatientRecordFactory(
            person=person,
            no_tobacco_use_status=not expected,
            tobacco_use_details='stale',
        )
        ObservationFactory(
            person=person, observation_concept=old_concept,
            observation_source_value=old_code, observation_date=OLD,
            value_as_string='old',
        )
        ObservationFactory(
            person=person, observation_concept=new_concept,
            observation_source_value=new_code, observation_date=NEW,
            value_as_string='new',
        )

    record = _refresh(person)

    assert record.no_tobacco_use_status is expected
    assert record.tobacco_use_details == expected_details


def test_behavior_measurement_union_uses_newer_source_only_row():
    person = PersonFactory()
    coded = ConceptFactory(concept_code='93832-4', concept_name='Sleep hours')
    source_only = ConceptFactory(concept_code='LOCAL-SLEEP', concept_name='Sleep hours local')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, sleep_hours_per_night=1.0)
        MeasurementFactory(
            person=person, measurement_concept=coded,
            measurement_source_value='93832-4', measurement_date=OLD,
            value_as_number=6,
        )
        MeasurementFactory(
            person=person, measurement_concept=source_only,
            measurement_source_value='93832-4', measurement_date=NEW,
            value_as_number=8,
        )

    record = _refresh(person)

    assert record.sleep_hours_per_night == 8


def test_behavior_measurement_non_finite_answer_is_skipped():
    """A non-numeric/infinite newer row must not abort or replace a real one."""
    person = PersonFactory()
    coded = ConceptFactory(concept_code='89555-7', concept_name='Exercise minutes')
    source_only = ConceptFactory(
        concept_code='LOCAL-EXERCISE', concept_name='Exercise minutes local',
    )
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, exercise_minutes_per_week=1)
        MeasurementFactory(
            person=person, measurement_concept=coded,
            measurement_source_value='89555-7', measurement_date=OLD,
            value_as_number=90,
        )
        MeasurementFactory(
            person=person, measurement_concept=source_only,
            measurement_source_value='89555-7', measurement_date=NEW,
            value_as_string='inf',
        )

    record = _refresh(person)

    assert record.exercise_minutes_per_week == 90


# ---------------------------------------------------------------------------
# CLL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('concept_name', 'field', 'old_value', 'new_value', 'expected'),
    [
        ('Binet stage', 'binet_stage', 'A', 'C', 'C'),
        ('Tumor burden criteria', 'tumor_burden', 'Low', 'High', 'High'),
        ('Disease activity', 'disease_activity', 'Inactive', 'Active', 'Active'),
        ('Bone marrow involvement', 'bone_marrow_involvement', 'true', 'false', False),
        ('Hepatomegaly', 'hepatomegaly', 'true', 'false', False),
        ('Splenomegaly', 'splenomegaly', 'true', 'false', False),
        ('Lymphadenopathy', 'lymphadenopathy', 'true', 'false', False),
        ('Autoimmune cytopenia', 'autoimmune_cytopenias_refractory_to_steroids',
         'true', 'false', False),
    ],
)
def test_cll_latest_observation_per_field(
    concept_name, field, old_value, new_value, expected,
):
    person = PersonFactory()
    concept = ConceptFactory(concept_name=concept_name)
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=OLD, value_as_string=old_value,
        )
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=NEW, value_as_string=new_value,
        )

    record = _refresh(person)

    assert getattr(record, field) == expected


@pytest.mark.parametrize(
    ('concept_name', 'field', 'old_value', 'new_value', 'expected'),
    [
        ('Clonal bone marrow B lymphocytes', 'clonal_bone_marrow_b_lymphocytes', 10, 20, 20.0),
        ('Clonal B lymphocyte count', 'clonal_b_lymphocyte_count', 10, 20, 20),
        # Zero is a real derived answer, not a missing one.
        ('Clonal bone marrow B lymphocytes', 'clonal_bone_marrow_b_lymphocytes', 5, 0, 0.0),
        ('Clonal B lymphocyte count', 'clonal_b_lymphocyte_count', 5, 0, 0),
    ],
)
def test_cll_latest_clonal_measurement_per_field(
    concept_name, field, old_value, new_value, expected,
):
    person = PersonFactory()
    concept = ConceptFactory(concept_name=concept_name)
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_date=OLD, value_as_number=old_value,
        )
        MeasurementFactory(
            person=person, measurement_concept=concept,
            measurement_date=NEW, value_as_number=new_value,
        )

    record = _refresh(person)

    assert getattr(record, field) == expected


def test_cll_empty_newer_observation_keeps_older_answer():
    """An observation with no value must not fabricate a negative answer."""
    person = PersonFactory()
    concept = ConceptFactory(concept_name='Hepatomegaly')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=OLD, value_as_string='true',
        )
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=NEW,
        )

    record = _refresh(person)

    assert record.hepatomegaly is True


def test_cll_padded_boolean_answer_is_still_recognised():
    person = PersonFactory()
    concept = ConceptFactory(concept_name='Hepatomegaly')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=OLD, value_as_string='false',
        )
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=NEW, value_as_string=' TRUE ',
        )

    record = _refresh(person)

    assert record.hepatomegaly is True


@pytest.mark.parametrize(
    ('concept_name', 'field'),
    [
        ('Hepatomegaly', 'hepatomegaly'),
        ('Bone marrow involvement', 'bone_marrow_involvement'),
        ('Autoimmune cytopenia', 'autoimmune_cytopenias_refractory_to_steroids'),
    ],
)
def test_cll_unrecognised_answer_does_not_fabricate_negative(concept_name, field):
    person = PersonFactory()
    concept = ConceptFactory(concept_name=concept_name)
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=OLD, value_as_string='true',
        )
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=NEW, value_as_string='unknown',
        )

    record = _refresh(person)

    assert getattr(record, field) is True


def test_cll_unrecognised_numeric_answer_is_skipped():
    person = PersonFactory()
    concept = ConceptFactory(concept_name='Hepatomegaly')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=OLD, value_as_string='true',
        )
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=NEW, value_as_number=2,
        )

    record = _refresh(person)

    assert record.hepatomegaly is True


@pytest.mark.parametrize(
    ('concept_name', 'field', 'old_value'),
    [
        ('Tumor burden criteria', 'tumor_burden', 'Low'),
        ('Disease activity', 'disease_activity', 'Inactive'),
    ],
)
def test_cll_empty_newer_label_keeps_older_answer(concept_name, field, old_value):
    """An empty newer label must not replace a real older answer."""
    person = PersonFactory()
    concept = ConceptFactory(concept_name=concept_name)
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=OLD, value_as_string=old_value,
        )
        ObservationFactory(
            person=person, observation_concept=concept,
            observation_date=NEW,
        )

    record = _refresh(person)

    assert getattr(record, field) == old_value


def test_cll_newest_false_observation_blocks_condition_fallback():
    person = PersonFactory()
    observation = ConceptFactory(concept_name='Hepatomegaly')
    condition = ConceptFactory(concept_name='Hepatomegaly')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=observation,
            observation_date=OLD, value_as_string='true',
        )
        ObservationFactory(
            person=person, observation_concept=observation,
            observation_date=NEW, value_as_string='false',
        )
        ConditionOccurrenceFactory(
            person=person, condition_concept=condition,
            condition_start_date=date(2026, 3, 1),
        )

    record = _refresh(person)

    assert record.hepatomegaly is False


def test_cll_latest_richter_condition():
    person = PersonFactory()
    old_concept = ConceptFactory(concept_name='Richter transformation A')
    new_concept = ConceptFactory(concept_name='Richter transformation B')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, richter_transformation='stale')
        ConditionOccurrenceFactory(
            person=person, condition_concept=old_concept,
            condition_start_date=OLD,
        )
        ConditionOccurrenceFactory(
            person=person, condition_concept=new_concept,
            condition_start_date=NEW,
        )

    record = _refresh(person)

    assert record.richter_transformation == 'Richter transformation B'


# ---------------------------------------------------------------------------
# Lymphoma
# ---------------------------------------------------------------------------


def test_lymphoma_latest_flipi_score():
    person = PersonFactory()
    flipi_score = ConceptFactory(concept_name='FLIPI score')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=flipi_score,
            observation_date=OLD, value_as_number=4,
        )
        ObservationFactory(
            person=person, observation_concept=flipi_score,
            observation_date=NEW, value_as_number=0,
        )

    record = _refresh(person)

    assert record.flipi_score == 0


def test_lymphoma_latest_flipi_options():
    person = PersonFactory()
    flipi_options = ConceptFactory(concept_name='FLIPI criteria')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=flipi_options,
            observation_date=OLD, value_as_string='age',
        )
        ObservationFactory(
            person=person, observation_concept=flipi_options,
            observation_date=NEW, value_as_string='age,stage',
        )

    record = _refresh(person)

    assert record.flipi_score_options == 'age,stage'


def test_lymphoma_latest_gelf_status():
    person = PersonFactory()
    gelf = ConceptFactory(concept_name='GELF criteria')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=gelf,
            observation_date=OLD, value_as_string='Yes',
        )
        ObservationFactory(
            person=person, observation_concept=gelf,
            observation_date=NEW, value_as_string='No',
        )

    record = _refresh(person)

    assert record.gelf_criteria_status == 'No'


def test_lymphoma_empty_newer_gelf_does_not_erase_older_status():
    person = PersonFactory()
    gelf = ConceptFactory(concept_name='GELF criteria')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=gelf,
            observation_date=OLD, value_as_string='No',
        )
        ObservationFactory(
            person=person, observation_concept=gelf,
            observation_date=NEW,
        )

    record = _refresh(person)

    assert record.gelf_criteria_status == 'No'


def test_lymphoma_newer_empty_flipi_does_not_erase_older():
    person = PersonFactory()
    flipi_score = ConceptFactory(concept_name='FLIPI score')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        ObservationFactory(
            person=person, observation_concept=flipi_score,
            observation_date=OLD, value_as_number=3,
        )
        ObservationFactory(
            person=person, observation_concept=flipi_score,
            observation_date=NEW, value_as_string='',
        )

    record = _refresh(person)

    assert record.flipi_score == 3


# ---------------------------------------------------------------------------
# Multiple myeloma
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('source_value', 'field', 'expected'),
    [
        ('47082-2', 'plasma_cell_leukemia', False),
        ('89599-5', 'meets_crab', False),
        ('24646-7', 'bone_lesions', 'Absent'),
    ],
)
def test_mm_latest_within_each_table(source_value, field, expected):
    person = PersonFactory()
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        MeasurementFactory(
            person=person, measurement_source_value=source_value,
            measurement_date=OLD, value_as_string='true',
        )
        MeasurementFactory(
            person=person, measurement_source_value=source_value,
            measurement_date=NEW, value_as_string='false',
        )

    record = _refresh(person)

    assert getattr(record, field) == expected


@pytest.mark.parametrize(
    ('source_value', 'field', 'expected'),
    [
        ('47082-2', 'plasma_cell_leukemia', False),
        ('89599-5', 'meets_crab', False),
        ('24646-7', 'bone_lesions', 'Absent'),
    ],
)
@pytest.mark.parametrize('older_table', ['measurement', 'observation'])
def test_mm_latest_across_tables(source_value, field, expected, older_table):
    person = PersonFactory()
    older_factory = MeasurementFactory if older_table == 'measurement' else ObservationFactory
    newer_factory = ObservationFactory if older_table == 'measurement' else MeasurementFactory
    older_kwargs = (
        {'measurement_source_value': source_value, 'measurement_date': OLD}
        if older_table == 'measurement'
        else {'observation_source_value': source_value, 'observation_date': OLD}
    )
    newer_kwargs = (
        {'measurement_source_value': source_value, 'measurement_date': NEW}
        if older_table == 'observation'
        else {'observation_source_value': source_value, 'observation_date': NEW}
    )
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        older_factory(person=person, value_as_string='true', **older_kwargs)
        newer_factory(person=person, value_as_string='false', **newer_kwargs)

    record = _refresh(person)

    assert getattr(record, field) == expected


def test_mm_unrecognized_newer_string_keeps_older_answer():
    """An unrecognised string is not an explicit negative."""
    person = PersonFactory()
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person)
        MeasurementFactory(
            person=person, measurement_source_value='24646-7',
            measurement_date=OLD, value_as_string='true',
        )
        MeasurementFactory(
            person=person, measurement_source_value='24646-7',
            measurement_date=NEW, value_as_string='unknown',
        )

    record = _refresh(person)

    assert record.bone_lesions == 'Present'


# ---------------------------------------------------------------------------
# Tie-breaking and persistence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('older_day', 'older_id', 'newer_day', 'newer_id', 'expected'),
    [
        (OLD, 900100, OLD, 900101, False),  # same date: higher id wins
        (OLD, 900101, NEW, 900100, False),  # date outranks id
    ],
)
def test_selection_uses_date_then_id(
    older_day, older_id, newer_day, newer_id, expected,
):
    person = PersonFactory()
    concept = ConceptFactory(concept_code='5221-7', concept_name='HIV')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, hiv_status=True, no_hiv_status=False)
        MeasurementFactory(
            person=person, measurement_id=older_id, measurement_concept=concept,
            measurement_source_value='5221-7', measurement_date=older_day,
            value_as_string='positive',
        )
        MeasurementFactory(
            person=person, measurement_id=newer_id, measurement_concept=concept,
            measurement_source_value='5221-7', measurement_date=newer_day,
            value_as_string='negative',
        )

    record = _refresh(person)

    assert record.hiv_status is expected


def test_refresh_persists_latest_values_and_version_16():
    person = PersonFactory()
    hiv = ConceptFactory(concept_code='5221-7', concept_name='HIV')
    binet = ConceptFactory(concept_name='Binet stage')
    with suppress_patient_record_refresh():
        PatientRecordFactory(person=person, hiv_status=True, no_hiv_status=False)
        MeasurementFactory(
            person=person, measurement_concept=hiv,
            measurement_source_value='5221-7', measurement_date=OLD,
            value_as_string='positive',
        )
        MeasurementFactory(
            person=person, measurement_concept=hiv,
            measurement_source_value='5221-7', measurement_date=NEW,
            value_as_string='negative',
        )
        ObservationFactory(
            person=person, observation_concept=binet,
            observation_date=OLD, value_as_string='A',
        )
        ObservationFactory(
            person=person, observation_concept=binet,
            observation_date=NEW, value_as_string='C',
        )

    record = _refresh(person)

    record.refresh_from_db()
    assert record.hiv_status is False
    assert record.no_hiv_status is True
    assert record.binet_stage == 'C'
    from omop_core.services.patient_record_service import DERIVATION_VERSION
    assert record.derivation_version == DERIVATION_VERSION
