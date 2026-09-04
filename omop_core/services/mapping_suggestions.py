"""Suggest destination concepts for source codes nobody has mapped yet.

The Code Mapping queue is only useful if something fills it. Ingest fills it for
codes it meets, but staging carries 10,483 distinct source values sitting at
``concept_id = 0`` from before the resolver existed, and a curator cannot type a
destination for each one.

Two stages, deliberately separated:

**Retrieval is lexical and index-backed.** The GIN trigram indexes narrow via
the ``%`` operator; ``similarity()`` then scores only the survivors. Scoring
first seq-scans 2.4M synonym rows -- 4.49s for one source value. A
synonym hit is worth more than a name hit -- synonyms are the terms clinicians
actually write, which is what a source value is.

**Ranking is not lexical, and that is the whole problem.** For
``SERUM FREE LIGHT CHAIN KAPPA`` trigram's top hit is *Free kappa/lambda light
chain ratio in serum* (0.67) -- a ratio, clinically the wrong quantity -- while
the correct *Kappa light chains.free [Mass/volume] in Serum* sits third at 0.64.
Retrieval put the answer in the shortlist and ranking buried it. So a model
re-ranks the shortlist.

Everything degrades rather than fails: no API key, no network, a bad response --
the lexical order stands and the proposal says so. A Suggest button that returns
nothing because a third party is down is worse than one that returns a decent
guess a curator can correct.
"""
import json
import logging

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Case, CharField, Count, F, Max, Q, Value, When
from django.db.models.functions import Upper

from omop_core.models import Concept, ConceptSynonym, SourceCodeConceptMapping
from omop_core.services.code_mapping import (
    CLINICAL_TABLES,
    NO_MATCHING_CONCEPT_ID,
    SOURCE_CODE_MAX,
    _QUARANTINE_TARGETS,
)

logger = logging.getLogger(__name__)

# A curator cannot review 10,483 codes. On staging 4,553 of them (43%) appear
# exactly once -- free text like 't(11;14)(CCND1,IGH) % in Bone Marrow by FISH',
# real findings but not codes anyone meets twice -- while 512 carry the traffic.
# Proposing for the long tail buries the codes that matter, so the default asks
# for ten sightings and the caller can lower it once the queue is drained.
DEFAULT_MIN_OCCURRENCES = 10

# How many candidates retrieval hands the ranker. Enough that the right concept
# is in the list (it was third in the motivating example), few enough to rank
# cheaply.
CANDIDATE_LIMIT = 25

# Below this a trigram hit is noise. Tuned against the staging corpus: the
# motivating example's correct answer scored 0.64 and its worst plausible
# candidate 0.59.
MIN_TRIGRAM_SCORE = 0.3

# A synonym match beats a name match at equal similarity. Synonyms are what
# clinicians write, and a source value is a clinician's words.
SYNONYM_BONUS = 0.05


