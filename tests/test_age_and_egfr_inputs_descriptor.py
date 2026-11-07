"""The derived values a trial asks for name the fields that move them.

`patient_age` is computed from the birth date, and the matcher recomputes eGFR
from creatinine, age and gender. A client that offers a box on the computed
value writes something the next derivation replaces; naming the inputs is the
descriptor's half of the fix (#5664).
"""
from omop_core.services.write_descriptor import (
    KIND_ALIAS,
    KIND_COMPUTED,
    KIND_DIRECT,
    KIND_EXPLICIT_ANSWER,
    build_writable_field_descriptor,
)

import pytest

pytestmark = pytest.mark.django_db


def test_age_names_the_birth_date_as_its_input():
    entry = build_writable_field_descriptor()['patient_age']

    assert entry['kind'] == KIND_COMPUTED
    assert entry['writable'] is False
    assert entry['inputs'] == ['date_of_birth']


def test_the_birth_date_itself_is_writable():
    entry = build_writable_field_descriptor()['date_of_birth']

    assert entry['writable'] is True
    assert entry['value_kind'] == 'date'
    # A Person projection: the age dialog is the one place that may cross the
    # demographic boundary, because the date IS the row's input.
    assert entry['projection_target'] == 'person'


def test_egfr_names_the_inputs_the_matcher_recomputes_it_from():
    entry = build_writable_field_descriptor()['estimated_glomerular_filtration_rate']

    assert entry['kind'] == KIND_ALIAS
    assert entry['writable'] is False
    assert entry['canonical'] == 'egfr_ml_min_173m2'
    assert entry['inputs'] == ['serum_creatinine_mg_dl', 'date_of_birth', 'gender']


def test_every_named_input_is_a_described_field():
    descriptor = build_writable_field_descriptor()

    for field, inputs in (
        ('patient_age', descriptor['patient_age']['inputs']),
        ('estimated_glomerular_filtration_rate',
         descriptor['estimated_glomerular_filtration_rate']['inputs']),
    ):
        for input_name in inputs:
            assert input_name in descriptor, (
                f'{field} names {input_name}, which the descriptor cannot describe'
            )


def test_preexisting_conditions_is_an_explicit_answer_not_a_column_write():
    entry = build_writable_field_descriptor()['pre_existing_condition_categories']

    assert entry['kind'] == KIND_EXPLICIT_ANSWER
    assert entry['writable'] is False, (
        'a generic PATCH would race the snapshot the endpoint exists to guard'
    )
    assert entry['endpoint'] == 'preexisting-conditions-answer'
    assert set(entry['actions']) == {'confirm_none', 'set_categories', 'withdraw'}
    assert entry['answerable'] is True


def test_the_explicit_answer_carries_the_category_vocabulary_in_code_space():
    """The dialog writes codes, not the titles a trial shows.

    `preexisting_conditions` stores the vocabulary code (`cardiacIssues`);
    offering the display title would produce an answer the endpoint refuses.
    """
    from omop_core.models import PreExistingConditionCategory

    PreExistingConditionCategory.objects.create(
        code="cardiacIssues", title="Cardiac Issues",
    )
    entry = build_writable_field_descriptor()['pre_existing_condition_categories']
    options = {option['value']: option['label'] for option in entry['options']}

    assert 'cardiacIssues' in options
    assert options['cardiacIssues'] == 'Cardiac Issues'


def test_the_birth_date_precision_is_offered_as_a_choice():
    """A full date in a year-only record must not be silently truncated.

    PROMOP keeps `date_of_birth_precision` beside the date; the editor needs
    it as its own control or the reader's intent cannot be carried.
    """
    entry = build_writable_field_descriptor()['date_of_birth_precision']

    assert entry['writable'] is True
    assert [option['value'] for option in entry['options']] == [
        'year', 'month', 'day',
    ]


def test_meets_lugano_is_a_writable_input():
    """There is no PET/CT or marrow result in the record to derive it from.

    Leaving it read-only left the row with no door at all; it is the
    clinician's assessment, entered directly.
    """
    entry = build_writable_field_descriptor()['meets_lugano']

    assert entry['kind'] == KIND_DIRECT
    assert entry['writable'] is True
    assert entry['target'] == 'patient_record'
    assert entry['value_kind'] == 'boolean'
