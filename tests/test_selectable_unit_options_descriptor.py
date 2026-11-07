"""The selectable unit fields carry their choices (#2318).

The patient-facing descriptor marks the twelve `*_units` columns as
`kind: "selectable"`, and the writable descriptor advertises their choices on
the field a write lands in and on the `*_units` entry beside it.

The option's `value` must be the code the column stores, not the display string
a curator writes: the write side validates against the model's `TextChoices`, so
advertising `mg/dL` for a column whose code is `MG/DL` (and `lbs` for `lb`) hands
the client a choice that fails validation. `label` stays the curated display.

One pair is a known gap: the RBC column only stores `CELLS/UL` and `CELLS/L`
while the curated table advertises millions/µL, which needs new enum codes and a
migration. The strict xfail below records that instead of hiding it.
"""
import pytest

from omop_core.models import PatientRecord
from omop_core.services.blood_count_source_pairs import UNIT_OPTIONS
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
    # Count source pairs (#5700) already carry stored codes.
    'absolute_neutrophile_count_units': UNIT_OPTIONS,
    'platelet_count_units': UNIT_OPTIONS,
    # Hemoglobin's slice (#2318) offers the two convertible scales; mmol/L waits
    # for a reviewed factor.
    'hemoglobin_level_units': [
        {'value': 'G/DL', 'label': 'g/dL'},
        {'value': 'G/L', 'label': 'g/L'},
    ],
    'white_blood_cell_count_units': [
        {'value': '10*3/uL', 'label': '10*3/uL'},
        {'value': '10*9/L', 'label': '10*9/L'},
    ],
    # The known gap: valid display strings, but not codes this column stores.
    'red_blood_cell_count_units': [
        {'value': '10*6/uL', 'label': '10*6/uL'},
        {'value': '10*12/L', 'label': '10*12/L'},
    ],
    'weight_units': [
        {'value': 'kg', 'label': 'kg'},
        {'value': 'lb', 'label': 'lb'},
    ],
    'height_units': [
        {'value': 'cm', 'label': 'cm'},
        {'value': 'in', 'label': 'in'},
    ],
    'albumin_level_units': [
        {'value': 'G/DL', 'label': 'g/dL'},
        {'value': 'G/L', 'label': 'g/L'},
    ],
    'serum_calcium_level_units': [
        {'value': 'MG/DL', 'label': 'mg/dL'},
        {'value': 'MMOL/L', 'label': 'mmol/L'},
    ],
    'serum_creatinine_level_units': [
        {'value': 'MG/DL', 'label': 'mg/dL'},
        {'value': 'UMOL/L', 'label': 'umol/L'},
    ],
    'serum_bilirubin_level_total_units': [
        {'value': 'MG/DL', 'label': 'mg/dL'},
        {'value': 'UMOL/L', 'label': 'umol/L'},
    ],
    'serum_bilirubin_level_direct_units': [
        {'value': 'MG/DL', 'label': 'mg/dL'},
        {'value': 'UMOL/L', 'label': 'umol/L'},
    ],
}

KNOWN_GAP = 'red_blood_cell_count_units'


def stored_codes(field: str) -> set[str]:
    return {code for code, _label in PatientRecord._meta.get_field(field).choices}


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


@pytest.mark.parametrize('field,qualifies', [
    pair for pair in PAIRS if pair[0] != KNOWN_GAP
])
def test_every_advertised_value_is_a_code_the_column_stores(field, qualifies):
    """A picker value the write side refuses is worse than no picker (#2318)."""
    options = build_writable_field_descriptor()[field]['unit_options']

    assert {option['value'] for option in options} <= stored_codes(field)
    # And the label is the display, so a stored code is never shown raw where a
    # curated notation exists.
    assert all(option['label'] for option in options)


@pytest.mark.xfail(
    strict=True,
    reason=(
        'RBC advertises millions/uL but the column only stores CELLS/UL and '
        'CELLS/L; adding the scales needs enum codes and a migration (#2318 '
        'follow-up). Remove this marker when that lands.'
    ),
)
def test_red_blood_cell_units_are_codes_the_column_stores():
    options = build_writable_field_descriptor()[KNOWN_GAP]['unit_options']

    assert {option['value'] for option in options} <= stored_codes(KNOWN_GAP)


def test_a_field_without_a_unit_list_carries_none():
    assert 'unit_options' not in build_writable_field_descriptor()['hiv_status']
