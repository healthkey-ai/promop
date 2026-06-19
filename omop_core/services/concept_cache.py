"""
Process-level in-memory cache for OMOP Concept lookups.

Concepts are static reference data that never changes at runtime.  Caching
them eliminates repeated DB round-trips when loading many patients that share
the same LOINC codes, HemOnc concept_ids, or condition concepts.

The cache is keyed by lookup type and lives for the lifetime of the worker
process.  It is shared across requests, which is safe because concepts are
never written to during a normal import run.

Usage:
    from omop_core.services.concept_cache import concept_by_id, concept_by_loinc

    c = concept_by_id(32856)            # EHR concept
    c = concept_by_loinc('718-7')       # Hemoglobin LOINC
    c = concept_by_vocab('SNOMED', '...')

    concept_cache_clear()               # for test isolation
"""
_cache: dict = {}


def concept_by_id(concept_id: int):
    """Return the Concept with this concept_id, or None.  Result is cached."""
    if concept_id not in _cache:
        from omop_core.models import Concept
        _cache[concept_id] = Concept.objects.filter(concept_id=concept_id).first()
    return _cache[concept_id]


def concept_by_loinc(loinc_code: str):
    """Return the Concept for this LOINC code, or None.  Result is cached."""
    return concept_by_vocab('LOINC', loinc_code)


def concept_by_vocab(vocabulary_id: str, concept_code: str):
    """Return the Concept for (vocabulary_id, concept_code), or None.  Result is cached."""
    key = (vocabulary_id, concept_code)
    if key not in _cache:
        from omop_core.models import Concept
        _cache[key] = Concept.objects.filter(
            vocabulary_id=vocabulary_id,
            concept_code=concept_code,
        ).first()
    return _cache[key]


def concept_by_name_ilike(name: str):
    """Return the first Concept whose concept_name icontains name, or None.  Result is cached."""
    key = ('_ilike', name)
    if key not in _cache:
        from omop_core.models import Concept
        _cache[key] = Concept.objects.filter(concept_name__icontains=name).first()
    return _cache[key]


def concept_cache_clear() -> None:
    """Flush the entire cache.  Call in test setUp/tearDown for isolation."""
    _cache.clear()
