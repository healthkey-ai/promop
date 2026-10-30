"""Read-only source candidates from installed terminology, before SCCM exists."""
import hashlib
import json
from functools import reduce
from operator import or_

from django.db.models import Case, CharField, Exists, OuterRef, Q, Value, When
from django.db.models.functions import Upper
from django.contrib.postgres.search import TrigramSimilarity
from django.utils import timezone

from omop_core.models import Concept, SourceCodeConceptMapping, SourceVocabularyTerm, UmlsSourceCode
from omop_core.services.concept_to_code import source_payload
from omop_core.services.source_labels import normalise
from omop_core.services.source_vocabularies import (
    DOMAIN_TO_TABLE, VOCAB_TO_UMLS_ROOT, VOCABULARY_OID_ALIASES,
    canonical_source_vocabulary,
)

ROOT_TO_VOCAB = {}
for _vocabulary, _root in VOCAB_TO_UMLS_ROOT.items():
    ROOT_TO_VOCAB.setdefault(_root, _vocabulary)


def current_concepts():
    today = timezone.now().date()
    return Concept.objects.filter(
        Q(invalid_reason__isnull=True) | Q(invalid_reason=''),
        valid_start_date__lte=today, valid_end_date__gte=today,
    ).exclude(vocabulary__vocabulary_id__startswith='HK-').exclude(source='HealthKey')


def _candidate(kind, row, domain_id):
    if kind == 'concept':
        vocabulary, code, name = row.vocabulary_id, row.concept_code, row.concept_name
        version = [row.concept_class_id, row.valid_start_date, row.valid_end_date]
    elif kind == 'catalog':
        vocabulary, code, name = row.vocabulary_id, row.code, row.name
        version = [row.vocabulary.release_version]
    else:
        vocabulary, code, name = ROOT_TO_VOCAB[row.root_source], row.code, row.name
        version = [row.concept_id, row.concept.release_id]
    if not code or len(code) > SourceCodeConceptMapping._meta.get_field('source_code').max_length:
        return None
    source = {
        'mapping_id': None, 'source_vocabulary_id': vocabulary, 'source_code': code,
        'source_code_description': name, 'occurrence_count': 0, 'status': 'unmapped',
        'origin_system': 'vocabulary', 'updated_at': '',
        'destination_concept_id': None, 'destination_concept_name': '',
        'destination_standard_concept': None, 'domain_id': domain_id,
        'omop_table': DOMAIN_TO_TABLE[domain_id],
        'source_label_norm': normalise(name),
        'source_kind': kind, 'source_record_id': row.pk,
    }
    source['source_revision'] = hashlib.sha256(json.dumps(
        [source, version], sort_keys=True, default=str,
    ).encode()).hexdigest()
    return source


def _unblocked(query, concept, vocabulary_field, code_field):
    """Exclude known decisions before applying a candidate limit."""
    blocked = SourceCodeConceptMapping.objects.filter(
        Q(status__in=['approved', 'rejected']) | Q(origin_system='athena')
        | Q(target_concept_id=concept.pk)
        | ~Q(domain_id__in=['', concept.domain_id])
        | ~Q(omop_table__in=['', DOMAIN_TO_TABLE[concept.domain_id]]),
        source_vocabulary_id=OuterRef(vocabulary_field), source_code=OuterRef(code_field),
    )
    return query.filter(~Exists(blocked))


def _compatible_catalog(query, concept, vocabulary_field, code_field):
    # Publisher/UMLS terms may also have OMOP metadata. Do not bypass a known
    # retirement or domain mismatch by returning the other representation.
    incompatible = Concept.objects.filter(
        vocabulary_id=OuterRef(vocabulary_field), concept_code=OuterRef(code_field),
    ).exclude(pk__in=current_concepts().filter(domain_id=concept.domain_id).values('pk'))
    return _unblocked(query.filter(~Exists(incompatible)), concept, vocabulary_field, code_field)


# Keys each catalog query may read for an estimate. Past this it is a floor.
CATALOG_CAP = 1000


