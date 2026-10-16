"""Compact, deterministic source unit sets for the global mapping queue.

Unit symbols are case sensitive. Retain the sender code (or display when no
code exists), without guessing from a destination concept. Sorting a distinct
set keeps reordered/multivalue evidence in the same non-overlapping subgroup.
"""
from django.db.models import Func, JSONField


class SourceUnits(Func):
    output_field = JSONField()
    template = """COALESCE((
        SELECT jsonb_agg(unit ORDER BY unit COLLATE "C")
        FROM (
            SELECT DISTINCT COALESCE(NULLIF(btrim(item->>'code'), ''),
                                     NULLIF(btrim(item->>'display'), '')) AS unit
            FROM jsonb_array_elements(CASE
                WHEN jsonb_typeof(%(expressions)s) = 'array' THEN %(expressions)s
                ELSE '[]'::jsonb END) AS item
        ) source_units WHERE unit IS NOT NULL
    ), '[]'::jsonb)"""


def source_units(mapping):
    if mapping is None:
        return []
    if hasattr(mapping, 'source_units'):
        return mapping.source_units
    evidence = mapping.source_unit_evidence
    if not isinstance(evidence, list):
        return []
    return sorted({
        str(item.get('code') or '').strip() or str(item.get('display') or '').strip()
        for item in evidence if isinstance(item, dict)
    } - {''})
