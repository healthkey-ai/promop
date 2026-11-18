"""CDISC Biomedical Concepts reached through the OMOP concept a code maps to.

A source code (a wearable metric, a lab code) has no BC of its own: it maps to
an OMOP concept, and ``ConceptBiomedicalConceptMapping`` links that concept to
a BC. So the BC follows the code mapping's destination, and a curator changing
either one changes the answer.
"""
from omop_core.models import ConceptBiomedicalConceptMapping, SourceCodeConceptMapping


def biomedical_concept_links(mapping: SourceCodeConceptMapping, statuses=('approved', 'proposed')):
    """The concept -> BC rows for a code mapping's destination, best status first.

    Empty when the mapping has no destination concept or no BC is linked to it.
    A rejected link is never returned unless asked for.
    """
    concept = mapping.target_concept
    if concept is None:
        return []
    links = ConceptBiomedicalConceptMapping.objects.filter(
        target_vocabulary_id=concept.vocabulary_id,
        target_concept_code=concept.concept_code,
        status__in=statuses,
    ).order_by('status', 'bc_id')
    return list(links)
