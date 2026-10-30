"""Display names for medicines and procedures, including unmapped ones.

A mapped row shows its concept's name. Otherwise its source concept may name
it, or establish the vocabulary for a code lookup. A bare source value does
not establish a namespace: hospital-local codes can collide with RxNorm/CPT.
Without that evidence show what was recorded, never guess a different medicine.
Read-only: nothing is mapped or recorded here.
"""
from __future__ import annotations

import re

from omop_core.services.source_descriptions import describe_source_code

# Where a bare code in each table comes from.
DRUG_VOCABULARIES = ('RxNorm', 'RxNorm Extension')
PROCEDURE_VOCABULARIES = ('CPT4', 'HCPCS', 'ICD10PCS')

# A code, not a name: letters and digits (and . -), at least one digit, no spaces.
CODE = re.compile(r'^(?=.*\d)[A-Za-z0-9.\-]{1,20}$')


def _named(vocabularies: tuple[str, ...], code: str, memo: dict | None) -> str:
    """The code's name in the first vocabulary that knows it; ``memo`` keeps one lookup per code per request."""
    key = (vocabularies, code)
    if memo is not None and key in memo:
        return memo[key]
    name = ''
    for vocabulary_id in vocabularies:
        found = describe_source_code(vocabulary_id, code)
        if found and found != code:
            name = found
            break
    if memo is not None:
        memo[key] = name
    return name


def _concept_name(concept) -> str:
    return (getattr(concept, 'concept_name', '') or '').strip()


def display_name(concept_id, concept, source_concept_id, source_concept, source_value,
                 vocabularies: tuple[str, ...], memo: dict | None = None) -> str:
    """The row's concept name; unmapped, its source concept's, or its code's; else the source value."""
    if concept_id and _concept_name(concept):
        return _concept_name(concept)
    value = (source_value or '').strip()
    if source_concept_id and _concept_name(source_concept) and _concept_name(source_concept) != value:
        return _concept_name(source_concept)
    vocabulary = getattr(source_concept, 'vocabulary_id', None)
    code = getattr(source_concept, 'concept_code', None)
    if source_concept_id and vocabulary in vocabularies and code == value and CODE.match(value):
        return _named((vocabulary,), value, memo) or value
    return value


def drug_name(row, memo: dict | None = None) -> str:
    """Kept on the row, so a listing names each row once."""
    if not hasattr(row, '_phr_name'):
        row._phr_name = display_name(row.drug_concept_id, row.drug_concept, row.drug_source_concept_id,
                                     row.drug_source_concept, row.drug_source_value, DRUG_VOCABULARIES, memo)
    return row._phr_name


def procedure_name(row, memo: dict | None = None) -> str:
    """Kept on the row, so a listing names each row once."""
    if not hasattr(row, '_phr_name'):
        row._phr_name = display_name(row.procedure_concept_id, row.procedure_concept,
                                     row.procedure_source_concept_id, row.procedure_source_concept,
                                     row.procedure_source_value, PROCEDURE_VOCABULARIES, memo)
    return row._phr_name
