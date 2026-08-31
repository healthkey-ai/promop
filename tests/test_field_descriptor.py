"""Tests for the field descriptor service."""

import pytest

from omop_core.models import PatientRecord, FieldConceptMapping, FieldChoice, FieldChoiceCode, FieldFormula
from omop_core.services.field_descriptor import (
    get_all_field_descriptors,
    _INTERNAL_FIELDS,
)
from omop_core.services.mappings import LAB_FIELD_TO_LOINC


pytestmark = pytest.mark.django_db


def test_all_concrete_fields_covered():
    """Every mapping-relevant concrete PatientRecord field appears in the output."""
    concrete_names = {
        f.name for f in PatientRecord._meta.get_fields()
        if getattr(f, 'concrete', False)
    } - _INTERNAL_FIELDS
    concrete_names = {name for name in concrete_names if not name.endswith('_units')}
    descriptors = get_all_field_descriptors()
    descriptor_names = {d['field_name'] for d in descriptors}
    missing = concrete_names - descriptor_names
    assert missing == set(), f"Fields missing from descriptors: {missing}"


def test_internal_fields_excluded():
    """Internal fields (id, person, organization, etc.) are not in the output."""
    descriptors = get_all_field_descriptors()
    descriptor_names = {d['field_name'] for d in descriptors}
    for field in _INTERNAL_FIELDS:
        assert field not in descriptor_names, f"Internal field '{field}' should be excluded"


def test_unit_companion_fields_excluded():
    """Units are configured on measurement mappings, not mapped as fields themselves."""
    descriptor_names = {d['field_name'] for d in get_all_field_descriptors()}
    unit_fields = {
        f.name for f in PatientRecord._meta.get_fields()
        if getattr(f, 'concrete', False) and f.name.endswith('_units')
    }
    assert unit_fields
    assert descriptor_names.isdisjoint(unit_fields)


def test_lab_fields_categorized_editable():
    """Fields in LAB_FIELD_TO_LOINC are categorized as 'editable'."""
    descriptors = get_all_field_descriptors()
    by_name = {d['field_name']: d for d in descriptors}
    for field_name in LAB_FIELD_TO_LOINC:
        assert field_name in by_name, f"LAB_FIELD_TO_LOINC field '{field_name}' missing from descriptors"
        assert by_name[field_name]['category'] == 'editable', (
            f"Field '{field_name}' should be 'editable' but is '{by_name[field_name]['category']}'"
        )


def test_provenance_merged():
    """Fields with provenance registry entries show concept codes."""
    descriptors = get_all_field_descriptors()
    # hemoglobin_g_dl has provenance via auto-registration (LOINC 718-7)
    hb = next(d for d in descriptors if d['field_name'] == 'hemoglobin_g_dl')
    assert hb['provenance'] is not None
    assert '718-7' in hb['provenance']['concept_codes']


def test_active_condition_derivations_are_read_only_computed_descriptors():
    descriptors = {d['field_name']: d for d in get_all_field_descriptors()}

    infection = descriptors['active_infection_status']
    malignancies = descriptors['active_malignancies']

    assert infection['category'] == 'computed'
    assert infection['mappable'] is False
    assert infection['provenance']['concept_codes'] == ['40733004']
    assert infection['provenance']['extractor'] == '_get_active_condition_data'
    assert malignancies['category'] == 'computed'
    assert malignancies['mappable'] is False
    assert malignancies['provenance']['concept_codes'] == ['363346000']


def test_mapping_merged():
    """FieldConceptMapping rows appear in descriptor output."""
    FieldConceptMapping.objects.create(
        field_name='smoking_status',
        vocabulary_id='SNOMED',
        concept_code='229819007',
        status='proposed',
    )
    descriptors = get_all_field_descriptors()
    smoking = next(d for d in descriptors if d['field_name'] == 'smoking_status')
    assert smoking['mapping'] is not None
    assert smoking['mapping']['vocabulary_id'] == 'SNOMED'
    assert smoking['mapping']['concept_code'] == '229819007'
    assert smoking['mapping']['status'] == 'proposed'


def test_descriptors_have_required_keys():
    """Each descriptor dict contains the expected keys."""
    descriptors = get_all_field_descriptors()
    required_keys = {
        'field_name', 'field_type', 'category', 'tab', 'provenance', 'mapping',
        'suggestion', 'mappable', 'locked_table', 'choices', 'formula', 'derivation_error',
    }
    for d in descriptors:
        missing = required_keys - set(d.keys())
        assert missing == set(), f"Descriptor for {d.get('field_name')} missing keys: {missing}"


def test_categories_are_valid():
    """All categories returned are from the known set."""
    valid_categories = {
        'editable', 'alias', 'profile', 'location',
        'therapy-inference', 'computed', 'needs-concept-set', 'other',
    }
    descriptors = get_all_field_descriptors()
    found_categories = {d['category'] for d in descriptors}
    unknown = found_categories - valid_categories
    assert unknown == set(), f"Unknown categories: {unknown}"


