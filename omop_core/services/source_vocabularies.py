"""Source code systems a code can arrive in, organised by OMOP domain.

A curator picks the **domain** first -- is this a drug, a procedure, a
condition, an observation, a measurement -- because that is what they can tell
by looking at the source data, and it settles two things at once: which code
systems are plausible, and which OMOP table the fact lands in.

The catalogue is static rather than read from ``vocabulary``. Most of these are
systems we *receive* codes in without holding their concepts: an NDC on a
dispensing record, a dm+d code from a UK extract, an ICD-O-3 morphology from a
pathology report. Deriving the list from loaded vocabularies would offer only
the handful we happen to have loaded and block a curator from recording a
mapping they can already make correctly.

``vocabulary_id`` values use OHDSI spellings where one exists, so a mapping
recorded today lines up with the concepts a later vocabulary load brings in.
"""

# OMOP domain -> the clinical table its facts land in. Picking the domain
# settles the destination table; the curator never chooses it separately.
DOMAIN_TO_TABLE = {
    'Drug': 'drug_exposure',
    'Procedure': 'procedure',
    'Condition': 'condition',
    'Observation': 'observation',
    'Measurement': 'measurement',
}

DOMAIN_CHOICES = (
    ('Condition', 'Condition — diagnoses, problems, findings'),
    ('Drug', 'Drug — medications, vaccines, regimens'),
    ('Measurement', 'Measurement — labs, vitals, quantitative results'),
    ('Observation', 'Observation — assertions, history, social and clinical facts'),
    ('Procedure', 'Procedure — interventions, surgeries, administered care'),
)

# (vocabulary_id, display label). Ordered so the systems a curator meets most
# often sit at the top of each domain's list rather than alphabetically.
_CONDITION_SYSTEMS = (
    ('SNOMED', 'SNOMED CT — OMOP standard; FHIR problem lists'),
    ('ICD10CM', 'ICD-10-CM — US claims and EHR billing'),
    ('ICD10', 'ICD-10 — WHO international'),
    ('ICD10GM', 'ICD-10-GM — Germany'),
    ('ICD10CA', 'ICD-10-CA — Canada'),
    ('ICD11', 'ICD-11 — WHO, current revision'),
    ('ICD9CM', 'ICD-9-CM — legacy, still in historical extracts'),
    ('Read', 'Read v2 — UK legacy primary care'),
    ('CTV3', 'CTV3 (Read v3) — UK legacy primary care'),
    ('ICDO3', 'ICD-O-3 — cancer morphology and topography'),
    ('Orphanet', 'Orphanet — rare disease'),
    ('OMIM', 'OMIM — Mendelian inheritance'),
    ('HPO', 'HPO — human phenotype ontology'),
    ('MedDRA', 'MedDRA — adverse events, regulatory and trial data'),
    ('NCIt', 'NCIt — NCI thesaurus; oncology fallback'),
    ('ICPC', 'ICPC-2 — European primary care'),
    ('CIEL', 'CIEL — interface terminology'),
    ('Nebraska Lexicon', 'Nebraska Lexicon — interface terminology'),
    ('DRG', 'MS-DRG — claims grouper'),
    ('APR-DRG', 'APR-DRG — claims grouper'),
)

_PROCEDURE_SYSTEMS = (
    ('SNOMED', 'SNOMED CT procedures — OMOP standard'),
    ('CPT4', 'CPT-4 — US professional services'),
    ('HCPCS', 'HCPCS Level II — US supplies and services'),
    ('ICD10PCS', 'ICD-10-PCS — US inpatient procedures'),
    ('ICD9Proc', 'ICD-9-Proc — legacy inpatient procedures'),
    ('CDT', 'CDT — dental'),
    ('Revenue Code', 'UB-04 revenue codes — facility billing'),
    ('OPCS4', 'OPCS-4 — UK'),
    ('OPS', 'OPS — Germany'),
    ('CCAM', 'CCAM — France'),
    ('CCI', 'CCI — Canada'),
)

_DRUG_SYSTEMS = (
    ('RxNorm', 'RxNorm — OMOP standard for drugs'),
    ('RxNorm Extension', 'RxNorm Extension — OMOP, non-US drugs'),
    ('NDC', 'NDC — US package level; very common in dispensing data'),
    ('ATC', 'ATC — WHO classification, common outside the US'),
    ('dm+d', 'dm+d — UK dictionary of medicines and devices'),
    ('CVX', 'CVX — vaccines administered'),
    ('MVX', 'MVX — vaccine manufacturers'),
    ('HemOnc', 'HemOnc — regimens and lines of therapy'),
    ('Multum', 'Multum — commercial drug database'),
    ('FDB', 'First Databank — commercial drug database'),
    ('Medi-Span', 'Medi-Span — commercial drug database'),
    ('Gold Standard', 'Gold Standard — commercial drug database'),
    ('GPI', 'GPI — generic product identifier'),
    ('VANDF', 'VA National Drug File'),
    ('NDFRT', 'NDF-RT — VA reference terminology'),
    ('UNII', 'UNII — FDA unique ingredient identifier'),
    ('SPL', 'SPL — FDA structured product labeling'),
    ('AMT', 'AMT — Australian medicines terminology'),
    ('CCDD', 'CCDD — Canadian clinical drug data set'),
)

_MEASUREMENT_SYSTEMS = (
    ('LOINC', 'LOINC — OMOP standard for labs and measurements'),
    ('SNOMED', 'SNOMED CT — findings and qualitative results'),
    ('CPT4', 'CPT-4 — billed lab panels'),
    ('UCUM', 'UCUM — units of measure'),
    ('Nebraska Lexicon', 'Nebraska Lexicon — interface terminology'),
)

_OBSERVATION_SYSTEMS = (
    ('SNOMED', 'SNOMED CT — OMOP standard for observations'),
    ('LOINC', 'LOINC — survey and assessment items'),
    ('ICD10CM', 'ICD-10-CM — Z-codes and social history'),
    ('HCPCS', 'HCPCS — assessments and screenings'),
    ('NCIt', 'NCIt — NCI thesaurus'),
    ('PPI', 'PPI — participant-provided information (surveys)'),
)

SOURCE_SYSTEMS_BY_DOMAIN = {
    'Condition': _CONDITION_SYSTEMS,
    'Procedure': _PROCEDURE_SYSTEMS,
    'Drug': _DRUG_SYSTEMS,
    'Measurement': _MEASUREMENT_SYSTEMS,
    'Observation': _OBSERVATION_SYSTEMS,
}

# The blank option, offered under every domain and first in the list. Uncoded is
# the normal case for a parsed paper lab or a phrase from a note, and making it
# the leading choice says so rather than making a curator hunt for its absence.
NO_SOURCE_SYSTEM = {
    'vocabulary_id': '',
    'label': 'None — uncoded / free text (common for labs)',
}


def source_systems_for(domain_id):
    """Code systems plausible for one OMOP domain, blank option first."""
    systems = SOURCE_SYSTEMS_BY_DOMAIN.get(domain_id, ())
    return [NO_SOURCE_SYSTEM] + [
        {'vocabulary_id': vocab, 'label': label} for vocab, label in systems
    ]


def table_for_domain(domain_id):
    return DOMAIN_TO_TABLE.get(domain_id, '')


def domain_for_table(omop_table):
    for domain, table in DOMAIN_TO_TABLE.items():
        if table == omop_table:
            return domain
    return ''
