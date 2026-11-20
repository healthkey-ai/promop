"""The therapy-line refusal catalog: what the client may be told, and only that."""
import pytest

from omop_core.services.therapy_errors import (
    FIELDS, MESSAGES, TherapyLineValidationError, refusal_payload,
)


def test_every_field_hint_names_a_catalogued_code():
    assert set(FIELDS) <= set(MESSAGES)


def test_an_unknown_code_cannot_be_raised():
    with pytest.raises(KeyError):
        TherapyLineValidationError('not_a_code')


def test_the_detail_stays_in_the_exception_string_only():
    exc = TherapyLineValidationError('unknown_drug', 424242)
    assert '424242' in str(exc)
    payload = refusal_payload(exc)
    assert payload == {'detail': MESSAGES['unknown_drug'], 'code': 'unknown_drug', 'field': 'drugs'}
    assert '424242' not in str(payload)


def test_a_code_the_catalog_does_not_know_has_no_payload():
    exc = TherapyLineValidationError('unknown_drug')
    exc.code = 'tampered'
    assert refusal_payload(exc) is None


def test_it_is_still_a_value_error_for_python_callers():
    assert isinstance(TherapyLineValidationError('future_line'), ValueError)
