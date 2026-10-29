"""#2113: every derived field names the components a caller should edit instead.

`refresh_patient_record` owns these values, so a direct write was accepted and then
replaced by the next derivation. The descriptor now reports them computed; for the
ones derived from record fields it also names those fields, which is the edit
surface a client should offer. `active_infection_status` and `active_malignancies`
are read off OMOP condition facts and have no record component, so they are computed
with no inputs rather than pointing at a field that cannot exist.
"""
import pytest

from omop_core.services.write_descriptor import (
    KIND_COMPUTED,
    build_writable_field_descriptor,
)

pytestmark = pytest.mark.django_db

INPUTS = {
    'b_symptoms': [
        'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
    ],
    'meets_slim': [
        'clonal_plasma_cells', 'kappa_flc', 'lambda_flc', 'kappa_lambda_ratio',
    ],
    'disease_slug': ['disease'],
    'active_infection_status': [],
    'active_malignancies': [],
}


@pytest.mark.parametrize('field, inputs', sorted(INPUTS.items()))
def test_derived_field_is_computed_and_names_its_inputs(field, inputs):
    entry = build_writable_field_descriptor()[field]

    assert entry['kind'] == KIND_COMPUTED
    assert entry['writable'] is False
    assert entry['inputs'] == inputs


@pytest.mark.parametrize('component', sorted({
    'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
    'clonal_plasma_cells', 'kappa_flc', 'lambda_flc', 'kappa_lambda_ratio', 'disease',
}))
def test_the_components_stay_writable(component):
    entry = build_writable_field_descriptor()[component]

    assert entry['writable'] is True, (
        f'{component} is the field a caller edits to move the verdict; refusing it '
        'would leave the computed field with no edit path at all'
    )
