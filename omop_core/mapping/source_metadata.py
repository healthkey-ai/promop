"""What the sending system attached to a source code, as Suggest evidence (#1782).

A source code's description can be wrong or generic. Epic adds LOINC 8716-3
"Vital signs" to every vital sign, so a respirations Observation can arrive
described as "Vital signs". The same Observation's ``code.text`` reads
"Respirations" and lists LOINC 9279-1 "Respiratory rate" among its codings.
That content arrives in ``source_metadata``, on the queue row
(``SourceCodeConceptMapping``) and on each patient's ``PatientSourceCode``.

This module gathers that metadata and reduces it to three things Suggest
uses:

- :func:`summarize`: a bounded summary the ranker sees beside the description.
- :func:`search_texts`: the one name it gives, searched beside the description.
- :func:`coded_candidates`: standard concepts named by codings on the resource.

The summary goes to third-party rankers, so it is built from named fields only:
the names and codings of the code, its category and unit, and the aggregate
evidence keys in ``_OTHER_KEYS``. A resource's subject, dates, identifiers,
notes and values never leave this module.

The metadata is gathered across occurrences of the code, and possibly across
patients, so it can name several different things. The summary keeps them all.
The ranker is told that several different texts mean the code covers several
things.
"""
import json

from django.db.models import Q

from omop_core.mapping.code_resolution import normalize_omop_table
from omop_core.mapping.suggestion_context import MAX_CONTEXT_TEXT
from omop_core.services.source_vocabularies import FHIR_SYSTEM_VOCABULARIES

#: Retrieval tag for a candidate named by a coding in the metadata. Short
#: because ``SourceCodeConceptMapping.suggest_strategy`` is 10 characters.
STRATEGY_METADATA = 'metadata'

MAX_METADATA_ROWS = 5
MAX_NAMES = 8
MAX_CODINGS = 12
MAX_OTHER_JSON = 2000
#: Extra search results per strategy. The description's own search keeps its
#: full limit; this one only has to surface the specific concept.
EXTRA_SEARCH_LIMIT = 5

_RESOURCE_KEYS = ('resource', 'resources', 'fhir')
# Aggregate evidence about the code, never about a patient: what the curation
# import stores (hospital_code_backfill._source_metadata) and what the ETL was
# asked for in etl#30. An allowlist, because the rest goes to a third party.
_OTHER_KEYS = (
    'category', 'value_types', 'valueType', 'reference_range', 'referenceRange',
    'unit_coverage',
)

_STANDARD_VOCABULARIES = set(FHIR_SYSTEM_VOCABULARIES.values())


def _table_aliases(omop_table):
    canonical = normalize_omop_table(omop_table)
    return {canonical} | {
        alias for alias in ('condition_occurrence', 'drug', 'procedure_occurrence')
        if normalize_omop_table(alias) == canonical
    }


def gather(*, source_code, source_vocabulary_id, omop_table, mapping_metadata=None):
    """The queue row's metadata, then the most recent distinct patient rows'."""
    from omop_core.models import PatientSourceCode

    found, seen = [], set()

    def add(metadata):
        if not isinstance(metadata, dict) or not metadata:
            return False
        key = json.dumps(metadata, sort_keys=True, default=str)
        if key in seen:
            return False
        seen.add(key)
        found.append(metadata)
        return True

    add(mapping_metadata)
    if source_code:
        rows = (
            PatientSourceCode.objects
            .filter(source_value=source_code,
                    source_vocabulary_id=source_vocabulary_id or '',
                    omop_table__in=_table_aliases(omop_table))
            .exclude(source_metadata={})
            .order_by('-last_seen')
            .values_list('source_metadata', flat=True)[:MAX_METADATA_ROWS * 4]
        )
        patient_rows = 0
        for metadata in rows:
            if patient_rows >= MAX_METADATA_ROWS:
                break
            patient_rows += add(metadata)
    return found


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


def search_texts(summary, *, description='', source_code=''):
    """The metadata's own name for the code, when it gives exactly one.

    One text is what the sender called this thing: "Respirations". Several
    mean the code covers several things, and searching any one of them would
    steer the pool toward it. The codings are covered by
    :func:`coded_candidates`, without a text search.
    """
    texts = summary.get('texts', [])
    if len(texts) != 1:
        return []
    already = {(description or '').strip().casefold(), (source_code or '').strip().casefold()}
    return [] if texts[0].casefold() in already else [texts[0]]


def coded_candidates(summary, domain_id, *, source_vocabulary_id='', source_code=''):
    """Standard concepts named by the resource's codings in standard vocabularies.

    A non-standard code (a retired LOINC, say) contributes the concepts it
    ``Maps to``. The row's own code is skipped: it is the thing being mapped.
    """
    from omop_core.models import Concept, ConceptRelationship

    own = ((source_vocabulary_id or '').casefold(), (source_code or '').casefold())
    by_pair = {}
    for coding in summary.get('codings', []):
        vocabulary_id = FHIR_SYSTEM_VOCABULARIES.get(coding['system'].strip().rstrip('/').casefold())
        if vocabulary_id not in _STANDARD_VOCABULARIES:
            continue
        if (vocabulary_id.casefold(), coding['code'].casefold()) == own:
            continue
        by_pair.setdefault((vocabulary_id, coding['code']), coding)
    if not by_pair:
        return []

    query = Q()
    for vocabulary_id, code in by_pair:
        query |= Q(vocabulary_id=vocabulary_id, concept_code=code)
    named = list(Concept.objects.filter(query))
    via = {}
    for concept in named:
        coding = by_pair.get((concept.vocabulary_id, concept.concept_code))
        if concept.standard_concept == 'S' and concept.invalid_reason is None:
            via.setdefault(concept.concept_id, coding)
    nonstandard = {c.concept_id: by_pair.get((c.vocabulary_id, c.concept_code))
                   for c in named if c.concept_id not in via}
    if nonstandard:
        for source_id, target_id in ConceptRelationship.objects.filter(
            concept_1_id__in=nonstandard, relationship_id='Maps to', invalid_reason__isnull=True,
        ).values_list('concept_1_id', 'concept_2_id'):
            via.setdefault(target_id, nonstandard[source_id])

    concepts = Concept.objects.filter(
        concept_id__in=via, standard_concept='S', invalid_reason__isnull=True,
    )
    if domain_id:
        concepts = concepts.filter(domain_id=domain_id)
    return [{
        'concept_id': c.concept_id,
        'concept_name': c.concept_name,
        'concept_code': c.concept_code,
        'vocabulary_id': c.vocabulary_id,
        'concept_class_id': c.concept_class_id,
        'domain_id': c.domain_id,
        'standard_concept': c.standard_concept,
        'retrieval': STRATEGY_METADATA,
        'metadata_coding': via[c.concept_id],
    } for c in concepts.order_by('concept_id')]
