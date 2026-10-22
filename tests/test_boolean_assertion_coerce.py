"""#728 — boolean assertion writes: reject/coerce non-boolean input so it round-trips.

Tests the write-side coercion in field_write_service.coerce_assertion_value and
the serializer integration that invokes it on Measurement / Observation writes.
"""

import pytest
from rest_framework import serializers as drf_serializers

from omop_core.services.field_write_service import (
    _BOOLEAN_ASSERTION_CODES,
    coerce_assertion_value,
)


# ---------------------------------------------------------------------------
# Unit tests for coerce_assertion_value
# ---------------------------------------------------------------------------

class TestCoerceAssertionValue:
    """Pure-function tests — no database required."""

    # A known boolean assertion code (contraceptive_use).
    CODE = '8659-8'

    # ------------------------------------------------------------------
    # Valid truthy values
    # ------------------------------------------------------------------

    @pytest.mark.parametrize('raw_string', ['true', 'True', 'TRUE', 'yes', 'Yes', 'YES', '1'])
    def test_truthy_strings_coerce_to_true(self, raw_string):
        num, string, error = coerce_assertion_value(self.CODE, None, raw_string)
        assert error is None
        assert string == 'True'
        assert num == 1.0

    @pytest.mark.parametrize('raw_number', [1, 1.0])
    def test_truthy_numbers_coerce_to_true(self, raw_number):
        num, string, error = coerce_assertion_value(self.CODE, raw_number, None)
        assert error is None
        assert string == 'True'
        assert num == 1.0

    def test_python_bool_true_coerces(self):
        num, string, error = coerce_assertion_value(self.CODE, None, True)
        assert error is None
        assert string == 'True'
        assert num == 1.0

    # ------------------------------------------------------------------
    # Valid falsy values
    # ------------------------------------------------------------------

    @pytest.mark.parametrize('raw_string', ['false', 'False', 'FALSE', 'no', 'No', 'NO', '0'])
    def test_falsy_strings_coerce_to_false(self, raw_string):
        num, string, error = coerce_assertion_value(self.CODE, None, raw_string)
        assert error is None
        assert string == 'False'
        assert num == 0.0

    @pytest.mark.parametrize('raw_number', [0, 0.0])
    def test_falsy_numbers_coerce_to_false(self, raw_number):
        num, string, error = coerce_assertion_value(self.CODE, raw_number, None)
        assert error is None
        assert string == 'False'
        assert num == 0.0

    def test_python_bool_false_coerces(self):
        num, string, error = coerce_assertion_value(self.CODE, None, False)
        assert error is None
        assert string == 'False'
        assert num == 0.0

    # ------------------------------------------------------------------
    # Invalid values are rejected
    # ------------------------------------------------------------------

    @pytest.mark.parametrize('bad_value', ['maybe', 'banana', 'unknown', 'N/A', ''])
    def test_invalid_strings_rejected(self, bad_value):
        # Empty string with no number means "no value" -- not rejected.
        if bad_value == '':
            num, string, error = coerce_assertion_value(self.CODE, None, bad_value)
            assert error is None
            return
        num, string, error = coerce_assertion_value(self.CODE, None, bad_value)
        assert error is not None
        assert 'boolean' in error.lower()

    @pytest.mark.parametrize('bad_number', [2, -1, 3.5, 42])
    def test_invalid_numbers_rejected(self, bad_number):
        num, string, error = coerce_assertion_value(self.CODE, bad_number, None)
        assert error is not None
        assert 'boolean' in error.lower()

    # ------------------------------------------------------------------
    # Non-assertion codes pass through unchanged
    # ------------------------------------------------------------------

    def test_non_assertion_code_passes_through(self):
        num, string, error = coerce_assertion_value('718-7', 12.5, 'some text')
        assert error is None
        assert num == 12.5
        assert string == 'some text'

    def test_none_source_value_passes_through(self):
        num, string, error = coerce_assertion_value(None, 99, 'anything')
        assert error is None
        assert num == 99
        assert string == 'anything'

    # ------------------------------------------------------------------
    # No value provided is fine (assertion with no answer = unknown)
    # ------------------------------------------------------------------

    def test_no_value_passes_through(self):
        num, string, error = coerce_assertion_value(self.CODE, None, None)
        assert error is None
        assert num is None
        assert string is None

    # ------------------------------------------------------------------
    # value_as_string takes precedence over value_as_number
    # ------------------------------------------------------------------

    def test_string_takes_precedence_over_number(self):
        num, string, error = coerce_assertion_value(self.CODE, 1, 'false')
        assert error is None
        assert string == 'False'
        assert num == 0.0

    # ------------------------------------------------------------------
    # All boolean assertion codes are covered
    # ------------------------------------------------------------------

    def test_boolean_assertion_codes_are_populated(self):
        """Ensure the lookup is not accidentally empty."""
        assert len(_BOOLEAN_ASSERTION_CODES) >= 5

    @pytest.mark.parametrize('code', list(_BOOLEAN_ASSERTION_CODES))
    def test_every_boolean_code_coerces(self, code):
        num, string, error = coerce_assertion_value(code, None, 'yes')
        assert error is None
        assert string == 'True'

    # ------------------------------------------------------------------
    # inverse_boolean codes also coerce (the inversion is on read, not write)
    # ------------------------------------------------------------------

    def test_inverse_boolean_code_coerces_same_as_boolean(self):
        # 75618-3 is inverse_boolean (no_mental_health_disorder_status)
        num, string, error = coerce_assertion_value('75618-3', None, 'yes')
        assert error is None
        assert string == 'True'
        assert num == 1.0


