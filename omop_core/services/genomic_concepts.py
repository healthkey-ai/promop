"""The OMOP concepts for a genomic finding's origin and interpretation.

These are **concept ids**, not vocabulary codes. The previous maps stored
SNOMED codes in the id columns, and the codes did not even mean what the map
said: `30166007` is "Struck by", `10828004` is "Positive", `42425007` is
"Equivocal" and `255395001` is "Origins". A patient-entered pathogenic finding
was written and read as "Struck by" (cancerbot #5671).

The ids below are the LOINC answer concepts loaded with the vocabulary
(`LA6683-2`, `LA6684-0`, `LA6668-3`, `LA26332-9`, `LA6675-8`, `LA26334-5`,
`LA26333-7`, `LA11883-8`). Keep this as the single source for both the writer
and the derivation; a second copy is how the last mismatch survived.
"""

MUTATION_ORIGIN_CONCEPTS = {
    45880308: 'germline',   # LA6683-2
    45880309: 'somatic',    # LA6684-0
}

MUTATION_INTERPRETATION_CONCEPTS = {
    45884094: 'pathogenic',          # LA6668-3
    21498358: 'likely_pathogenic',   # LA26332-9
    45880925: 'benign',              # LA6675-8
    21498359: 'likely_benign',       # LA26334-5
    21498841: 'vus',                 # LA26333-7
    # `no_mutation_detected` is deliberately absent. The closest generic LOINC
    # answer is "Not detected", but CB's value is a NEGATIVE finding and this
    # writer has no finding-status field: mapping it here would record a
    # negative as `status="present"`. Pending clinical review in #5671; the
    # writer fails closed on it until then.
}

# The widget sends display labels ("Likely pathogenic", "VUS", "Uncertain"),
# while the record stores codes. Normalise both spellings to the stored code.
_INTERPRETATION_ALIASES = {
    'likely_pathogenic': 'likely_pathogenic',
    'likely_benign': 'likely_benign',
    'uncertain': 'vus',
    'uncertain_significance': 'vus',
    'variant_of_uncertain_significance': 'vus',
}


def normalise_interpretation(value):
    """The stored interpretation code for a submitted label/code, or None."""
    key = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    key = _INTERPRETATION_ALIASES.get(key, key)
    return key if key in MUTATION_INTERPRETATION_CONCEPTS.values() else None


def normalise_origin(value):
    key = str(value or '').strip().lower()
    return key if key in MUTATION_ORIGIN_CONCEPTS.values() else None
