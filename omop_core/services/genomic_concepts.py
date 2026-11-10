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

# Rows the previous record writers authored (#1749 until cancerbot #5671)
# carry SNOMED *codes* in the id columns. They mean what that writer meant, and
# only there: the same integers on an imported fact are real concepts ("Struck
# by", "Positive", "Equivocal"), never an origin or interpretation. Read them
# back for this writer's own rows so a stored finding keeps its meaning until
# the rows are repaired (audit_genomic_concept_ids reports them).
LEGACY_WRITER_SOURCE = 'cb-profile:genetic-mutation'
# The earlier PROMOP record writer (2feb62c0 until 9034cc0b) used the same ids
# and marked its rows in value_source_value instead.
LEGACY_VALUE_SOURCE_PREFIX = 'promop-genetic-mutation:'
LEGACY_ORIGIN_IDS = {255395001: 'germline', 255461003: 'somatic'}
LEGACY_INTERPRETATION_IDS = {30166007: 'pathogenic', 10828004: 'benign', 42425007: 'vus'}


def _legacy_writer_row(measurement):
    return (measurement.measurement_source_value == LEGACY_WRITER_SOURCE
            or (measurement.value_source_value or '').startswith(LEGACY_VALUE_SOURCE_PREFIX))


def origin_for(measurement):
    """The origin code a genomic Measurement records, or None."""
    concept_id = measurement.qualifier_concept_id
    if concept_id in MUTATION_ORIGIN_CONCEPTS:
        return MUTATION_ORIGIN_CONCEPTS[concept_id]
    if _legacy_writer_row(measurement):
        return LEGACY_ORIGIN_IDS.get(concept_id)
    return None


def interpretation_for(measurement):
    """The interpretation code a genomic Measurement records, or None."""
    concept_id = measurement.value_as_concept_id
    if concept_id in MUTATION_INTERPRETATION_CONCEPTS:
        return MUTATION_INTERPRETATION_CONCEPTS[concept_id]
    if _legacy_writer_row(measurement):
        return LEGACY_INTERPRETATION_IDS.get(concept_id)
    return None


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
