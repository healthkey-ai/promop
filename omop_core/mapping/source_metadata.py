"""Source Metadata as context for the Suggest rankers (#1782, #1786).

A source code's description can be missing, wrong or generic. Epic adds LOINC
8716-3 "Vital signs" to every vital sign, so respirations can arrive described
as "Vital signs". The Source Metadata the mapping dialog shows -- often FHIR
JSON -- says "Respirations" and lists LOINC 9279-1 "Respiratory rate".

:func:`summarize` turns that metadata into a bounded digest that Jev and
Anthropic see beside the description. It is context only: retrieval does not
read it.

The digest goes to third parties, so it is built from named fields only: the
code's names and codings, its category and unit, and the aggregate evidence
keys in ``_OTHER_KEYS``. A resource's subject, dates, identifiers, notes and
values never leave this module.
"""
import json

from omop_core.mapping.suggestion_context import MAX_CONTEXT_TEXT

MAX_NAMES = 8
MAX_CODINGS = 12
MAX_OTHER_JSON = 2000

_RESOURCE_KEYS = ('resource', 'resources', 'fhir')
# Aggregate evidence about the code, never about a patient: what the curation
# import stores (hospital_code_backfill._source_metadata) and what the ETL was
# asked for in etl#30. An allowlist, because the rest goes to a third party.
_OTHER_KEYS = (
    'category', 'value_types', 'valueType', 'reference_range', 'referenceRange',
    'unit_coverage',
)


def _strings(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v.strip()[:MAX_CONTEXT_TEXT] for v in value if isinstance(v, str) and v.strip()]


def _resources(metadata):
    if metadata.get('resourceType'):
        yield metadata
    for key in _RESOURCE_KEYS:
        value = metadata.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                yield item


def _codeable(concept, names, displays, codings):
    if not isinstance(concept, dict):
        return
    names.extend(_strings(concept.get('text')))
    for coding in concept.get('coding') or []:
        if not isinstance(coding, dict):
            continue
        display = _strings(coding.get('display'))
        displays.extend(display)
        code = coding.get('code')
        if isinstance(code, (str, int)) and str(code).strip():
            codings.append({
                'system': str(coding.get('system') or '')[:MAX_CONTEXT_TEXT],
                'code': str(code).strip()[:MAX_CONTEXT_TEXT],
                'display': display[0] if display else '',
            })


def _prune(value, depth=0):
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 2:
        return None
    if isinstance(value, list):
        return [_prune(v, depth + 1) for v in value[:5]]
    if isinstance(value, dict):
        return {str(k)[:100]: _prune(v, depth + 1) for k, v in list(value.items())[:20]}
    return None


def _distinct(values, limit):
    out, seen = [], set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out[:limit]


def summarize(metadatas):
    """A bounded, JSON-safe digest of *metadatas*, or ``{}`` when they say nothing."""
    names, displays, codings, categories, units, other = [], [], [], [], [], {}
    for metadata in metadatas or []:
        if not isinstance(metadata, dict):
            continue
        names.extend(_strings(metadata.get('text')))
        displays.extend(_strings(metadata.get('display')))
        for resource in _resources(metadata):
            _codeable(resource.get('code'), names, displays, codings)
            for category in resource.get('category') or []:
                category_names, category_displays = [], []
                _codeable(category, category_names, category_displays, [])
                categories.extend(category_names or category_displays)
            quantity = resource.get('valueQuantity')
            if isinstance(quantity, dict):
                units.extend(_strings(quantity.get('unit')))
        if metadata.get('resourceType'):
            continue    # a bare resource: only what _codeable read above
        for key in _OTHER_KEYS:
            if key in metadata and key not in other:
                other[key] = _prune(metadata[key])

    seen_codings, distinct_codings = set(), []
    for coding in codings:
        key = (coding['system'].casefold(), coding['code'].casefold())
        if key not in seen_codings:
            seen_codings.add(key)
            distinct_codings.append(coding)

    summary = {
        'texts': _distinct(names, MAX_NAMES),
        'displays': _distinct(displays, MAX_NAMES),
        'codings': distinct_codings[:MAX_CODINGS],
        'categories': _distinct(categories, MAX_NAMES),
        'units': _distinct(units, MAX_NAMES),
    }
    if other and len(json.dumps(other, default=str)) <= MAX_OTHER_JSON:
        summary['other'] = other
    return {key: value for key, value in summary.items() if value}