def unmapped_source_values(omop_table, min_occurrences=DEFAULT_MIN_OCCURRENCES,
                           limit=None, source_vocabulary_id=None):
    """Source values at concept 0 that nobody has proposed a mapping for.

    A source value is identified by both its text and source vocabulary.  The
    same text is valid in multiple code systems, and combining them would both
    lose FHIR provenance and create a mapping that could re-point the wrong
    facts.  ``''`` is reserved for genuinely uncoded rows.

    Ordered by how often they occur, because that is the order a curator should
    meet them in: the code seen 400 times is worth more of their attention than
    the one seen once.
    """
    model, concept_col, source_col = CLINICAL_TABLES[omop_table]
    source_concept_col = source_col.replace('_source_value', '_source_concept_id')
    source_vocabulary = Case(
        When(
            Q(**{f'{source_concept_col}__isnull': True})
            | Q(**{source_concept_col: NO_MATCHING_CONCEPT_ID}),
            then=Value(''),
        ),
        default=F(f'{source_concept_col}__vocabulary_id'),
        output_field=CharField(),
    )

    # Rejected counts as decided. Excluding it here put the code back at the
    # front of the queue on every run -- it sorts by occurrence -- where it
    # spent a model call and created nothing, and the caller reported
    # "no unmapped codes" because nothing was created.
    already = {
        ((vocabulary_id or ''), code.upper())
        for vocabulary_id, code in SourceCodeConceptMapping.objects.values_list(
            'source_vocabulary_id', 'source_code',
        )
    }

    rows = (
        model.objects
        .filter(**{concept_col: NO_MATCHING_CONCEPT_ID})
        .exclude(**{f'{source_col}__isnull': True})
        .exclude(**{source_col: ''})
        .annotate(source_vocabulary_id=source_vocabulary)
        .values(source_col, 'source_vocabulary_id')
        .annotate(occurrences=Count(model._meta.pk.name))
        .filter(occurrences__gte=min_occurrences)
        .order_by('-occurrences', source_col, 'source_vocabulary_id')
    )
    # When filtering by source vocabulary, only return rows from that vocabulary.
    if source_vocabulary_id is not None:
        rows = rows.filter(source_vocabulary_id=source_vocabulary_id)

    out = []
    for row in rows.iterator():
        value = row[source_col]
        source_vocabulary_id = row['source_vocabulary_id'] or ''
        if (source_vocabulary_id, value.upper()) in already:
            continue
        out.append((value, source_vocabulary_id, row['occurrences']))
        if limit and len(out) >= limit:
            break
    return out


def lexical_candidates(source_value, domain_id, limit=CANDIDATE_LIMIT):
    """Concepts whose name or synonyms look like this source value.

    Scoped to the domain, so a lab name cannot retrieve a drug. Standard
    concepts only -- a curator re-pointing at a non-standard one is a decision
    they can still make by hand, but it is never what we should suggest.
    """
    query = (source_value or '').strip().upper()
    if len(query) < 3:
        return []

    # Narrow with `%` first, score second.
    #
    # TrigramSimilarity(...) > x compiles to SIMILARITY(...) > x, which is not
    # an indexable expression -- it seq-scans 2.4M synonym rows, measured at
    # 4.49s for a single source value. `__trigram_similar` emits the `%`
    # operator, which is what gin_trgm_ops answers, so the GIN index does the
    # narrowing and similarity() only scores the handful that survive.
    #
    # The `%` must be applied to UPPER(col), not the raw column: both indexes
    # are on the uppercased expression, and querying the raw column silently
    # misses them -- the same raw-vs-UPPER mismatch that made concepts/search
    # ineffective (#262).
    #
    # `%` uses pg_trgm.similarity_threshold (0.3 by default), the same cut
    # MIN_TRIGRAM_SCORE applies -- the explicit filter stays so the constant
    # governs regardless of the session setting.
    by_name = (
        Concept.objects
        .filter(standard_concept='S', domain_id=domain_id, invalid_reason__isnull=True)
        .annotate(name_upper=Upper('concept_name'))
        .filter(name_upper__trigram_similar=query)
        .annotate(score=TrigramSimilarity(Upper('concept_name'), query))
        .filter(score__gt=MIN_TRIGRAM_SCORE)
        .order_by('-score')[:limit]
    )

    # Synonyms are a separate index and a separate signal; merged by concept,
    # keeping whichever route scored higher.
    synonym_hits = (
        ConceptSynonym.objects
        .annotate(name_upper=Upper('concept_synonym_name'))
        .filter(name_upper__trigram_similar=query)
        .annotate(score=TrigramSimilarity(Upper('concept_synonym_name'), query))
        .filter(score__gt=MIN_TRIGRAM_SCORE)
        .values('concept_id')
        .annotate(score=Max('score'))
        .order_by('-score')[:limit]
    )
    synonym_scores = {h['concept_id']: h['score'] + SYNONYM_BONUS for h in synonym_hits}

    merged = {}
    for concept in by_name:
        merged[concept.concept_id] = (concept, float(concept.score))
    if synonym_scores:
        for concept in Concept.objects.filter(
            concept_id__in=list(synonym_scores),
            standard_concept='S', domain_id=domain_id, invalid_reason__isnull=True,
        ):
            score = float(synonym_scores[concept.concept_id])
            if concept.concept_id not in merged or score > merged[concept.concept_id][1]:
                merged[concept.concept_id] = (concept, score)

    ranked = sorted(merged.values(), key=lambda pair: -pair[1])[:limit]
    return [
        {
            'concept_id': c.concept_id,
            'concept_name': c.concept_name,
            'concept_code': c.concept_code,
            'vocabulary_id': c.vocabulary_id,
            'concept_class_id': c.concept_class_id,
            'lexical_score': round(score, 3),
        }
        for c, score in ranked
    ]


