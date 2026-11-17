"""Shared provisional-data rules (#2434).

A row holds an approved fact ("real") when it carries no proposal confidence and
a mapped concept. The bulk planner and the FHIR sync collapse duplicates along
different code paths but must agree on this predicate, or one path can treat a
proposal as evidence while the other does not (#2543, #2581).
"""


def is_real_row(confidence, concept_id):
    """True when a stored row is an approved fact rather than a proposal."""
    return confidence is None and concept_id != 0


def provisional_shown(requested: bool = True) -> bool:
    """Whether a reader that asked for provisional rows gets them: only with the
    site switch on. With it off nothing provisional is read or shown to anyone."""
    from django.conf import settings
    return bool(requested) and settings.PROVISIONAL_DATA_ENABLED


def real_or_provisional(queryset, provisional: bool = False):
    """The rows a reader may show: real ones, plus provisional ones only when the
    reader asked and the switch is on.

    The default is real only, so every outbound or composite reader (exports,
    shares, therapy lines, imaging, genetics) stays approved-only; the patient's
    own PHR sections opt in.
    """
    return queryset if provisional_shown(provisional) else queryset.filter(confidence__isnull=True)


def without_shadowed(rows, concept_attr, date_attr=None):
    """Drop a provisional row that a real row already answers.

    Real beats provisional: if the record holds an approved row for the same
    concept, a proposal for it adds nothing and would only show twice. With
    *date_attr* the real row must also be on the same date, for an event that
    repeats (a second bone marrow biopsy is not the first). *rows* is a list of
    model instances; order is kept.
    """
    def key(row):
        return (getattr(row, concept_attr), getattr(row, date_attr) if date_attr else None)

    real = {key(r) for r in rows if r.confidence is None}
    return [r for r in rows if r.confidence is None or key(r) not in real]
