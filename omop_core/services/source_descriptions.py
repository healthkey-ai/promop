"""Name source codes on the code-mapping queue from the vocabularies we hold.

Queue rows created by ``enqueue_unmapped_source_codes`` and the crossmap
importers carry only a code, and FHIR sync stores the code itself when a
Coding has no display; a curator comparing "C90.20" against a proposed
destination then has nothing to compare (#1464).  The name almost always
exists somewhere already loaded:

1. **Athena** — ``concept`` by ``(vocabulary_id, concept_code)``.  Covers
   SNOMED, LOINC, RxNorm, CVX, HemOnc and every other Athena vocabulary.
   ``VOCABULARY_OID_ALIASES`` folds source ids that spell a vocabulary
   differently (the SNOMED OID); the inverse of ``ICD10CM_MERGE`` tries
   ICD-10-CM when an "ICD-10" code has no WHO entry (HT-One's ICD-10 codes are
   ICD-10-CM codes: ``C85.90`` exists only under ICD10CM).
2. **UMLS** — ``umls_source_code`` by ``(root_source, code)`` for vocabularies
   Athena does not carry on a deployment (MedDRA and CPT4 are licensed and
   absent from staging), or for codes Athena lacks.  The best atom per code
   wins: a PT first, then MRCONSO's preferred flag, then the longest name, then
   the name itself so ties resolve the same way on every deployment.  PT goes
   before the preferred flag because ISPREF is per string within a CUI, and on
   staging 2,918 CPT codes carry a preferred abbreviation ("ANES NTRORL EXC
   RTRPHRNG TUM") beside an unflagged full descriptor.

A description that is empty, or that merely repeats the code, counts as
missing.  Nothing else is ever written, so a curator's text survives and the
routine can run as often as needed.  ``backfill_source_descriptions`` is the
set-based form for the queue; ``describe_source_code`` names one code at the
moment ingest creates its row.
"""
import logging

from django.db import connections

from omop_core.services.source_vocabularies import (
    ICD10CM_MERGE, VOCABULARY_OID_ALIASES, VOCAB_TO_UMLS_ROOT,
)

logger = logging.getLogger(__name__)

DESCRIPTION_MAX = 255

# (source vocabulary as it arrives, Athena vocabulary to look it up in).
ATHENA_ALIASES = list(VOCABULARY_OID_ALIASES.items()) + [
    (merged_into, member) for member, merged_into in ICD10CM_MERGE.items()
]

# (source vocabulary as it arrives, UMLS root_source), OID spellings included.
UMLS_ROOTS = list(VOCAB_TO_UMLS_ROOT.items()) + [
    (oid, VOCAB_TO_UMLS_ROOT[canonical])
    for oid, canonical in VOCABULARY_OID_ALIASES.items()
    if canonical in VOCAB_TO_UMLS_ROOT
]

# A row is unnamed when its description is blank or just echoes the code.
_UNNAMED = "(m.source_code_description = '' OR m.source_code_description = m.source_code)"


def _values(pairs):
    return ', '.join(['(%s, %s)'] * len(pairs)), [v for pair in pairs for v in pair]


def backfill_source_descriptions(Mapping, Concept, UmlsSourceCode, *, using='default',
                                 min_id=None):
    """Name unnamed queue rows in place; return per-tier counts of rows written.

    ``Mapping``, ``Concept`` and ``UmlsSourceCode`` may be live or historical
    model classes — only their table names are used.  ``min_id`` restricts
    every pass to rows with a larger id, so a caller that has just inserted a
    batch can name only that batch.
    """
    mapping = Mapping._meta.db_table
    concept = Concept._meta.db_table
    umls = UmlsSourceCode._meta.db_table
    scope = ' AND m.id > %s' if min_id is not None else ''
    scope_params = [min_id] if min_id is not None else []
    counts = {}
    with connections[using].cursor() as cursor:
        # Tier 1a: same vocabulary id in Athena.
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(c.concept_name, %s)
            FROM "{concept}" AS c
            WHERE {_UNNAMED}{scope}
              AND c.vocabulary_id = m.source_vocabulary_id
              AND c.concept_code = m.source_code
              AND c.concept_name <> ''
        ''', [DESCRIPTION_MAX, *scope_params])
        counts['athena'] = cursor.rowcount

        # Tier 1b: aliases and fallbacks over what is still unnamed.
        values, params = _values(ATHENA_ALIASES)
        cursor.execute(f'''
            UPDATE "{mapping}" AS m
            SET source_code_description = LEFT(c.concept_name, %s)
            FROM (VALUES {values}) AS alias(source_vocabulary_id, athena_vocabulary_id)
            JOIN "{concept}" AS c ON c.vocabulary_id = alias.athena_vocabulary_id
            WHERE {_UNNAMED}{scope}
              AND m.source_vocabulary_id = alias.source_vocabulary_id
              AND c.concept_code = m.source_code
              AND c.concept_name <> ''
        ''', [DESCRIPTION_MAX, *params, *scope_params])
        counts['athena_alias'] = cursor.rowcount

        # Tier 2: the best UMLS atom per remaining row; fills the UMLS name too
        # when that is blank.
        values, params = _values(UMLS_ROOTS)
        gap_scope = scope.replace('m.id', 'gap.id')
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
                WHERE (gap.source_code_description = ''
                       OR gap.source_code_description = gap.source_code){gap_scope}
                  AND u.name <> ''
                ORDER BY gap.id, (u.term_type = 'PT') DESC, u.is_preferred DESC,
                         length(u.name) DESC, u.name
            ) AS best
            WHERE m.id = best.id
        ''', [DESCRIPTION_MAX, *params, *scope_params])
        counts['umls'] = cursor.rowcount

        cursor.execute(f'''
            SELECT count(*) FROM "{mapping}" AS m
            WHERE {_UNNAMED}{scope} AND m.source_vocabulary_id <> ''
        ''', scope_params)
        counts['still_unnamed'] = cursor.fetchone()[0]
    logger.info(
        'source descriptions: athena=%s athena_alias=%s umls=%s still_unnamed=%s%s',
        counts['athena'], counts['athena_alias'], counts['umls'], counts['still_unnamed'],
        f' (ids > {min_id})' if min_id is not None else '',
    )
    return counts


def describe_source_code(source_vocabulary_id, source_code):
    """The name for one code, or ``''`` when neither Athena nor UMLS knows it.

    Same tiers and the same atom preference as the bulk routine, for the
    moment ingest creates a queue row.
    """
    from omop_core.models import Concept, UmlsSourceCode

    if not source_vocabulary_id or not source_code:
        return ''
    vocabularies = [source_vocabulary_id] + [
        athena for arriving, athena in ATHENA_ALIASES if arriving == source_vocabulary_id
    ]
    for vocabulary_id in vocabularies:
        name = (
            Concept.objects
            .filter(vocabulary_id=vocabulary_id, concept_code=source_code)
            .exclude(concept_name='')
            .order_by('concept_id')
            .values_list('concept_name', flat=True)
            .first()
        )
        if name:
            return name[:DESCRIPTION_MAX]
    root = dict(UMLS_ROOTS).get(source_vocabulary_id)
    if not root:
        return ''
    atoms = (
        UmlsSourceCode.objects
        .filter(root_source=root, code=source_code)
        .exclude(name='')
        .values_list('term_type', 'is_preferred', 'name')
    )
    best = max(atoms, key=lambda a: (a[0] == 'PT', a[1], len(a[2]), a[2]), default=None)
    return best[2][:DESCRIPTION_MAX] if best else ''
