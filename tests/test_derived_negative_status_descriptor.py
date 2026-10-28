"""#2106: the HIV/hepatitis negatives are derived, so they are not writable.

`no_hiv_status`, `no_hepatitis_b_status` and `no_hepatitis_c_status` are read back
from their paired status fact by `_latest_result` during derivation. Before this
they were advertised as direct writes: the endpoint answered `applied`, then the
next refresh replaced the value with the paired field's. The descriptor now
reports them computed with the positive field as the input, which is what a client
needs to render the negative read-only and point the edit at the source.
"""
import pytest

from omop_core.services.write_descriptor import (
    KIND_COMPUTED,
    build_writable_field_descriptor,
)

pytestmark = pytest.mark.django_db

PAIRS = {
    'no_hiv_status': 'hiv_status',
    'no_hepatitis_b_status': 'hepatitis_b_status',
    'no_hepatitis_c_status': 'hepatitis_c_status',
}


@pytest.mark.parametrize('negative, positive', sorted(PAIRS.items()))
def test_negative_status_is_computed_and_names_its_source(negative, positive):
    entry = build_writable_field_descriptor()[negative]

    assert entry['kind'] == KIND_COMPUTED
    assert entry['writable'] is False
    assert entry['inputs'] == [positive]


@pytest.mark.parametrize('positive', sorted(PAIRS.values()))
def test_the_paired_positive_status_stays_writable(positive):
    entry = build_writable_field_descriptor()[positive]

    assert entry['writable'] is True, (
        f'{positive} is the field the derivation reads; refusing it too would leave '
        'no way to answer the question at all'
    )
