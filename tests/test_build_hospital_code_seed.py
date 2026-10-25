from scripts.build_hospital_code_seed import merged_units

NIKITA = (
    'k/uL', 'k/uL', 'http://unitsofmeasure.org', '10*3/uL',
    'normalisation_table', 'valid',
    "normalisation table: 'k/uL' -> '10*3/uL'",
    12, 3, 12,
)
SOURCE = 'nikita-fhir_code_inventory_20261005b'


def test_a_unit_seen_on_the_code_takes_the_per_code_counts_from_the_v2_extract():
    """Nikita's 12/3/12 is the label's count, repeated for every code with the
    label; the v2 extract's 2/1/2 is this code's (#2041)."""
    units = merged_units(
        [{'display': 'k/uL', 'code': 'k/uL', 'count': 2, 'patients': 1, 'values': 2,
          'distribution': {'p50': 7.1}, 'source': 'alex-unmapped-source-codes-v2'}],
        [NIKITA], unit_source=SOURCE,
    )

    assert units == [{
        'display': 'k/uL',
        'code': 'k/uL',
        'system': 'http://unitsofmeasure.org',
        'normalized': '10*3/uL',
        'normalized_source': 'normalisation_table',
        'verdict': 'valid',
        'verdict_reason': "normalisation table: 'k/uL' -> '10*3/uL'",
        'count': 2,
        'patients': 1,
        'values': 2,
        'distribution': {'p50': 7.1},
        'distribution_source': 'alex-unmapped-source-codes-v2',
        'source': SOURCE,
    }]


def test_a_unit_known_only_to_the_label_is_listed_without_a_count():
    units = merged_units([], [NIKITA], unit_source=SOURCE)

    assert units == [{
        'display': 'k/uL',
        'code': 'k/uL',
        'system': 'http://unitsofmeasure.org',
        'normalized': '10*3/uL',
        'normalized_source': 'normalisation_table',
        'verdict': 'valid',
        'verdict_reason': "normalisation table: 'k/uL' -> '10*3/uL'",
        'count': 0,
        'count_scope': 'label',
        'label_count': 12,
        'label_patients': 3,
        'label_values': 12,
        'source': SOURCE,
    }]


def test_a_label_level_unit_is_not_evidence_for_the_code():
    from collections import Counter
    from types import SimpleNamespace

    from omop_core.services.mapping_unit_consistency import _imported_unit_counts

    mapping = SimpleNamespace(pk=1, source_unit_evidence=merged_units(
        [{'display': 'g/dL', 'code': 'g/dL', 'count': 1, 'source': 'alex-unmapped-source-codes-v2'}],
        [('g/dL', 'g/dL', '', 'g/dL', 'exact', 'valid', '', 10, 1, 10),
         ('%', '%', '', '%', 'exact', 'valid', '', 1, 1, 1)],
        unit_source=SOURCE,
    ))
    assert _imported_unit_counts([mapping]) == {1: Counter({'g/dL': 1})}
