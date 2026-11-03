"""ESR1 is a trial-required gene in the CB BC catalog (#5673).

EXACT compares gene SYMBOLS, not concept ids: the adapter lowercases PANEL's
`BRCA1` to `brca1` and the matcher intersects that with the trial's
`mutation_genes_required` codes. PROMOP materializes those symbols from the
gene LOINC; a gene with no LOINC row is invisible to both the writer and the
derivation, so the trial can never match a patient-entered finding.
"""
from omop_core.services.patient_record_service import _GENETIC_MUTATION_LOINCS


def test_esr1_has_a_gene_loinc_mapping():
    assert _GENETIC_MUTATION_LOINCS['104295-1'] == 'ESR1'


def test_every_bc_trial_gene_has_a_mapping():
    # The five genes the CB breast catalog requires; ESR1 was the missing one.
    required = {'BRCA1', 'BRCA2', 'ESR1', 'PIK3CA', 'TP53'}
    mapped = {gene for gene in _GENETIC_MUTATION_LOINCS.values() if gene}

    assert required <= mapped
