"""Tests for the field descriptor service."""

import pytest

from omop_core.models import PatientRecord, FieldConceptMapping
from omop_core.services.field_descriptor import get_all_field_descriptors
from omop_core.services.mappings import LAB_FIELD_TO_LOINC


pytestmark = pytest.mark.django_db


def test_all_concrete_fields_covered():
    """Every concrete PatientRecord field appears in the descriptor output."""
    concrete_names = {
        f.name for f in PatientRecord._meta.get_fields()
        if getattr(f, 'concrete', False)
    }
    descriptors = get_all_field_descriptors()
    descriptor_names = {d['field_name'] for d in descriptors}
    missing = concrete_names - descriptor_names
    assert missing == set(), f"Fields missing from descriptors: {missing}"


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
    required_keys = {'field_name', 'field_type', 'category', 'tab', 'provenance', 'mapping'}
    for d in descriptors:
        missing = required_keys - set(d.keys())
        assert missing == set(), f"Descriptor for {d.get('field_name')} missing keys: {missing}"


def test_categories_are_valid():
    """All categories returned are from the known set."""
    valid_categories = {
        'editable', 'alias', 'unit', 'profile', 'location',
        'therapy-inference', 'computed', 'wearable-metadata',
        'needs-concept-set', 'internal', 'other',
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
        'behavior', 'wearables', 'internal', 'other',
    }
    descriptors = get_all_field_descriptors()
    found_tabs = {d['tab'] for d in descriptors}
    unknown = found_tabs - valid_tabs
    assert unknown == set(), f"Unknown tabs: {unknown}"


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
        'id': 'internal',
    }
    for field, expected_tab in checks.items():
        assert field in by_name, f"Field '{field}' not in descriptors"
        assert by_name[field]['tab'] == expected_tab, (
            f"Field '{field}' should be tab '{expected_tab}' but is '{by_name[field]['tab']}'"
        )
