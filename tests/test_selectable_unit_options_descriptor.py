"""The selectable unit fields carry their choices (#2318).

The patient-facing descriptor marks the twelve `*_units` columns as
`kind: "selectable"`, but a client had no way to know which units those are:
the choices lived only in the admin/mapping descriptor. `FIELD_COMMON_UNITS`
already holds them, curated and ordered, so the writable descriptor exposes the
same list on the field a write lands in and on the `*_units` entry beside it.
"""
import pytest

from omop_core.services.mappings import FIELD_COMMON_UNITS
from omop_core.services.write_descriptor import (
    KIND_SELECTABLE,
    build_writable_field_descriptor,
)

pytestmark = pytest.mark.django_db

#: The twelve pairs, written down rather than discovered, so a field that
#: silently loses its `selectable` kind fails here instead of shrinking the
#: parametrisation to nothing.
PAIRS = (
    ('absolute_neutrophile_count_units', 'absolute_neutrophile_count'),
    ('albumin_level_units', 'albumin_level'),
    ('height_units', 'height'),
    ('hemoglobin_level_units', 'hemoglobin_level'),
    ('platelet_count_units', 'platelet_count'),
    ('red_blood_cell_count_units', 'red_blood_cell_count'),
    ('serum_bilirubin_level_direct_units', 'serum_bilirubin_level_direct'),
    ('serum_bilirubin_level_total_units', 'serum_bilirubin_level_total'),
    ('serum_calcium_level_units', 'serum_calcium_level'),
    ('serum_creatinine_level_units', 'serum_creatinine_level'),
    ('weight_units', 'weight'),
    ('white_blood_cell_count_units', 'white_blood_cell_count'),
)

EXPECTED = {
    field: [{'value': unit, 'label': unit} for unit in FIELD_COMMON_UNITS[qualifies]]
    for field, qualifies in PAIRS
}


def test_the_descriptor_still_has_twelve_selectable_unit_fields():
    descriptor = build_writable_field_descriptor()
    selectable = {
        field for field, entry in descriptor.items()
        if entry.get('kind') == KIND_SELECTABLE
    }

    assert selectable == {field for field, _ in PAIRS}


@pytest.mark.parametrize('field,qualifies', PAIRS)
def test_a_selectable_unit_field_lists_the_curated_choices(field, qualifies):
    entry = build_writable_field_descriptor()[field]

    assert entry['qualifies'] == qualifies
    assert entry['unit_options'] == EXPECTED[field]


@pytest.mark.parametrize('field,qualifies', PAIRS)
def test_the_field_it_qualifies_lists_the_same_choices(field, qualifies):
    descriptor = build_writable_field_descriptor()

    assert descriptor[qualifies]['unit_options'] == descriptor[field]['unit_options']


def test_a_field_without_a_unit_list_carries_none():
    assert 'unit_options' not in build_writable_field_descriptor()['hiv_status']
