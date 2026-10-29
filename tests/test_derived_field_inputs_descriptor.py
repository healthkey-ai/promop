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


def test_disease_slug_follows_a_direct_disease_edit():
    """The descriptor names `disease` as the input, so a direct edit has to move it.

    The full refresh derives the slug from the OMOP condition; a record-level edit
    does not touch that, so `recompute_patient_record_fields` owns it for the edit
    path. Without this the descriptor would promise an input that does nothing.
    """
    from omop_core.services.patient_record_service import recompute_patient_record_fields
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory(disease='Multiple Myeloma', disease_slug='multiple-myeloma')

    record.disease = 'Chronic Lymphocytic Leukemia'
    recompute_patient_record_fields(record, changed_fields={'disease'})
    record.refresh_from_db()

    assert record.disease_slug == 'chronic-lymphocytic-leukemia'


def test_the_shared_form_takes_its_navigation_from_the_descriptor():
    """`patient_form.FORM_INPUTS` used to carry its own copy for `meets_slim`.

    It listed `bone_lesions` and omitted `kappa_lambda_ratio`, while
    `derive_meets_slim` reads the FLC ratio and deliberately does not infer the MRI
    criterion from `bone_lesions`. The table is still what the form serves, so it is
    corrected in place and this test pins it to the descriptor.
    """
    from omop_core.services.patient_form import patient_form_descriptors

    entries = patient_form_descriptors()
    assert entries['meets_slim']['inputs'] == INPUTS['meets_slim']
    assert entries['b_symptoms']['inputs'] == INPUTS['b_symptoms']
    assert entries['disease_slug']['inputs'] == INPUTS['disease_slug']


def test_clearing_disease_clears_the_slug():
    """A cleared diagnosis must not keep a machine-readable one."""
    from omop_core.services.patient_record_service import recompute_patient_record_fields
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory(disease='Multiple Myeloma', disease_slug='multiple-myeloma')

    record.disease = None
    recompute_patient_record_fields(record, changed_fields={'disease'})
    record.refresh_from_db()

    assert record.disease_slug is None


def test_full_refresh_keeps_the_slug_in_step_with_a_pending_disease_edit():
    """The refresh derives the slug from OMOP, then restores the user's pending name.

    Without telling the recompute that the name moved, a saved diagnosis and its
    slug disagreed after every refresh.
    """
    from omop_core.services.patient_record_service import refresh_patient_record
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory(
        disease='Multiple Myeloma', disease_slug='multiple-myeloma',
        user_edited_fields=['disease'],
    )
    record.disease = 'Chronic Lymphocytic Leukemia'
    record.save(update_fields=['disease'])

    refreshed = refresh_patient_record(record.person)

    assert refreshed.disease == 'Chronic Lymphocytic Leukemia'
    assert refreshed.disease_slug == 'chronic-lymphocytic-leukemia'
