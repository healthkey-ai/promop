"""Display names for medicines and procedures, including unmapped ones.

A mapped row shows its concept's name. An import can leave a row unmapped
(concept 0) with only its code as the source value, RxNorm "2762" or CPT
"85025", and a patient can't read a code. Then the row's source concept names
it, or the vocabularies we hold do, the way the code-mapping queue names its
codes (``describe_source_code``: Athena, then the source catalogue, then
UMLS). Read-only: nothing is mapped or recorded here, so the queue and its
curators stay the only way a row gets its concept.

The row does not say which code system its code is from. The patient's own
source codes do (``PatientSourceCode``, written by the importer beside the
rows), so a code is named in the system it arrived in, and from that code's
mapping row when no vocabulary we hold knows it: an Epic procedure id has
only the name its import sent (#2168). Only a code the patient's source codes
do not record is looked up in the vocabularies its table usually uses, and a
code they record in another system is never: CPT "23521" is not the Epic
procedure "23521".
"""
from __future__ import annotations

import re

from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from omop_core.services.source_descriptions import describe_source_code
from omop_core.services.source_vocabularies import canonical_source_vocabulary, hospital_vendor

# Where a bare code in each table usually comes from, when the record does not say.
DRUG_VOCABULARIES = ('RxNorm', 'RxNorm Extension')
VACCINE_VOCABULARIES = ('CVX',) + DRUG_VOCABULARIES
PROCEDURE_VOCABULARIES = ('CPT4', 'HCPCS', 'ICD10PCS')

# The systems a recorded code may name for each table, best first. A code the
# patient's source codes file under no table is trusted only in one of these.
DRUG_SYSTEMS = ('RxNorm', 'RxNorm Extension', 'CVX', 'NDC')
PROCEDURE_SYSTEMS = PROCEDURE_VOCABULARIES

# The standard systems importers send by OID or URI, as the vocabulary ids we name them in.
SYSTEM_VOCABULARIES = {
    'urn:oid:2.16.840.1.113883.6.12': 'CPT4',
    'http://www.ama-assn.org/go/cpt': 'CPT4',
    'urn:oid:2.16.840.1.113883.6.14': 'HCPCS',
    'urn:oid:2.16.840.1.113883.6.285': 'HCPCS',
    'urn:oid:2.16.840.1.113883.6.4': 'ICD10PCS',
    'urn:oid:2.16.840.1.113883.6.88': 'RxNorm',
    'http://www.nlm.nih.gov/research/umls/rxnorm': 'RxNorm',
    'urn:oid:2.16.840.1.113883.12.292': 'CVX',
    'http://hl7.org/fhir/sid/cvx': 'CVX',
    'urn:oid:2.16.840.1.113883.6.69': 'NDC',
    'http://hl7.org/fhir/sid/ndc': 'NDC',
}

# PatientSourceCode.omop_table spellings per clinical table.
DRUG_TABLES = ('drug_exposure',)
PROCEDURE_TABLES = ('procedure_occurrence', 'procedure')

# A code, not a name: letters and digits (and . -), at least one digit, no spaces.
CODE = re.compile(r'^(?=.*\d)[A-Za-z0-9.\-]{1,20}$')


def _vocabulary(system: str) -> str:
    system = (system or '').strip()
    return SYSTEM_VOCABULARIES.get(system) or canonical_source_vocabulary(system)


def _memoized(memo: dict | None, key, compute):
    if memo is not None and key in memo:
        return memo[key]
    value = compute()
    if memo is not None:
        memo[key] = value
    return value


def _vocabulary_name(vocabulary_id: str, code: str, memo: dict | None) -> str:
    """The code's name in one vocabulary we hold, or ``''``."""
    def compute():
        found = describe_source_code(vocabulary_id, code)
        return found if found and found != code else ''
    return _memoized(memo, ('vocabulary', vocabulary_id, code), compute)


def _named(vocabularies: tuple[str, ...], code: str, memo: dict | None) -> str:
    """The code's name in the first vocabulary that knows it."""
    for vocabulary_id in vocabularies:
        name = _vocabulary_name(vocabulary_id, code, memo)
        if name:
            return name
    return ''