_RANKING_SCHEMA = {
    'type': 'object',
    'properties': {
        'concept_id': {
            'type': ['integer', 'null'],
            'description': 'The best candidate, or null if none of them is right.',
        },
        'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
        'reason': {'type': 'string', 'description': 'One sentence, for the curator.'},
    },
    'required': ['concept_id', 'confidence', 'reason'],
    'additionalProperties': False,
}

_RANKING_SYSTEM = """You map clinical source codes onto OMOP concepts.

You are given one source value as it appeared in real clinical data, and a
shortlist of candidate OMOP concepts retrieved by string similarity. Pick the
candidate that means the same clinical thing, or null if none does.

String similarity is not meaning. The highest-scoring candidate is often wrong
in a specific way: a ratio is not the analyte it is computed from, a panel is
not one of its components, a urine measurement is not a serum one, and a
qualitative finding is not a quantitative result. Prefer the candidate whose
specimen, quantity and method match the source value; where the source value is
silent on method, prefer the more general concept over a method-specific one.

Answer null rather than guessing. A wrong mapping is written into patient
records; an unmapped code stays in a queue where a human will see it."""


def rank_candidates(source_value, candidates, source_description=''):
    """Re-rank a lexical shortlist by meaning. Returns (chosen, note).

    ``chosen`` is a candidate dict or None; ``note`` explains the choice for the
    curator, including when the model was unavailable and the lexical order
    stands.
    """
    if not candidates:
        return None, 'No candidate concept scored above the similarity threshold.'

    top = candidates[0]
    lexical_note = (
        f'Lexical match only (score {top["lexical_score"]}). '
        f'Ranking model unavailable, so this is the highest string similarity, '
        f'which is frequently not the closest clinical match — review carefully.'
    )

    if not getattr(settings, 'ANTHROPIC_API_KEY', ''):
        return top, lexical_note

    try:
        import anthropic
    except ImportError:
        logger.warning('anthropic SDK not installed; falling back to lexical order.')
        return top, lexical_note

    listing = '\n'.join(
        f'{c["concept_id"]}\t{c["vocabulary_id"]}:{c["concept_code"]}\t'
        f'{c["concept_name"]}\t(class {c["concept_class_id"]})'
        for c in candidates
    )
    described = f'\nSource description: {source_description}' if source_description else ''

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model='claude-opus-5',
            # Thinking tokens count against this. At 1024 the response stopped
            # at max_tokens with no text block, json.loads raised, and the
            # ranker silently degraded to the lexical order it exists to fix.
            max_tokens=16000,
            system=_RANKING_SYSTEM,
            thinking={'type': 'adaptive'},
            output_config={
                'effort': 'medium',
                'format': {'type': 'json_schema', 'schema': _RANKING_SCHEMA},
            },
            messages=[{
                'role': 'user',
                'content': (
                    f'Source value: {source_value}{described}\n\n'
                    f'Candidates (concept_id, vocabulary:code, name, class):\n{listing}'
                ),
            }],
        )
    except Exception as exc:                      # noqa: BLE001 - degrade, never fail
        # A Suggest button that returns nothing because a third party is down is
        # worse than one that returns a decent guess a curator can correct.
        logger.warning('Concept ranking failed for %r: %s', source_value, exc)
        return top, lexical_note

    payload = next(
        (block.text for block in response.content if block.type == 'text'), ''
    )
    try:
        verdict = json.loads(payload)
    except (TypeError, ValueError):
        verdict = None
    # A bare `null` or a list parses fine and then has no .get -- and the system
    # prompt asks for null, so this is the shape a model most plausibly gets
    # wrong. Degrading is the contract; 500ing the request is not.
    if not isinstance(verdict, dict):
        logger.warning('Concept ranking returned unusable output for %r.', source_value)
        return top, lexical_note

    chosen_id = verdict.get('concept_id')
    if chosen_id is None:
        return None, f'No suitable concept: {verdict.get("reason", "")}'.strip()

    chosen = next((c for c in candidates if c['concept_id'] == chosen_id), None)
    if chosen is None:
        # The model named something outside the shortlist. Do not follow it --
        # the candidates were domain-scoped and validated, an arbitrary id is not.
        logger.warning(
            'Concept ranking chose %s, which was not among the candidates for %r.',
            chosen_id, source_value,
        )
        return top, lexical_note

    return chosen, (
        f'{verdict.get("confidence", "unknown")} confidence: '
        f'{verdict.get("reason", "")}'.strip()
    )


