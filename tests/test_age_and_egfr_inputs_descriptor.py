"""The derived values a trial asks for name the fields that move them.

`patient_age` is computed from the birth date, and the matcher recomputes eGFR
from creatinine, age and gender. A client that offers a box on the computed
value writes something the next derivation replaces; naming the inputs is the
descriptor's half of the fix (#5664).
"""
from omop_core.services.write_descriptor import (
    KIND_ALIAS,
    KIND_COMPUTED,
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
    assert entry['inputs'] == ['serum_creatinine_level', 'patient_age', 'gender']


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
