"""The fields a patient tab renders decide which proposals block an attestation (#2761).

``patientVisibleFields.json`` (beside the frontend code, read here and imported by the
frontend) is generated from the tab components, so it must match what the tabs say today.
The always-shown tabs give ``fields``; the disease tab gives ``disease_fields`` per disease
type, chosen by the same keyword rules the page uses.
"""
import json
import re

import pytest

from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
from omop_core.services import patient_visible_fields as visible

REGENERATE = 'regenerate with: python -m omop_core.services.patient_visible_fields --write'


def test_the_stored_list_matches_what_the_tabs_name_today():
    assert sorted(visible.names()) == visible.scan(), REGENERATE


def test_the_file_is_exactly_what_the_generator_writes():
    assert (visible.ROOT / visible.LIST_FILE).read_text() == visible.render(
        visible.scan(), visible.scan_disease()), REGENERATE


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


def _stored():
    return json.loads((visible.ROOT / visible.LIST_FILE).read_text())


def test_every_section_of_the_disease_tab_is_mapped_to_a_disease_type():
    # A new `*Section` function that no disease type names would render fields the
    # attestation never counts.
    source = (visible.ROOT / visible.TAB_DIR / visible.DISEASE_TAB).read_text()
    sections = set(visible.function_blocks(source)) - set(visible.NOT_SECTIONS)
    mapped = {name for names in visible.DISEASE_SECTIONS.values() for name in names}
    assert sections == mapped, 'map each DiseaseTab section in DISEASE_SECTIONS'


def test_the_disease_fields_are_scalar_columns_per_disease_type_and_staff_fields_stay_out():
    data = _stored()
    assert sorted(data['disease_fields']) == sorted(visible.DISEASE_SECTIONS)
    for disease, listed in data['disease_fields'].items():
        assert listed == sorted(set(listed)), disease
        assert set(listed) <= visible.scalar_confidence_columns(), disease
        assert not set(listed) & set(PROVISIONAL_JSON_FIELDS), disease
        assert not set(listed) & set(data['not_for_patients']), disease


def test_the_controls_that_bypass_field_are_found_by_the_scan():
    data = _stored()
    # The treating institution picker is never proposed (identity and contact fields are
    # never overlaid), so it is not wrapped and not listed; nor is planned therapies,
    # which is not a column that holds a proposal.
    assert 'facility_name' not in visible.names()
    assert 'planned_therapies' not in visible.scalar_confidence_columns()
    # GELF and FLIPI are the lymphoma section's, and only that section's.
    assert {'gelf_criteria_options', 'flipi_score'} <= set(data['disease_fields']['lymphoma'])
    for other in ('breast', 'myeloma', 'cll', 'mcl'):
        assert not {'gelf_criteria_options', 'flipi_score'} & set(data['disease_fields'][other])
    # The checklist's own factor keys are not record fields.
    assert 'splenomegaly' not in data['disease_fields']['lymphoma']


@pytest.mark.django_db
def test_a_disease_field_is_on_screen_only_for_its_disease():
    assert 'b_symptom_fever' in visible.on_screen('Follicular Lymphoma')
    assert 'b_symptom_fever' not in visible.on_screen('Multiple Myeloma')
    assert 'b_symptom_fever' not in visible.on_screen(None)
    # Whatever the disease, the always-shown tabs are.
    for disease in ('Follicular Lymphoma', 'Multiple Myeloma', None, ''):
        assert visible.names() <= visible.on_screen(disease)


# The same cases are in provisional.test.ts: the page and the server must pick the
# same disease section from one text.
DISEASE_TYPE_CASES = [
    ('Breast Cancer', 'breast'),
    ('Mantle Cell Lymphoma', 'mcl'),
    ('Follicular Lymphoma', 'lymphoma'),
    ('Multiple Myeloma', 'myeloma'),
    ('CLL', 'cll'),
    ('Chronic Lymphocytic Leukemia', 'cll'),
    ('Squamous cell carcinoma', 'other'),
    ('', 'other'),
    (None, 'other'),
    (42, 'other'),
    ('breast lymphoma', 'breast'),          # first rule wins
    ('MANTLE CELL LYMPHOMA', 'mcl'),        # case does not matter
]


@pytest.mark.parametrize('text,expected', DISEASE_TYPE_CASES)
def test_the_disease_type_follows_the_pages_keyword_rules(text, expected):
    assert visible.disease_type(text) == expected


def test_the_pages_own_rule_is_the_generated_one():
    # PatientDetail picks the section by the record's Disease Category (#2332) and
    # falls back to provisional.ts's keyword rules, which come from the file; it
    # must not keep a second copy of the keywords.
    source = (visible.ROOT / 'frontend/src/components/Patient/PatientDetail.tsx').read_text()
    assert re.search(r'getDiseaseType = \(\) => diseaseTypeOfCategory\(recordCategory\)', source)
    assert re.search(r'recordDiseaseCategory\(.*?diseaseTypeOf\(editedInfo\?\.disease\)', source, re.S)
    assert 'd.includes("mantle")' not in source


def test_the_disease_tab_switch_renders_each_section_for_the_type_the_map_says():
    # Swapping two `case` lines would put a section's fields on the wrong disease's list.
    source = (visible.ROOT / visible.TAB_DIR / visible.DISEASE_TAB).read_text()
    assert visible.switch_sections(source) == {
        kind: set(names) for kind, names in visible.DISEASE_SECTIONS.items()}
    assert _stored()['disease_sections'] == {
        kind: list(names) for kind, names in visible.DISEASE_SECTIONS.items()}


def test_the_switch_check_notices_swapped_cases():
    source = (visible.ROOT / visible.TAB_DIR / visible.DISEASE_TAB).read_text()
    swapped = source.replace('<CLLSection', '<TMPSection').replace(
        '<MCLSection', '<CLLSection').replace('<TMPSection', '<MCLSection')
    assert visible.switch_sections(swapped) != visible.switch_sections(source)


def test_a_date_precision_follows_its_date_and_never_blocks_alone():
    # The precision picker is neither a Field nor a MarkedControl: it shows no indicator,
    # and re-picking a precision to clear a mark would rewrite a proposed date.
    data = _stored()
    columns = visible.scalar_confidence_columns()
    assert visible.is_precision_companion('sct_date_precision', columns)
    assert visible.is_precision_companion('dlbcl_transformation_date_precision', columns)
    assert not visible.is_precision_companion('sct_date', columns)
    assert not visible.is_precision_companion('some_other_precision', columns)   # no such date
    for listed in (data['fields'], *data['disease_fields'].values()):
        assert not [name for name in listed if visible.is_precision_companion(name, columns)]
    assert 'sct_date' in data['disease_fields']['myeloma']
    assert 'dlbcl_transformation_date' in data['disease_fields']['lymphoma']


def test_a_name_a_tab_shows_only_as_text_is_not_listed():
    data = _stored()
    assert 'cytogenetic_markers' in data['not_editable']
    assert 'cytogenetic_markers' not in data['disease_fields']['myeloma']


def test_the_scan_ignores_names_in_comments_and_keeps_urls():
    columns = frozenset({'hemoglobin_g_dl', 'sodium_meq_l'})
    assert visible._named('{/* hemoglobin_g_dl */} // "sodium_meq_l"\n', columns) == set()
    assert visible._named(
        '<a href="https://x.test/">a</a> name="hemoglobin_g_dl" // note', columns) == {'hemoglobin_g_dl'}
