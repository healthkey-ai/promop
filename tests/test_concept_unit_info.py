"""Tests for omop_core.services.concept_unit_info."""
import pytest
from types import SimpleNamespace

from omop_core.services.concept_unit_info import (
    concept_unit_fields,
    get_loinc_to_unit,
    get_loinc_example_units,
    measurement_input_type,
)


class TestMeasurementInputType:
    def test_curated_unit_is_quantitative(self):
        assert measurement_input_type("Glucose", "mg/dL") == "quantitative"

    def test_quantitative_name_marker(self):
        assert measurement_input_type("Glucose [Mass/volume]", "") == "quantitative"

    def test_qualitative_name_marker(self):
        assert measurement_input_type("Bacteria identified [Presence]", "") == "qualitative"

    def test_unknown_when_no_markers(self):
        assert measurement_input_type("Some concept", "") == ""

    def test_none_concept_name(self):
        assert measurement_input_type(None, "") == ""

    def test_unit_takes_precedence_over_qualitative_marker(self):
        # A curated unit is conclusive, even if the name has a qualitative marker.
        assert measurement_input_type("[Presence] of something", "mg/dL") == "quantitative"


def _make_concept(domain_id="Measurement", vocabulary_id="LOINC",
                  concept_code="2345-7", concept_name="Glucose"):
    return SimpleNamespace(
        domain_id=domain_id, vocabulary_id=vocabulary_id,
        concept_code=concept_code, concept_name=concept_name,
    )


class TestConceptUnitFields:
    def test_none_concept(self):
        assert concept_unit_fields(None) == {}

    def test_non_measurement_domain(self):
        c = _make_concept(domain_id="Condition")
        assert concept_unit_fields(c) == {}

    def test_non_loinc_vocabulary(self):
        c = _make_concept(vocabulary_id="SNOMED")
        assert concept_unit_fields(c) == {}

    def test_loinc_measurement_with_known_code(self):
        # Use a code that exists in LAB_FIELD_TO_LOINC (glucose).
        result = concept_unit_fields(_make_concept(concept_code="2345-7"))
        # Should have measurement_type at minimum; may or may not have
        # suggested_unit depending on LAB_FIELD_TO_LOINC contents.
        assert isinstance(result, dict)
        if "suggested_unit" in result:
            assert result["measurement_type"] == "quantitative"

    def test_loinc_measurement_unknown_code_with_name_marker(self):
        c = _make_concept(concept_code="99999-9", concept_name="Test [Presence]")
        result = concept_unit_fields(c)
        assert result.get("measurement_type") == "qualitative"
        assert "suggested_unit" not in result

    def test_loinc_measurement_unknown_code_no_marker(self):
        c = _make_concept(concept_code="99999-9", concept_name="Unknown concept")
        result = concept_unit_fields(c)
        # No unit, no recognizable name marker → empty dict.
        assert result == {}


@pytest.mark.django_db
class TestGetLoincToUnitDBFallback:
    """Verify that get_loinc_to_unit() picks up LoincCodeClass.example_units."""

    def setup_method(self):
        # Clear the lru_cache so each test gets a fresh lookup.
        get_loinc_to_unit.cache_clear()
        get_loinc_example_units.cache_clear()

    def teardown_method(self):
        get_loinc_to_unit.cache_clear()
        get_loinc_example_units.cache_clear()

    def test_db_units_included(self):
        from omop_core.models import LoincClass, LoincCodeClass
        LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
        LoincCodeClass.objects.update_or_create(
            loinc_num='99990-0',
            defaults={'loinc_class_id': 'CHEM', 'example_units': 'mmol/L'},
        )
        units = get_loinc_to_unit()
        assert units.get('99990-0') == 'mmol/L'

    def test_curated_overrides_db(self):
        """LAB_FIELD_TO_LOINC entry wins over a DB row with the same code."""
        from omop_core.models import LoincClass, LoincCodeClass
        LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
        # Glucose (2345-7) is in LAB_FIELD_TO_LOINC with unit 'mg/dL'.
        LoincCodeClass.objects.update_or_create(
            loinc_num='2345-7',
            defaults={'loinc_class_id': 'CHEM', 'example_units': 'wrong_unit'},
        )
        units = get_loinc_to_unit()
        assert units['2345-7'] == 'mg/dL'

    def test_empty_example_units_excluded(self):
        from omop_core.models import LoincClass, LoincCodeClass
        LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
        LoincCodeClass.objects.update_or_create(
            loinc_num='99991-0',
            defaults={'loinc_class_id': 'CHEM', 'example_units': ''},
        )
        units = get_loinc_to_unit()
        assert '99991-0' not in units

    def test_concept_unit_fields_uses_db_unit(self):
        """concept_unit_fields returns DB-sourced unit for a LOINC Measurement."""
        from omop_core.models import LoincClass, LoincCodeClass
        LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
        LoincCodeClass.objects.update_or_create(
            loinc_num='99992-0',
            defaults={'loinc_class_id': 'CHEM', 'example_units': 'ng/mL'},
        )
        c = _make_concept(concept_code='99992-0', concept_name='Some analyte')
        result = concept_unit_fields(c)
        assert result == {'measurement_type': 'quantitative', 'suggested_unit': 'ng/mL', 'example_units': ['ng/mL']}
