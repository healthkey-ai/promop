"""Maintain suggest_synonym_term, the domain-scoped synonym table Suggest reads.

See ``omop_core.models.SuggestSynonymTerm`` for why it exists. This module keeps
it in step with concept and concept_synonym.
"""

from django.db import connection, transaction

from omop_core.models import SUGGEST_DESTINATION_DOMAINS, SuggestSynonymTerm

# What makes a synonym retrievable: its concept is standard, active and in a
# destination domain. Kept literally in step with the filters in
# mapping.suggestions.lexical_candidates, which still applies them to whatever
# this table returns -- so a stale row can cost a slot in the shortlist but can
# never put an ineligible concept in front of the ranker.
#
# The eligibility clause is written out in both statements rather than
# interpolated: these are complete SQL literals with bound parameters only, which
# is what the SAST gate (bandit B608) requires and is easier to read in a log.
_INSERT_MISSING = """
    INSERT INTO suggest_synonym_term (concept_id, domain_id, term)
    SELECT DISTINCT s.concept_id, c.domain_id, UPPER(s.concept_synonym_name)
    FROM concept_synonym s
    JOIN concept c ON c.concept_id = s.concept_id
    WHERE c.standard_concept = 'S'
      AND c.invalid_reason IS NULL
      AND c.domain_id = ANY(%(domains)s)
    ON CONFLICT (concept_id, md5(term)) DO NOTHING
"""

# A term goes when its concept stopped being eligible, moved domain, or the
# synonym itself was removed by a vocabulary load.
_DELETE_STALE = """
    DELETE FROM suggest_synonym_term t
    WHERE NOT EXISTS (
        SELECT 1
        FROM concept c
        JOIN concept_synonym s ON s.concept_id = c.concept_id
        WHERE c.concept_id = t.concept_id
          AND c.domain_id = t.domain_id
          AND UPPER(s.concept_synonym_name) = t.term
          AND c.standard_concept = 'S'
          AND c.invalid_reason IS NULL
          AND c.domain_id = ANY(%(domains)s)
    )
"""


def refresh():
    """Bring the table in step with the vocabulary. Returns (inserted, deleted).

    Incremental and idempotent: rows that are already right are left alone, so
    a re-run after a small vocabulary change writes little, and readers are
    never locked out the way a TRUNCATE-and-reload would lock them.
    """
    params = {'domains': list(SUGGEST_DESTINATION_DOMAINS)}
    with transaction.atomic(), connection.cursor() as cursor:
        # Delete first. A concept that moved domain still has its row under the
        # old one, and the insert would collide with it on (concept, term) and
        # skip -- leaving the term in neither domain.
        cursor.execute(_DELETE_STALE, params)
        deleted = cursor.rowcount
        cursor.execute(_INSERT_MISSING, params)
        inserted = cursor.rowcount
    return inserted, deleted


def is_populated(domain_id):
    """Whether the table can answer for this domain.

    False for a domain Suggest does not scope to, and for a table nobody has
    built yet -- a test database, or an instance whose vocabulary was loaded
    before this table existed. The caller then searches concept_synonym
    directly, which is slow but complete. Never silently fewer results.
    """
    if domain_id not in SUGGEST_DESTINATION_DOMAINS:
        return False
    return SuggestSynonymTerm.objects.filter(domain_id=domain_id).exists()
