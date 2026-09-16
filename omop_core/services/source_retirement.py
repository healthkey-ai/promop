"""Source-code retirement evidence, independent of mapping destinations."""
from collections import defaultdict

from django.db.models import Q
from django.db.models.functions import Trim, Upper
from django.utils import timezone

from omop_core.models import Concept
from omop_core.services.source_vocabularies import ICD10CM_MERGE, VOCABULARY_OID_ALIASES


def mapping_source_retirement(mappings):
    mappings = list(mappings)
    aliases = {**ICD10CM_MERGE, **VOCABULARY_OID_ALIASES}

    def canonical(vocabulary):
        return aliases.get(vocabulary, vocabulary)

    vocabularies = {m.source_vocabulary_id for m in mappings if m.source_vocabulary_id}
    canonical_vocabularies = {canonical(v) for v in vocabularies}
    vocabularies |= canonical_vocabularies | {v for v, c in aliases.items() if c in canonical_vocabularies}
    codes = {m.source_code.strip().upper() for m in mappings if m.source_code.strip()}
    linked_ids = {m.source_concept_id for m in mappings if m.source_concept_id}
    candidates = Concept.objects.annotate(normalized_code=Upper(Trim('concept_code'))).filter(
        Q(concept_id__in=linked_ids)
        | Q(vocabulary_id__in=vocabularies, normalized_code__in=codes)
    ).only('concept_id', 'concept_code', 'vocabulary_id', 'invalid_reason', 'valid_end_date', 'source')
    by_id, by_code = {}, defaultdict(list)
    for concept in candidates:
        by_id[concept.pk] = concept
        by_code[(canonical(concept.vocabulary_id), concept.normalized_code)].append(concept)

    result = {}
    today = timezone.localdate()
    for mapping in mappings:
        sources = {c.pk: c for c in by_code.get(
            (canonical(mapping.source_vocabulary_id), mapping.source_code.strip().upper()), []
        )}
        if mapping.source_concept_id in by_id:
            sources[mapping.source_concept_id] = by_id[mapping.source_concept_id]
        evidence = []
        for source in sorted(sources.values(), key=lambda c: c.pk):
            reasons = []
            if source.invalid_reason:
                reasons.append(f'invalid reason {source.invalid_reason}')
            if source.valid_end_date and source.valid_end_date < today:
                reasons.append(f'validity ended {source.valid_end_date.isoformat()}')
            if reasons:
                evidence.append(
                    f'{source.source or "Loaded vocabulary"} {source.vocabulary_id} '
                    f'concept {source.pk}: {"; ".join(reasons)}'
                )
        result[mapping.pk] = {
            'source_retired': bool(evidence) if sources else None,
            'source_retirement_evidence': evidence,
        }
    return result
