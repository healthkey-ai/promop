"""The fields a patient tab renders decide which proposals block an attestation (#2761).

``patient_visible_fields.json`` is generated from the tab components. Two copies exist
(the backend reads ``omop_core/data``, the frontend imports the one beside its code),
so they must match each other and what the tabs say today.
"""
import json

from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
from omop_core.services import patient_visible_fields as visible

REGENERATE = 'regenerate with: python -m omop_core.services.patient_visible_fields --write'


def test_the_stored_list_matches_what_the_tabs_name_today():
    assert sorted(visible.names()) == visible.scan(), REGENERATE


def test_both_copies_are_identical():
    backend = (visible.ROOT / visible.DATA_COPY).read_text()
    frontend = (visible.ROOT / visible.FRONTEND_COPY).read_text()
    assert backend == frontend == visible.render(visible.scan()), REGENERATE


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


def test_the_file_is_sorted_json():
    data = json.loads((visible.ROOT / visible.DATA_COPY).read_text())
    assert data['fields'] == sorted(set(data['fields']))
