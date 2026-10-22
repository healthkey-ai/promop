from scripts.build_hospital_code_seed import merged_units


def test_merged_units_records_the_selected_inventory_snapshot():
    units = merged_units(
        [],
        [(
            'k/uL', 'k/uL', 'http://unitsofmeasure.org', '10*3/uL',
            'normalisation_table', 'valid',
            "normalisation table: 'k/uL' -> '10*3/uL'",
            12, 3, 12,
        )],
        unit_source='nikita-fhir_code_inventory_20261005b',
    )

    assert units == [{
        'display': 'k/uL',
        'code': 'k/uL',
        'system': 'http://unitsofmeasure.org',
        'normalized': '10*3/uL',
        'normalized_source': 'normalisation_table',
        'verdict': 'valid',
        'verdict_reason': "normalisation table: 'k/uL' -> '10*3/uL'",
        'count': 12,
        'patients': 3,
        'values': 12,
        'source': 'nikita-fhir_code_inventory_20261005b',
    }]
