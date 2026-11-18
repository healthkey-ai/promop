"""CDISC Biomedical Concepts a source code reaches through its OMOP concept.

A BC is itself a source code: a ``SourceCodeConceptMapping`` row in the
``CDISC-BC`` vocabulary, mapped to one OMOP concept. A wearable code has no BC
of its own; it reaches the BCs that share its destination. So changing either
mapping changes the answer, and nothing is stored about the pair.
"""
from omop_core.models import SourceCodeConceptMapping
from omop_core.services.source_vocabularies import CDISC_BC_VOCABULARY


def biomedical_concept_mappings(mapping: SourceCodeConceptMapping, statuses=('approved', 'proposed')):
    """The global BC mappings that point at *mapping*'s destination concept.

    Approved before proposed. Empty when the mapping has no destination, or is
    a BC row itself. A rejected BC mapping is returned only when asked for.
    """
    if not mapping.target_concept_id or mapping.source_vocabulary_id == CDISC_BC_VOCABULARY:
        return []
    return list(SourceCodeConceptMapping.objects.filter(
        organization__isnull=True, source_vocabulary_id=CDISC_BC_VOCABULARY,
        target_concept_id=mapping.target_concept_id, status__in=statuses,
    ).order_by('status', 'source_code'))