def _catalog_queries(concept, *, strategies, terms, merged=False):
    """(kind, unordered query, evidence, key fields, order) for each catalog search.

    ``merged`` ORs the terms into one query per kind, for a count; a run asks
    each term separately so that each one's best matches get a slot.
    """
    if 'umls' in strategies and concept.vocabulary_id in VOCAB_TO_UMLS_ROOT:
        cuis = UmlsSourceCode.objects.filter(
            root_source=VOCAB_TO_UMLS_ROOT[concept.vocabulary_id], code=concept.concept_code,
        ).values('concept_id')
        siblings = UmlsSourceCode.objects.filter(concept_id__in=cuis, root_source__in=ROOT_TO_VOCAB)
        siblings = siblings.annotate(source_vocabulary=Case(
            *[When(root_source=root, then=Value(vocabulary)) for root, vocabulary in ROOT_TO_VOCAB.items()],
            default=Value(''), output_field=CharField(),
        ))
        siblings = _compatible_catalog(siblings, concept, 'source_vocabulary', 'code').exclude(
            root_source=VOCAB_TO_UMLS_ROOT[concept.vocabulary_id], code=concept.concept_code,
        )
        yield 'umls', siblings, 'umls', ('source_vocabulary', 'code'), ('root_source', 'code', '-is_preferred', 'name', 'pk')

    if 'lexical' in strategies:
        concepts = _unblocked(current_concepts().filter(domain_id=concept.domain_id).exclude(
            pk=concept.pk,
        ), concept, 'vocabulary_id', 'concept_code')
        publisher = _compatible_catalog(SourceVocabularyTerm.objects.filter(retired=False).exclude(
            vocabulary__vocabulary_id__startswith='HK-',
        ), concept, 'vocabulary_id', 'code')
        terms = [term for term in terms if len(term.strip()) >= 3]
        if merged:
            if terms:
                yield 'concept', concepts.annotate(label=Upper('concept_name')).filter(
                    reduce(or_, (Q(label__trigram_similar=term) for term in terms)),
                ), 'lexical', ('vocabulary_id', 'concept_code'), ()
                yield 'catalog', publisher.annotate(label=Upper('search_text')).filter(
                    reduce(or_, (Q(label__contains=term) for term in terms)),
                ), 'lexical', ('vocabulary_id', 'code'), ()
            return
        for term in terms:
            matches = concepts.annotate(label=Upper('concept_name')).filter(
                label__trigram_similar=term,
            ).annotate(score=TrigramSimilarity(Upper('concept_name'), term))
            yield 'concept', matches, 'lexical', ('vocabulary_id', 'concept_code'), ('-score', 'concept_id')
            # search_text has an uppercase GIN index and includes publisher
            # synonyms. Similarity on the name sorts the bounded matching set.
            matches = publisher.annotate(label=Upper('search_text')).filter(
                label__contains=term,
            ).annotate(score=TrigramSimilarity(Upper('name'), term))
            yield 'catalog', matches, 'lexical', ('vocabulary_id', 'code'), ('-score', 'pk')


def _registered(keys, evaluate):
    vocabularies = {key[0] for key in keys}
    vocabularies.update(alias for alias, canonical in VOCABULARY_OID_ALIASES.items() if canonical in vocabularies)
    return evaluate(SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id__in=vocabularies, source_code__in={key[1] for key in keys},
    ).select_related('target_concept'))