def suggest_mappings(omop_table, *, min_occurrences=DEFAULT_MIN_OCCURRENCES,
                     limit=None, source_system='suggest', dry_run=False,
                     source_vocabulary_id=None):
    """Propose mappings for unmapped source values in one clinical table.

    Every proposal lands as ``proposed`` with ``origin='import'`` -- a machine
    guessed it, and the queue says so, which is the difference between a
    suggestion and a decision. Where no candidate is convincing, the proposal
    deliberately has no destination. Minting an HK-* concept named only after
    the source code would create a fake destination with no clinical meaning.

    Returns a list of result dicts, one per source value considered.
    """
    target = _QUARANTINE_TARGETS.get(omop_table)
    if target is None:
        raise ValueError(f'No quarantine vocabulary for table {omop_table!r}.')
    _hk_vocabulary, domain_id, _concept_class_id, _slug_prefix = target

    results = []
    for source_value, src_vocab_id, occurrences in unmapped_source_values(
        omop_table, min_occurrences=min_occurrences, limit=limit,
        source_vocabulary_id=source_vocabulary_id,
    ):
        # If the incoming code's vocabulary is loaded, its own concept name is
        # evidence supplied by the source system, not an inference from code
        # punctuation. It makes ranking a code such as ``85319-5`` meaningful
        # without pretending the code itself is a display name.
        source_concept = Concept.objects.filter(
            vocabulary_id=src_vocab_id,
            concept_code__iexact=source_value,
        ).first() if src_vocab_id else None
        source_description = source_concept.concept_name if source_concept else ''
        candidates = lexical_candidates(source_description or source_value, domain_id)
        chosen, note = rank_candidates(
            source_value, candidates, source_description=source_description,
        )

        entry = {
            'source_code': source_value,
            'source_vocabulary_id': src_vocab_id,
            'source_code_description': source_description,
            'occurrences': occurrences,
            'suggested': chosen,
            'note': note,
            'candidates_considered': len(candidates),
        }
        if dry_run:
            results.append(entry)
            continue

        concept = Concept.objects.filter(concept_id=chosen['concept_id']).first() if chosen else None

        mapping, created = SourceCodeConceptMapping.objects.get_or_create(
            source_vocabulary_id=src_vocab_id,
            source_code=source_value[:SOURCE_CODE_MAX],
            defaults={
                'domain_id': domain_id,
                'source_code_description': source_description[:255],
                'target_concept': concept,
                'destination_vocabulary_id': concept.vocabulary_id if concept else '',
                'omop_table': omop_table,
                'status': 'proposed',
                'origin': 'import',
                'origin_system': source_system,
                'occurrence_count': occurrences,
                'notes': note,
            },
        )
        entry['created'] = created
        entry['mapping_id'] = mapping.id
        results.append(entry)

    return results