def test_tab_key_present():
    """Every descriptor has a 'tab' key."""
    descriptors = get_all_field_descriptors()
    for d in descriptors:
        assert 'tab' in d, f"Descriptor for {d['field_name']} missing 'tab' key"


def test_tabs_are_valid():
    """All tab values are from the known set."""
    valid_tabs = {
        'general', 'disease', 'treatment', 'blood', 'labs',
        'behavior', 'other',
    }
    descriptors = get_all_field_descriptors()
    found_tabs = {d['tab'] for d in descriptors}
    unknown = found_tabs - valid_tabs
    assert unknown == set(), f"Unknown tabs: {unknown}"


def test_no_internal_tab():
    """No descriptor should have tab='internal'."""
    descriptors = get_all_field_descriptors()
    internal_tabs = [d for d in descriptors if d['tab'] == 'internal']
    assert internal_tabs == [], f"Fields with internal tab: {[d['field_name'] for d in internal_tabs]}"


def test_no_wearables_tab():
    """No descriptor should have tab='wearables'."""
    descriptors = get_all_field_descriptors()
    wearable_tabs = [d for d in descriptors if d['tab'] == 'wearables']
    assert wearable_tabs == [], f"Fields with wearables tab: {[d['field_name'] for d in wearable_tabs]}"


def test_known_field_tab_assignments():
    """Spot-check specific field->tab assignments."""
    descriptors = get_all_field_descriptors()
    by_name = {d['field_name']: d for d in descriptors}
    checks = {
        'hemoglobin_g_dl': 'blood',
        'smoking_status': 'behavior',
        'date_of_birth': 'general',
        'serum_creatinine_level': 'labs',
        'first_line_therapy': 'treatment',
        'myeloma_type': 'disease',
    }
    for field, expected_tab in checks.items():
        assert field in by_name, f"Field '{field}' not in descriptors"
        assert by_name[field]['tab'] == expected_tab, (
            f"Field '{field}' should be tab '{expected_tab}' but is '{by_name[field]['tab']}'"
        )


def test_reclassified_fields():
    """Fields reclassified from 'other' to proper tabs."""
    descriptors = get_all_field_descriptors()
    by_name = {d['field_name']: d for d in descriptors}
    checks = {
        'diagnosis_date': 'general',
        'death_date': 'general',
        'tumor_size': 'disease',
        'lymph_node_status': 'disease',
        'metastasis_status': 'disease',
        'toxicity_grade': 'treatment',
        'condition_code_icd_10': 'disease',
        'prior_procedures': 'disease',
        'metastatic_status': 'disease',
        'reason_for_discontinuation': 'treatment',
        'serum_creatinine_mg_dl': 'labs',
        'renal_adequacy_status': 'labs',
        'pregnancy_test_result': 'behavior',
        'median_daily_steps_30d': 'behavior',
    }
    for field, expected_tab in checks.items():
        if field in by_name:
            assert by_name[field]['tab'] == expected_tab, (
                f"Field '{field}' should be tab '{expected_tab}' but is '{by_name[field]['tab']}'"
            )


# ── Suggestion tests ──────────────────────────────────────────────


def test_suggestion_present_for_loinc_fields():
    """Fields in LAB_FIELD_TO_LOINC have suggestion with concept_code."""
    descriptors = get_all_field_descriptors()
    by_name = {d['field_name']: d for d in descriptors}
    for field_name, (code, unit, display) in LAB_FIELD_TO_LOINC.items():
        if field_name in by_name:
            d = by_name[field_name]
            assert d['suggestion'] is not None, f"Field '{field_name}' should have a suggestion"
            assert d['suggestion']['concept_code'] == code, (
                f"Field '{field_name}' suggestion code should be '{code}'"
            )
            assert d['suggestion']['vocabulary_id'] == 'LOINC'


def test_suggestion_has_unit_for_lab_fields():
    """Lab fields have unit in suggestion."""
    descriptors = get_all_field_descriptors()
    by_name = {d['field_name']: d for d in descriptors}
    for field_name, (code, unit, display) in LAB_FIELD_TO_LOINC.items():
        if field_name in by_name and unit:
            d = by_name[field_name]
            assert d['suggestion']['unit'] == unit, (
                f"Field '{field_name}' suggestion unit should be '{unit}'"
            )


# ── Mappable tests ────────────────────────────────────────────────


def test_mappable_false_for_computed():
    """Computed fields have mappable=False."""
    descriptors = get_all_field_descriptors()
    computed = [d for d in descriptors if d['category'] == 'computed']
    assert len(computed) > 0, "Should have at least some computed fields"
    for d in computed:
        assert d['mappable'] is False, f"Computed field '{d['field_name']}' should have mappable=False"