# ---------------------------------------------------------------------------
# Serializer integration tests (need Django but not necessarily a full DB)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSerializerBooleanCoercion:
    """Verify that MeasurementSerializer and ObservationSerializer reject
    invalid boolean assertion values."""

    def test_measurement_serializer_rejects_invalid_boolean(self):
        from patient_portal.api.serializers import MeasurementSerializer
        from tests.factories import ConceptFactory, PersonFactory

        person = PersonFactory()
        concept = ConceptFactory(concept_code='8659-8', concept_name='Contraceptive use')
        type_concept = ConceptFactory(concept_code='32856', concept_name='Lab')

        data = {
            'person': person.person_id,
            'measurement_concept': concept.concept_id,
            'measurement_date': '2026-01-15',
            'measurement_datetime': '2026-01-15T10:00:00Z',
            'measurement_type_concept': type_concept.concept_id,
            'measurement_source_value': '8659-8',
            'value_as_string': 'maybe',
        }
        serializer = MeasurementSerializer(data=data)
        assert not serializer.is_valid()
        assert 'value_as_string' in serializer.errors

    def test_measurement_serializer_coerces_valid_boolean(self):
        from patient_portal.api.serializers import MeasurementSerializer
        from tests.factories import ConceptFactory, PersonFactory

        person = PersonFactory()
        concept = ConceptFactory(concept_code='8659-8', concept_name='Contraceptive use')
        type_concept = ConceptFactory(concept_code='32856', concept_name='Lab')

        data = {
            'person': person.person_id,
            'measurement_concept': concept.concept_id,
            'measurement_date': '2026-01-15',
            'measurement_datetime': '2026-01-15T10:00:00Z',
            'measurement_type_concept': type_concept.concept_id,
            'measurement_source_value': '8659-8',
            'value_as_string': 'yes',
        }
        serializer = MeasurementSerializer(data=data)
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data['value_as_string'] == 'True'
        assert serializer.validated_data['value_as_number'] == 1.0

    def test_observation_serializer_rejects_invalid_boolean(self):
        from patient_portal.api.serializers import ObservationSerializer
        from tests.factories import ConceptFactory, PersonFactory

        person = PersonFactory()
        concept = ConceptFactory(concept_code='75985-6', concept_name='Ability to consent')
        type_concept = ConceptFactory(concept_code='32817', concept_name='EHR')

        data = {
            'person': person.person_id,
            'observation_concept': concept.concept_id,
            'observation_date': '2026-01-15',
            'observation_datetime': '2026-01-15T10:00:00Z',
            'observation_type_concept': type_concept.concept_id,
            'observation_source_value': '75985-6',
            'value_as_string': 'banana',
        }
        serializer = ObservationSerializer(data=data)
        assert not serializer.is_valid()
        assert 'value_as_string' in serializer.errors

    def test_observation_serializer_coerces_valid_boolean(self):
        from patient_portal.api.serializers import ObservationSerializer
        from tests.factories import ConceptFactory, PersonFactory

        person = PersonFactory()
        concept = ConceptFactory(concept_code='75985-6', concept_name='Ability to consent')
        type_concept = ConceptFactory(concept_code='32817', concept_name='EHR')

        data = {
            'person': person.person_id,
            'observation_concept': concept.concept_id,
            'observation_date': '2026-01-15',
            'observation_datetime': '2026-01-15T10:00:00Z',
            'observation_type_concept': type_concept.concept_id,
            'observation_source_value': '75985-6',
            'value_as_string': 'no',
        }
        serializer = ObservationSerializer(data=data)
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data['value_as_string'] == 'False'
        assert serializer.validated_data['value_as_number'] == 0.0

    def test_non_assertion_measurement_passes_through(self):
        """A normal lab measurement should not be affected by the coercion."""
        from patient_portal.api.serializers import MeasurementSerializer
        from tests.factories import ConceptFactory, PersonFactory

        person = PersonFactory()
        concept = ConceptFactory(concept_code='718-7', concept_name='Hemoglobin')
        type_concept = ConceptFactory(concept_code='32856', concept_name='Lab')

        data = {
            'person': person.person_id,
            'measurement_concept': concept.concept_id,
            'measurement_date': '2026-01-15',
            'measurement_datetime': '2026-01-15T10:00:00Z',
            'measurement_type_concept': type_concept.concept_id,
            'measurement_source_value': '718-7',
            'value_as_number': 12.5,
            'value_as_string': 'some text',
        }
        serializer = MeasurementSerializer(data=data)
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data['value_as_number'] == 12.5
        assert serializer.validated_data['value_as_string'] == 'some text'
