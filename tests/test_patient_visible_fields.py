"""The fields a patient tab renders decide which proposals block an attestation (#2761).

``patientVisibleFields.json`` (beside the frontend code, read here and imported by the
frontend) is generated from the tab components, so it must match what the tabs say today.
"""
import json

from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
from omop_core.services import patient_visible_fields as visible

REGENERATE = 'regenerate with: python -m omop_core.services.patient_visible_fields --write'


def test_the_stored_list_matches_what_the_tabs_name_today():
    assert sorted(visible.names()) == visible.scan(), REGENERATE


def test_the_file_is_exactly_what_the_generator_writes():
    assert (visible.ROOT / visible.LIST_FILE).read_text() == visible.render(visible.scan()), REGENERATE


def test_only_scalar_fields_that_can_carry_a_proposal_are_listed():
    listed = visible.names()
    assert listed <= visible.scalar_confidence_columns()
    assert not listed & set(PROVISIONAL_JSON_FIELDS)


def test_fields_the_server_proposes_but_no_patient_tab_shows_are_not_listed():
    listed = visible.names()
    for hidden in ('concomitant_medications', 'diagnosis_date', 'diagnosis_date_precision',
                   'condition_clinical_status', 'disease_slug', 'blood_type', 'ejection_fraction'):
        assert hidden not in listed
    for shown in ('hemoglobin_g_dl', 'sodium_meq_l', 'disease', 'weight', 'smoking_status'):
        assert shown in listed


def test_fields_a_patient_cannot_see_or_edit_are_not_listed():
    data = json.loads((visible.ROOT / visible.LIST_FILE).read_text())
    for staff_only in ('validated', 'validated_by', 'validation_date'):
        assert staff_only not in visible.names()
        assert staff_only in data['not_for_patients']


def test_the_stored_columns_are_the_models_scalar_confidence_columns():
    data = json.loads((visible.ROOT / visible.LIST_FILE).read_text())
    assert data['columns'] == sorted(visible.scalar_confidence_columns()), REGENERATE


def test_the_file_is_sorted_json():
    data = json.loads((visible.ROOT / visible.LIST_FILE).read_text())
    assert data['fields'] == sorted(set(data['fields']))