def test_30d_fields_are_computed():
    """All _30d fields have category='computed' and mappable=False."""
    descriptors = get_all_field_descriptors()
    for d in descriptors:
        if d['field_name'].endswith('_30d'):
            assert d['category'] == 'computed', (
                f"Field '{d['field_name']}' should be 'computed' but is '{d['category']}'"
            )
            assert d['mappable'] is False


def test_mappable_true_for_editable():
    """Editable (LOINC) fields have mappable=True."""
    descriptors = get_all_field_descriptors()
    editable = [d for d in descriptors if d['category'] == 'editable']
    assert len(editable) > 0
    for d in editable:
        assert d['mappable'] is True, f"Editable field '{d['field_name']}' should have mappable=True"


# ── Locked table tests ───────────────────────────────────────────


def test_locked_table_for_profile():
    """Profile fields have locked_table='Person'."""
    descriptors = get_all_field_descriptors()
    profile = [d for d in descriptors if d['category'] == 'profile']
    assert len(profile) > 0
    for d in profile:
        assert d['locked_table'] == 'Person', (
            f"Profile field '{d['field_name']}' should have locked_table='Person'"
        )


def test_locked_table_for_location():
    """Location fields have locked_table='Location'."""
    descriptors = get_all_field_descriptors()
    location = [d for d in descriptors if d['category'] == 'location']
    assert len(location) > 0
    for d in location:
        assert d['locked_table'] == 'Location', (
            f"Location field '{d['field_name']}' should have locked_table='Location'"
        )


def test_locked_table_none_for_others():
    """Non-profile, non-location fields have locked_table=None."""
    descriptors = get_all_field_descriptors()
    for d in descriptors:
        if d['category'] not in ('profile', 'location'):
            assert d['locked_table'] is None, (
                f"Field '{d['field_name']}' (category={d['category']}) should have locked_table=None"
            )


# ── Choices tests (Phase 3) ─────────────────────────────────────


def test_choices_included_in_descriptors():
    """Field choices appear in descriptor output when seeded."""
    FieldChoice.objects.all().delete()
    choice = FieldChoice.objects.create(field_name='disease', display='Test Disease', sort_order=0)
    FieldChoiceCode.objects.create(choice=choice, code='12345', vocabulary_id='SNOMED', is_primary=True)

    descriptors = get_all_field_descriptors()
    disease = next(d for d in descriptors if d['field_name'] == 'disease')
    assert len(disease['choices']) == 1
    assert disease['choices'][0]['display'] == 'Test Disease'
    assert disease['choices'][0]['codes'][0]['code'] == '12345'


def test_choices_empty_for_fields_without():
    """Non-choice fields have empty choices list."""
    FieldChoice.objects.all().delete()
    descriptors = get_all_field_descriptors()
    hb = next(d for d in descriptors if d['field_name'] == 'hemoglobin_g_dl')
    assert hb['choices'] == []


# ── Formula tests (Phase 5) ─────────────────────────────────────


def test_formula_included_in_descriptors():
    """Formula data appears in descriptors for computed fields with formulas."""
    FieldFormula.objects.all().delete()
    FieldFormula.objects.create(field_name='bmi', formula='weight / (height / 100) ^ 2', is_active=False)

    descriptors = get_all_field_descriptors()
    bmi = next(d for d in descriptors if d['field_name'] == 'bmi')
    assert bmi['formula'] is not None
    assert bmi['formula']['expression'] == 'weight / (height / 100) ^ 2'
    assert bmi['formula']['is_active'] is False


def test_formula_none_for_non_computed():
    """Non-computed fields without formulas have formula=None."""
    FieldFormula.objects.all().delete()
    descriptors = get_all_field_descriptors()
    hb = next(d for d in descriptors if d['field_name'] == 'hemoglobin_g_dl')
    assert hb['formula'] is None


def test_invalid_stored_formula_is_flagged_as_a_derivation_error():
    """Legacy/direct DB formula rows remain visible but cannot look healthy."""
    FieldFormula.objects.all().delete()
    FieldFormula.objects.create(field_name='bmi', formula='unknown_input + 1', is_active=False)

    descriptors = {d['field_name']: d for d in get_all_field_descriptors()}

    assert descriptors['bmi']['derivation_error'] == 'Invalid formula: Unknown field: unknown_input'


# ── Descriptor keys test (updated for new keys) ────────────────


def test_descriptors_have_choices_and_formula_keys():
    """Each descriptor dict contains choices and formula keys."""
    descriptors = get_all_field_descriptors()
    for d in descriptors:
        assert 'choices' in d, f"Descriptor for {d['field_name']} missing 'choices' key"
        assert 'formula' in d, f"Descriptor for {d['field_name']} missing 'formula' key"
        assert 'derivation_error' in d, f"Descriptor for {d['field_name']} missing 'derivation_error' key"
