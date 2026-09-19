"""Fill ``SourceCodeConceptMapping.source_code_description`` from the vocabularies.

Queue rows created by ``enqueue_unmapped_source_codes`` and the crossmap
importers carry only a code; a curator comparing "C90.20" against a proposed
destination has nothing to compare (#1464).  The name almost always exists
somewhere we already hold:

1. **Athena** — ``concept`` by ``(vocabulary_id, concept_code)``.  Covers
   SNOMED, LOINC, RxNorm, CVX, HemOnc and every other Athena vocabulary.
   ``VOCABULARY_ALIASES`` folds source ids that name the same vocabulary
   differently (the SNOMED OID); ``ATHENA_FALLBACKS`` tries a second Athena
   vocabulary when the first has no such code (HT-One's "ICD-10" codes are
   ICD-10-CM codes, so ``C85.90`` exists only under ICD10CM).
2. **UMLS** — ``umls_source_code`` by ``(root_source, code)`` for vocabularies
   Athena does not carry on a given deployment (MedDRA and CPT4 are licensed and
   absent from staging), or for codes Athena lacks.  The best atom wins:
   preferred, then a PT, then the longest name — CPT ships an abbreviated atom
   ("ECHO TRANSTHORC R-T 2D") beside the full descriptor.

Only empty descriptions are written, so a curator's own text is never replaced
and the function can run as often as needed.  It works on whichever model
classes it is given, so migration 0244 runs it on historical models and the
enqueue command on live ones.
"""
import logging

from django.db import connections

logger = logging.getLogger(__name__)

# Source vocabulary id as it arrives -> the Athena vocabulary to look up.
VOCABULARY_ALIASES = {
    'urn:oid:2.16.840.1.113883.6.96': 'SNOMED',
}

# Athena vocabulary to try when the source vocabulary has no such code.
ATHENA_FALLBACKS = {
    'ICD10': 'ICD10CM',
}

# UMLS root_source (SAB) for a source vocabulary. Mirrors
# ``omop_core.mapping.suggestions.VOCAB_TO_UMLS_ROOT`` and adds the two
# vocabularies Athena does not carry here.
UMLS_ROOT_FOR_VOCABULARY = {
    'SNOMED': 'SNOMEDCT_US',
    'urn:oid:2.16.840.1.113883.6.96': 'SNOMEDCT_US',
    'ICD10CM': 'ICD10CM',
    'ICD10': 'ICD10CM',
    'ICD10PCS': 'ICD10PCS',
    'LOINC': 'LNC',
    'RxNorm': 'RXNORM',
    'CPT4': 'CPT',
    'HCPCS': 'HCPCS',
    'NDC': 'NDC',
    'CVX': 'CVX',
    'ICD9CM': 'ICD9CM',
    'MeSH': 'MSH',
    'NDFRT': 'MED-RT',
    'MedDRA': 'MDR',
}

DESCRIPTION_MAX = 255


def backfill_source_descriptions(Mapping, Concept, UmlsSourceCode, *, using='default'):
    """Fill empty descriptions in place; return per-tier counts of rows written.

    ``Mapping``, ``Concept`` and ``UmlsSourceCode`` may be live or historical
    model classes — only their table names are used.
    """
    mapping = Mapping._meta.db_table
    concept = Concept._meta.db_table
    umls = UmlsSourceCode._meta.db_table
    counts = {}
    with connections[using].cursor() as cursor:
        # Tier 1a: same vocabulary id in Athena.
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(c.concept_name, %s)
            FROM "{concept}" AS c
            WHERE m.source_code_description = ''
              AND c.vocabulary_id = m.source_vocabulary_id
              AND c.concept_code = m.source_code
              AND c.concept_name <> ''
        ''', [DESCRIPTION_MAX])
        counts['athena'] = cursor.rowcount

        # Tier 1b: aliases and fallbacks, in one pass over the remaining gaps.
        pairs = list(VOCABULARY_ALIASES.items()) + list(ATHENA_FALLBACKS.items())
        values = ', '.join(['(%s, %s)'] * len(pairs))
        params = [DESCRIPTION_MAX] + [v for pair in pairs for v in pair]
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(c.concept_name, %s)
            FROM (VALUES {values}) AS alias(source_vocabulary_id, athena_vocabulary_id)
            JOIN "{concept}" AS c ON c.vocabulary_id = alias.athena_vocabulary_id
            WHERE m.source_code_description = ''
              AND m.source_vocabulary_id = alias.source_vocabulary_id
              AND c.concept_code = m.source_code
              AND c.concept_name <> ''
        ''', params)
        counts['athena_alias'] = cursor.rowcount

        # Tier 2: UMLS atoms for whatever is still empty. One best atom per
        # (root_source, code); also fills the UMLS name when that is blank.
        roots = list(UMLS_ROOT_FOR_VOCABULARY.items())
        values = ', '.join(['(%s, %s)'] * len(roots))
        params = [DESCRIPTION_MAX] + [v for pair in roots for v in pair]
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(best.name, %s),
                umls_source_name = CASE WHEN m.umls_source_name = '' THEN best.name
                                        ELSE m.umls_source_name END
            FROM (
                SELECT DISTINCT ON (gap.id) gap.id, u.name
                FROM "{mapping}" AS gap
                JOIN (VALUES {values}) AS sab(source_vocabulary_id, root_source)
                  ON sab.source_vocabulary_id = gap.source_vocabulary_id
                JOIN "{umls}" AS u
                  ON u.root_source = sab.root_source AND u.code = gap.source_code
                WHERE gap.source_code_description = '' AND u.name <> ''
                ORDER BY gap.id, u.is_preferred DESC, (u.term_type = 'PT') DESC,
                         length(u.name) DESC
            ) AS best
            WHERE m.id = best.id
        ''', params)
        counts['umls'] = cursor.rowcount

        cursor.execute(f'''
            SELECT count(*) FROM "{mapping}"
            WHERE source_code_description = '' AND source_vocabulary_id <> ''
        ''')
        counts['still_empty'] = cursor.fetchone()[0]
    logger.info(
        'source descriptions backfilled: athena=%s athena_alias=%s umls=%s still_empty=%s',
        counts['athena'], counts['athena_alias'], counts['umls'], counts['still_empty'],
    )
    return counts
