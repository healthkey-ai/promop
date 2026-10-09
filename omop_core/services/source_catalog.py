"""Publisher source terminology, kept separate from OMOP destination concepts."""
from django.db.models import Case, IntegerField, Value, When
from django.db.models.functions import Upper

from omop_core.models import SourceVocabulary, SourceVocabularyTerm

ATTRIBUTIONS = {
    'NCIt': 'NCI Thesaurus, National Cancer Institute, CC BY 4.0. Imported publisher content.',
    'MeSH': 'Courtesy of the U.S. National Library of Medicine. Pinned snapshot; may not reflect the latest NLM data.',
}

def serialize_term(term):
    return {
        'vocabulary_id': term.vocabulary_id,
        'code': term.code,
        'name': term.name,
        'definition': term.definition,
        'synonyms': term.synonyms,
        'parents': term.parents,
        'semantic_types': term.semantic_types,
        'status': term.status,
        'retired': term.retired,
        'metadata': term.metadata,
        'release_version': term.vocabulary.release_version,
        'source_url': term.vocabulary.source_url,
    }


def lookup_source_term(vocabulary_id, code):
    return (SourceVocabularyTerm.objects.select_related('vocabulary')
            .filter(vocabulary_id=vocabulary_id, code=code).first())


def search_source_terms(vocabulary_id, query, *, include_retired=False):
    terms = SourceVocabularyTerm.objects.select_related('vocabulary').filter(
        vocabulary_id=vocabulary_id,
    )
    if not include_retired:
        terms = terms.filter(retired=False)
    return (terms.annotate(search=Upper('search_text'))
            .filter(search__contains=query.upper())
            .annotate(rank=Case(
                When(code__iexact=query, then=Value(0)),
                When(name__iexact=query, then=Value(1)),
                default=Value(2), output_field=IntegerField(),
            )).order_by('rank', 'name', 'code')[:25])


def catalog_response(vocabulary_id, *, code='', query='', include_retired=False):
    vocabulary = SourceVocabulary.objects.filter(pk=vocabulary_id).first()
    if not vocabulary:
        return {'available': False, 'results': [], 'term': None}
    term = lookup_source_term(vocabulary_id, code) if code else None
    return {
        'available': True,
        'release_version': vocabulary.release_version,
        'term_count': vocabulary.term_count,
        'source_url': vocabulary.source_url,
        'attribution': ATTRIBUTIONS.get(vocabulary_id, ''),
        'term': serialize_term(term) if term else None,
        'results': [serialize_term(t) for t in search_source_terms(
            vocabulary_id, query, include_retired=include_retired,
        )] if query else [],
    }
