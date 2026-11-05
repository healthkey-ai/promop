"""No HIV / No Hepatitis B / No Hepatitis C are inverted views (#5663).

The trial vocabulary asks the negated question; the reference record stores the
positive status. The descriptor must carry both halves — which column a write
lands in, and that the value crosses a polarity boundary — because a client
given only one of them stores the opposite of the answer.
"""
import pytest

from omop_core.services.write_descriptor import (
    KIND_NEGATED,
    build_writable_field_descriptor,
)

pytestmark = pytest.mark.django_db

PAIRS = {
    'no_hiv_status': 'hiv_status',
    'no_hepatitis_b_status': 'hepatitis_b_status',
    'no_hepatitis_c_status': 'hepatitis_c_status',
}


@pytest.mark.parametrize('negated,positive', sorted(PAIRS.items()))
def test_a_negated_entry_names_the_positive_field_and_the_inversion(negated, positive):
    descriptor = build_writable_field_descriptor()
    entry = descriptor[negated]

    assert entry['kind'] == KIND_NEGATED
    assert entry['canonical'] == positive
    assert entry['inverted'] is True
    assert entry['value_kind'] == 'boolean'
    # Navigation metadata: the dialog opens on the canonical field. It is not
    # evidence the value was present — that is the raw-boundary provenance the
    # client reads separately (#5667).
    assert entry['inputs'] == [positive]
    # The value itself is not writable here: the canonical field is.
    assert entry['writable'] is False
    assert 'Inverted view of' in entry['reason']


@pytest.mark.parametrize('negated,positive', sorted(PAIRS.items()))
def test_the_field_the_write_lands_in_is_writable(negated, positive):
    descriptor = build_writable_field_descriptor()

    assert descriptor[positive]['writable'] is True, (
        f'{negated} points at {positive}, and a pointer to a refused write '
        'would leave the trial row with no edit path at all'
    )


@pytest.mark.parametrize('negated', sorted(PAIRS))
def test_the_removed_negative_is_not_a_stored_column(negated):
    """#2119 removed the columns; the descriptor describes the question, it does
    not bring the column back."""
    from omop_core.models import PatientRecord

    assert negated not in {field.name for field in PatientRecord._meta.concrete_fields}


@pytest.mark.parametrize('negated,positive', sorted(PAIRS.items()))
def test_the_canonical_entry_carries_the_words_the_dialog_asks(negated, positive):
    """#5667: the dedicated dialog shows the record's answers, not Yes/No.

    The labels ride on the CANONICAL entry, next to the field a write lands in;
    the negated entry is the question, not the answer.
    """
    from omop_core.services.boolean_answers import BOOLEAN_ANSWERS

    descriptor = build_writable_field_descriptor()
    positive_label, negative_label = BOOLEAN_ANSWERS[positive]

    assert descriptor[positive]['answer_labels'] == {
        'true': positive_label,
        'false': negative_label,
    }