def catalog_keys(concept, *, strategies, terms, evaluate):
    """Unregistered (vocabulary, code) keys a run could propose, by evidence.

    A registered code is its mapping rows instead: a run proposes those when
    all of them are still open, and skips a decided one. Returns
    ({evidence: unregistered keys}, {evidence: open mapping row ids}, capped).
    """
    found = {'umls': set(), 'lexical': set()}
    capped = False
    limit = SourceCodeConceptMapping._meta.get_field('source_code').max_length
    for _kind, query, evidence, fields, _order in _catalog_queries(concept, strategies=strategies, terms=terms, merged=True):
        # No DISTINCT: it would read every match before the cap. A set dedupes.
        keys = evaluate(query.order_by().values_list(*fields)[:CATALOG_CAP])
        capped = capped or len(keys) >= CATALOG_CAP
        found[evidence].update(key for key in keys if key[1] and len(key[1]) <= limit
                               and key != (concept.vocabulary_id, concept.concept_code))
    rows = {evidence: set() for evidence in found}
    every = set().union(*found.values())
    if every:
        registered = {}
        for row in _registered(every, evaluate):
            registered.setdefault((canonical_source_vocabulary(row.source_vocabulary_id), row.source_code), []).append(row)
        for evidence, keys in found.items():
            for key in keys & registered.keys():
                if _open(registered[key], concept):
                    rows[evidence].update(row.pk for row in registered[key])
        found = {evidence: keys - registered.keys() for evidence, keys in found.items()}
    return found, rows, capped


def _open(rows, concept):
    """Whether a run may propose a registered code: none of its rows is decided."""
    return not any(row.status != 'proposed' or row.origin_system == 'athena' or row.target_concept_id == concept.pk
                   or row.domain_id not in ('', concept.domain_id)
                   or row.omop_table not in ('', DOMAIN_TO_TABLE[concept.domain_id]) for row in rows)


def catalog_candidates(concept, *, strategies, terms, limit, evaluate):
    """Bound each indexed query; the caller enforces a shared SQL deadline."""
    pool = {}

    def collect(kind, records, evidence):
        for record in evaluate(records):
            candidate = _candidate(kind, record, concept.domain_id)
            if candidate is None:
                continue
            key = (candidate['source_vocabulary_id'], candidate['source_code'])
            if key == (concept.vocabulary_id, concept.concept_code):
                continue
            current = pool.setdefault(key, {**candidate, 'evidence': []})
            if evidence not in current['evidence']:
                current['evidence'].append(evidence)
            if evidence == 'lexical':
                current['lexical_score'] = max(current.get('lexical_score', 0), float(record.score))

    for kind, query, evidence, _fields, order in _catalog_queries(concept, strategies=strategies, terms=terms):
        if kind == 'umls':
            query = query.select_related('concept').order_by(*order).distinct('root_source', 'code')
        elif kind == 'catalog':
            query = query.select_related('vocabulary').order_by(*order)
        else:
            query = query.order_by(*order)
        collect(kind, query[:limit], evidence)

    if not pool:
        return []
    mappings = _registered(pool, evaluate)
    registered = {}
    for row in mappings:
        registered.setdefault((canonical_source_vocabulary(row.source_vocabulary_id), row.source_code), []).append(row)

    candidates = []
    for key, candidate in pool.items():
        existing = registered.get(key, [])
        if not existing:
            candidates.append(candidate)
            continue
        if not _open(existing, concept):
            continue
        for row in existing:
            candidates.append({**source_payload(row), 'evidence': candidate['evidence'],
                               'lexical_score': candidate.get('lexical_score', 0)})
    return candidates


def refresh_catalog_candidate(candidate, concept):
    """Recheck the installed source and preview revision before a proposal."""
    kind, record_id = candidate.get('source_kind'), candidate.get('source_record_id')
    if kind == 'concept':
        row = current_concepts().filter(pk=record_id, domain_id=concept.domain_id).first()
    elif kind == 'catalog':
        row = SourceVocabularyTerm.objects.select_related('vocabulary').filter(pk=record_id, retired=False).first()
    elif kind == 'umls':
        row = UmlsSourceCode.objects.select_related('concept').filter(pk=record_id, root_source__in=ROOT_TO_VOCAB).first()
    else:
        return None
    if row is None:
        return None
    fresh = _candidate(kind, row, concept.domain_id)
    if (fresh is None or fresh['source_vocabulary_id'].startswith('HK-')
            or fresh['source_revision'] != candidate.get('source_revision')):
        return None
    known = Concept.objects.filter(vocabulary_id=fresh['source_vocabulary_id'], concept_code=fresh['source_code'])
    if known.exclude(pk__in=current_concepts().filter(domain_id=concept.domain_id).values('pk')).exists():
        return None
    return fresh