def _mapping_name(systems: tuple[str, ...], code: str, memo: dict | None) -> str:
    """The name the code's mapping row holds, which is what its import sent."""
    def compute():
        names = (SourceCodeConceptMapping.objects
                 .filter(source_vocabulary_id__in=systems, source_code=code)
                 .exclude(source_code_description='').exclude(source_code_description=code)
                 .order_by('organization_id', 'id')
                 .values_list('source_code_description', flat=True))
        return next((name.strip() for name in names if name and name.strip()), '')
    return _memoized(memo, ('mapping', systems, code), compute)


def _recorded_systems(person_id, tables: tuple[str, ...], code: str, memo: dict | None) -> list[str]:
    """The systems the patient's source codes record this code in, for this table, best first.

    A code filed under the table counts in any system. A code filed under no
    table counts in a system the table uses, or in a hospital's own system:
    an Epic procedure id often arrives with no table, and dropping it would
    let a guessed CPT code name it. A code filed under another table (a lab
    component under ``measurement``) does not count. Order: the table's usual
    systems, then codes filed under the table, then hospital codes filed
    under none.
    """
    def load():
        rows = PatientSourceCode.objects.filter(person_id=person_id, omop_table__in=(*tables, ''))
        if memo is None:
            rows = rows.filter(source_value=code)
        recorded: dict[str, list[tuple[str, str]]] = {}
        for value, system, omop_table in rows.values_list('source_value', 'source_vocabulary_id', 'omop_table'):
            recorded.setdefault(value, []).append((system, omop_table))
        return recorded

    usual = DRUG_SYSTEMS if tables == DRUG_TABLES else PROCEDURE_SYSTEMS
    recorded = _memoized(memo, ('recorded', person_id, tables), load).get(code, [])
    rank = {vocabulary_id: index for index, vocabulary_id in enumerate(usual)}
    best: dict[str, tuple] = {}
    for system, omop_table in recorded:
        filed_here = omop_table in tables
        if not (filed_here or _vocabulary(system) in usual or hospital_vendor(system)):
            continue
        key = (rank.get(_vocabulary(system), len(rank)), 0 if filed_here else 1, system)
        best[system] = min(best.get(system, key), key)
    return sorted(best, key=best.get)


def _code_name(person_id, tables: tuple[str, ...], code: str, guesses: tuple[str, ...], memo: dict | None) -> str:
    systems = _recorded_systems(person_id, tables, code, memo) if person_id and tables else []
    if not systems:
        return _named(guesses, code, memo)
    for system in systems:
        vocabulary_id = _vocabulary(system)
        name = (_vocabulary_name(vocabulary_id, code, memo)
                or _mapping_name(tuple(dict.fromkeys((system, vocabulary_id))), code, memo))
        if name:
            return name
    return ''


def _concept_name(concept) -> str:
    return (getattr(concept, 'concept_name', '') or '').strip()


def display_name(concept_id, concept, source_concept_id, source_concept, source_value,
                 vocabularies: tuple[str, ...], memo: dict | None = None, *,
                 person_id=None, tables: tuple[str, ...] = ()) -> str:
    """The row's concept name; unmapped, its source concept's, or its code's; else the source value."""
    if concept_id and _concept_name(concept):
        return _concept_name(concept)
    value = (source_value or '').strip()
    if source_concept_id and _concept_name(source_concept) and _concept_name(source_concept) != value:
        return _concept_name(source_concept)
    if CODE.match(value):
        return _code_name(person_id, tables, value, vocabularies, memo) or value
    return value


def drug_name(row, memo: dict | None = None) -> str:
    """Kept on the row, so a listing names each row once."""
    if not hasattr(row, '_phr_name'):
        vaccine = (row.route_source_value or '').strip().upper() == 'VACCINE'
        row._phr_name = display_name(row.drug_concept_id, row.drug_concept, row.drug_source_concept_id,
                                     row.drug_source_concept, row.drug_source_value,
                                     VACCINE_VOCABULARIES if vaccine else DRUG_VOCABULARIES, memo,
                                     person_id=row.person_id, tables=DRUG_TABLES)
    return row._phr_name


def procedure_name(row, memo: dict | None = None) -> str:
    """Kept on the row, so a listing names each row once."""
    if not hasattr(row, '_phr_name'):
        row._phr_name = display_name(row.procedure_concept_id, row.procedure_concept,
                                     row.procedure_source_concept_id, row.procedure_source_concept,
                                     row.procedure_source_value, PROCEDURE_VOCABULARIES, memo,
                                     person_id=row.person_id, tables=PROCEDURE_TABLES)
    return row._phr_name
