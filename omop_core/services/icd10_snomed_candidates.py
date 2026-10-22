"""ICD-10 → SNOMED candidate concept lookup.

Loads the one-to-many ICD-10→SNOMED mapping file (copied from HealthTree-One)
and resolves SNOMED concept IDs against the local Concept table so curators
can pick the best standard concept for a field mapping.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "icd10_snomed_mappings.json"


@lru_cache(maxsize=1)
def _load_raw_mappings() -> dict[str, list[str]]:
    """Load the ICD-10→SNOMED mapping file into memory (cached)."""
    if not _DATA_FILE.exists():
        logger.warning("ICD-10→SNOMED mapping file not found: %s", _DATA_FILE)
        return {}
    with open(_DATA_FILE) as f:
        return json.load(f)


def reload_mappings() -> int:
    """Clear the cache and reload.  Returns the number of ICD-10 codes loaded."""
    _load_raw_mappings.cache_clear()
    data = _load_raw_mappings()
    return len(data)


def get_snomed_ids_for_icd10(icd10_code: str) -> list[str]:
    """Return raw SNOMED concept IDs for a given ICD-10 code."""
    data = _load_raw_mappings()
    return data.get(icd10_code.strip(), [])


def candidate_count_for_icd10(icd10_code: str) -> int:
    """Return the number of SNOMED candidates for an ICD-10 code, or 0."""
    return len(get_snomed_ids_for_icd10(icd10_code))


def resolve_candidates(icd10_code: str) -> list[dict]:
    """Look up SNOMED candidates for an ICD-10 code, resolved against the
    local Concept table.  Returns a list of serialised concept dicts for
    every SNOMED ID that exists in the DB, ordered by concept_name.
    """
    from omop_core.models import Concept

    snomed_ids = get_snomed_ids_for_icd10(icd10_code)
    if not snomed_ids:
        return []

    # Look up by concept_code in SNOMED vocabulary.
    concepts = (
        Concept.objects
        .filter(concept_code__in=snomed_ids, vocabulary_id="SNOMED")
        .order_by("concept_name")
    )

    return [
        {
            "concept_id": c.concept_id,
            "concept_name": c.concept_name,
            "concept_code": c.concept_code,
            "vocabulary_id": c.vocabulary_id,
            "domain_id": c.domain_id,
            "concept_class_id": c.concept_class_id,
            "standard_concept": c.standard_concept,
        }
        for c in concepts
    ]


def icd10_codes_list() -> list[str]:
    """Return all ICD-10 codes present in the mapping file."""
    return sorted(_load_raw_mappings().keys())
