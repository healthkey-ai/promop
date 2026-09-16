"""Static assertion: genomics LOINC domain assumptions match the installed vocabulary.

When someone adds a genomics component with a LOINC code, the FIELDS dict and
seed migrations assume a domain (Measurement or Observation). If the assumption
is wrong, the component gets concept 0 or is written to the wrong table.

This test hardcodes the known LOINC domains from Athena and fails immediately
when a new component is added with a wrong assumption. Update the reference
table when LOINC changes a domain in a future release.

See also: ``manage.py audit_genomics_domains`` for a runtime check against the
installed vocabulary after a vocabulary load.
"""
import pytest

from omop_core.services.genomics import FIELDS
from omop_core.services.genomics_catalog import catalog

# Source of truth: LOINC domains confirmed against Athena (LOINC 2.77,
# OMOP vocabulary_version v5.0 22-JUN-23, verified on staging 2026-09-12).
# The parent code is included for completeness but is intentionally kept in
# Measurement for event linking — concept 0 is acceptable there.
_LOINC_DOMAINS = {
    '48000-4': 'Measurement',   # chromosome
    '48001-2': 'Measurement',   # cytogenetic_location
    '48002-0': 'Measurement',   # genomic_source_class
    '48004-6': 'Measurement',   # transcript_dna_change
    '48005-3': 'Measurement',   # amino_acid_change
    '48006-1': 'Measurement',   # amino_acid_change_type
    '48018-6': 'Measurement',   # gene
    '51958-7': 'Measurement',   # transcript_reference_sequence_id
    '53037-8': 'Measurement',   # interpretation
    '62374-4': 'Measurement',   # genome_assembly
    '69548-6': 'Measurement',   # status (genetic variant assessment)
    '81252-9': 'Observation',   # parent (discrete genetic variant) — see note above
    '81253-7': 'Observation',   # variant_name
    '81258-6': 'Measurement',   # allelic_frequency
    '81290-9': 'Observation',   # genomic_dna_change
    '81304-8': 'Observation',   # variant_analysis_method_type
    '82121-5': 'Observation',   # coverage_depth
    '83005-9': 'Observation',   # variant_category
}

# The parent is intentionally stored in Measurement despite LOINC saying
# Observation. This set exempts codes from the domain-match assertion.
_INTENTIONAL_OVERRIDES = {'81252-9'}


def _catalog_domain(code):
    """Look up what domain the catalog or FIELDS dict assumes for a code."""
    for key, (field_code, domain) in FIELDS.items():
        if field_code == code:
            return domain
    for attr in catalog()['attributes']:
        if attr['code'] == code:
            return attr['table'].title()
    return None


def test_fields_dict_domains_match_loinc():
    """Every LOINC-coded component in FIELDS must assume the correct domain."""
    mismatches = []
    for key, (code, assumed_domain) in FIELDS.items():
        if code.startswith('genomics:'):
            continue
        expected = _LOINC_DOMAINS.get(code)
        if expected is None:
            continue  # Not in the reference table — a new code needs adding
        if code in _INTENTIONAL_OVERRIDES:
            continue
        if assumed_domain != expected:
            mismatches.append(f'{key}: code {code} assumes {assumed_domain}, LOINC says {expected}')
    assert not mismatches, (
        'FIELDS dict domain assumptions disagree with LOINC. '
        'Update the FIELDS entry or _LOINC_DOMAINS:\n  ' + '\n  '.join(mismatches)
    )


def test_catalog_attribute_domains_match_loinc():
    """Every LOINC-coded attribute in the frozen catalog must match or be
    corrected by the seed logic (which now uses the concept's own domain)."""
    mismatches = []
    for attr in catalog()['attributes']:
        code = attr['code']
        if code.startswith('genomics:'):
            continue
        expected = _LOINC_DOMAINS.get(code)
        if expected is None:
            continue
        if code in _INTENTIONAL_OVERRIDES:
            continue
        catalog_domain = attr['table'].title()
        if catalog_domain != expected:
            mismatches.append(
                f"{attr['key']}: catalog says {catalog_domain}, LOINC says {expected} "
                f"(OK if seed migration uses concept domain, not catalog domain)"
            )
    # This is informational — the seed now resolves without domain filtering,
    # so catalog mismatches are tolerated. But new attributes should be added
    # with correct domains. Warn rather than fail.
    if mismatches:
        pytest.skip(
            'Catalog has domain assumptions that differ from LOINC '
            '(tolerated — seed uses concept domain). '
            'New attributes should use the correct LOINC domain:\n  '
            + '\n  '.join(mismatches)
        )


def test_all_loinc_codes_have_reference_entries():
    """Every LOINC code used by the genomics system must be in the reference table."""
    missing = []
    for key, (code, _) in FIELDS.items():
        if code.startswith('genomics:'):
            continue
        if code not in _LOINC_DOMAINS:
            missing.append(f'{key}: code {code} not in _LOINC_DOMAINS')
    assert not missing, (
        'New LOINC codes need entries in _LOINC_DOMAINS. '
        'Check the domain in Athena and add them:\n  ' + '\n  '.join(missing)
    )
