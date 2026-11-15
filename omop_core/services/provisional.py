"""Shared provisional-data rules (#2434).

A row holds an approved fact ("real") when it carries no proposal confidence and
a mapped concept. The bulk planner and the FHIR sync collapse duplicates along
different code paths but must agree on this predicate, or one path can treat a
proposal as evidence while the other does not (#2543, #2581).
"""


def is_real_row(confidence, concept_id):
    """True when a stored row is an approved fact rather than a proposal."""
    return confidence is None and concept_id != 0
