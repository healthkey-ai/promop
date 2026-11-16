"""#5603: curator-answer sets reach mapped fields, not only unmapped ones.

`bone_lesions` is mapped to SNOMED 363817008, so it takes the mapped-field
descriptor path. That path never attached the `FieldChoice` options, and the
federated editor renders from the descriptor rather than a tab's local option
list — the picker came up without Yes/No/Unknown.
"""
import pytest

from omop_core.models import FieldChoice
from omop_core.services.write_descriptor import build_writable_field_descriptor

pytestmark = pytest.mark.django_db


def _seed_bone_lesions():
    for order, display in enumerate(('Yes', 'No', 'Unknown')):
        FieldChoice.objects.create(
            field_name='bone_lesions', display=display, sort_order=order)


def test_a_mapped_field_carries_its_curator_options():
    _seed_bone_lesions()

    entry = build_writable_field_descriptor()['bone_lesions']

    assert entry['writable'] is True
    assert [option['value'] for option in entry['options']] == ['Yes', 'No', 'Unknown']
    assert all('code' in option for option in entry['options'])


def test_no_curator_rows_means_no_invented_options():
    # The screenshot's stored "Absent" is a value, not a licensed answer: the
    # contract is the curated table, and an empty table must stay empty.
    entry = build_writable_field_descriptor()['bone_lesions']

    assert entry.get('options') is None
