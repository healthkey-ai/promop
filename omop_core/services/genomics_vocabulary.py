"""Conservative resolution shared by genomic writes and vocabulary audits."""
from dataclasses import dataclass

from omop_core.models import Concept, ConceptRelationship


@dataclass(frozen=True)
class Resolution:
    source: Concept | None = None
    standard: Concept | None = None
    problem: str = ''


def resolve_loinc(code):
    sources = list(Concept.objects.filter(
        vocabulary_id='LOINC', concept_code=code, invalid_reason__isnull=True,
    )[:2])
    if len(sources) != 1:
        return Resolution(problem='missing source' if not sources else 'ambiguous source')
    source = sources[0]
    if source.standard_concept == 'S':
        targets = [source]
    else:
        targets = list(Concept.objects.filter(
            pk__in=ConceptRelationship.objects.filter(
                concept_1=source, relationship_id='Maps to', invalid_reason__isnull=True,
            ).values('concept_2_id'),
            standard_concept='S', invalid_reason__isnull=True,
        )[:2])
    if len(targets) != 1:
        return Resolution(source=source, problem='missing standard target' if not targets else 'ambiguous Maps to')
    standard = targets[0]
    if standard.domain_id not in ('Measurement', 'Observation'):
        return Resolution(source=source, problem=f'unsupported standard domain {standard.domain_id}')
    return Resolution(source, standard)
