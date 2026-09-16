"""Warm the vector reranker for everything currently sitting in the Suggest queue.

The Suggest pipeline is UMLS → lexical top-N → vector rerank → one ranking call.
Only the rerank needs anything precomputed: it scores the retrieved shortlist
against stored ``concept_embedding`` rows, and a candidate with no stored vector
is demoted rather than scored.  So the useful precompute is not "embed every
concept" (1.5M rows, 138 minutes, and most of them are never a candidate for
anything in the queue) but "embed the concepts the queue can actually retrieve".

That set is small and exactly derivable:

    for every eligible queue row (including replaceable Suggest answers)
        take its top-N lexical candidates and UMLS candidates
        embed any of them that is not embedded yet

This command is also the measurement tool for deciding how to trigger it.
``--measure`` runs the retrieval half, reports what it costs and what it would
have to embed, and writes nothing:

    manage.py precompute_suggest_embeddings --measure
    manage.py precompute_suggest_embeddings --measure --source-vocabulary ICD10

Retrieval is the expensive half — one trigram query per queue row, 2.7-3.9s each
against staging — which is the whole reason this is a batch job and not
something a Suggest click pays for.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from omop_core.mapping.code_resolution import _QUARANTINE_TARGETS
from omop_core.mapping.suggestions import (
    CANDIDATE_LIMIT,
    DEFAULT_MIN_OCCURRENCES,
    LEXICAL_LIMIT_MAX,
    SUGGESTION_MODEL_VERSION,
    _find_source_concept,
    _source_description,
    lexical_candidates,
    suggestable_mappings,
    umls_candidates,
)
from omop_core.models import ConceptEmbedding, SuggestEmbeddingSnapshot
from omop_core.services.embedding_snapshot import read_snapshot, snapshot_key

MODEL_NAME = 'BAAI/bge-small-en-v1.5'
ENCODE_BATCH = 256
DB_WRITE_BATCH = 64


def _fmt(seconds):
    return f'{seconds:.1f}s' if seconds < 90 else f'{seconds / 60:.1f}m'


class Command(BaseCommand):
    help = 'Embed the destination concepts the Suggest queue can retrieve.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--measure', action='store_true',
            help='Report timings and the work that would be done; write nothing.',
        )
        parser.add_argument(
            '--source-vocabulary', dest='source_vocabulary_id', default=None,
            help='Only walk one tab. Default: the whole queue.',
        )
        parser.add_argument(
            '--min-occurrences', type=int, default=DEFAULT_MIN_OCCURRENCES,
            help=f'Skip queue rows seen fewer times. Default {DEFAULT_MIN_OCCURRENCES}.',
        )
        parser.add_argument(
            '--lexical-limit', type=int, default=CANDIDATE_LIMIT,
            help=(
                'Candidates per queue row. Must match what Suggest requests, or '
                f'the extra ones are unembedded at request time. Default {CANDIDATE_LIMIT}.'
            ),
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after this many queue rows. Use it to time a sample.',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Re-embed candidates that already have a vector.',
        )

    def handle(self, **options):
        lexical_limit = max(1, min(options['lexical_limit'], LEXICAL_LIMIT_MAX))
        measure = options['measure']
        key = snapshot_key({
            'model': MODEL_NAME,
            'suggestion_version': SUGGESTION_MODEL_VERSION,
            'retrieval_version': 2,
            'source_vocabulary_id': options['source_vocabulary_id'],
            'min_occurrences': options['min_occurrences'],
            'limit': options['limit'],
            'lexical_limit': lexical_limit,
        })
        fingerprint, cached_ids, missing_cached = read_snapshot(key)
        if cached_ids is not None and not missing_cached and not options['force']:
            self.stdout.write(self.style.SUCCESS('Every candidate is already embedded (unchanged inputs).'))
            return

        started = time.time()
        candidate_ids = (set(cached_ids) if cached_ids is not None
                         else self._retrieve_candidates(options, lexical_limit))
        retrieval_seconds = time.time() - started

        if options['force']:
            missing = candidate_ids
        else:
            embedded = set(
                ConceptEmbedding.objects.filter(concept_id__in=candidate_ids)
                .values_list('concept_id', flat=True)
            )
            missing = candidate_ids - embedded
            self.stdout.write(f'Already embedded: {len(embedded)}. Missing: {len(missing)}.')

        if measure:
            self.stdout.write(self.style.SUCCESS(
                f'--measure: would embed {len(missing)} concept(s). Nothing written.'
            ))
            return
        if not missing:
            self._remember(key, fingerprint, candidate_ids)
            self.stdout.write(self.style.SUCCESS('Every candidate is already embedded.'))
            return

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise CommandError(
                'sentence-transformers is not installed. '
                'Run: pip install sentence-transformers'
            ) from exc

        from omop_core.models import Concept

        started = time.time()
        model = SentenceTransformer(MODEL_NAME)
        load_seconds = time.time() - started
        self.stdout.write(f'Model loaded in {_fmt(load_seconds)}.')

        pending = list(
            Concept.objects.filter(concept_id__in=missing)
            .values_list('concept_id', 'concept_name')
        )
        started = time.time()
        written = 0
        for offset in range(0, len(pending), ENCODE_BATCH):
            chunk = pending[offset:offset + ENCODE_BATCH]
            vectors = model.encode([name for _cid, name in chunk], show_progress_bar=False)
            rows_to_write = [
                ConceptEmbedding(concept_id=cid, embedding=vector.tolist())
                for (cid, _name), vector in zip(chunk, vectors)
            ]
            for start in range(0, len(rows_to_write), DB_WRITE_BATCH):
                ConceptEmbedding.objects.bulk_create(
                    rows_to_write[start:start + DB_WRITE_BATCH],
                    update_conflicts=True,
                    update_fields=['embedding'],
                    unique_fields=['concept_id'],
                )
            written += len(chunk)
            self.stdout.write(f'  embedded {written}/{len(pending)}', ending='\r')
        embed_seconds = time.time() - started
        self._remember(key, fingerprint, candidate_ids)

        self.stdout.write(self.style.SUCCESS(
            f'\nEmbedded {written} concept(s) in {_fmt(embed_seconds)}. '
            f'Total: {_fmt(retrieval_seconds + load_seconds + embed_seconds)}.'
        ))

    @staticmethod
    def _remember(key, fingerprint, candidate_ids):
        SuggestEmbeddingSnapshot.objects.update_or_create(
            key=key,
            defaults={'fingerprint': fingerprint, 'candidate_ids': sorted(candidate_ids)},
        )

    def _retrieve_candidates(self, options, lexical_limit):
        started = time.time()
        rows = suggestable_mappings(
            None,
            source_vocabulary_id=options['source_vocabulary_id'],
            min_occurrences=options['min_occurrences'],
            limit=options['limit'],
            # The queue rows a *future* run may touch, which includes the ones
            # already answered: a model bump or a Replace re-asks them, and by
            # then their candidates should already be embedded.
            resuggest=True,
        )
        queue_seconds = time.time() - started
        self.stdout.write(
            f'Eligible queue rows: {len(rows)} '
            f'in {_fmt(queue_seconds)}'
        )
        if not rows:
            self.stdout.write('Nothing queued. Done.')
            return set()

        # --- retrieval: one trigram query per queue row ---------------------
        started = time.time()
        candidate_ids = set()
        no_candidates = 0
        for index, mapping in enumerate(rows, start=1):
            target = _QUARANTINE_TARGETS.get(mapping.omop_table)
            domain_id = mapping.domain_id or (target[1] if target else '')
            if not domain_id:
                continue
            # The run's own preference order, not a re-spelling of it: a row
            # with both a UMLS name and a loaded source concept would otherwise
            # be embedded against text the run never asks for, leaving the
            # candidates it does ask for unembedded -- which vector_rerank
            # silently demotes rather than reporting.
            source_concept = mapping.source_concept or _find_source_concept(
                mapping.source_vocabulary_id, mapping.source_code,
            )
            text, _umls_name = _source_description(mapping, source_concept)
            hits = lexical_candidates(text or mapping.source_code, domain_id,
                                      limit=lexical_limit)
            # Warm both paths independently: callers can disable UMLS even
            # when its single definitive hit would otherwise skip lexical.
            umls_hits, _cui = umls_candidates(
                mapping.source_code, mapping.source_vocabulary_id, domain_id,
            )
            hits = [*hits, *umls_hits]
            if not hits:
                no_candidates += 1
            candidate_ids.update(hit['concept_id'] for hit in hits)
            if index % 25 == 0:
                self.stdout.write(
                    f'  retrieved for {index}/{len(rows)} rows '
                    f'({(time.time() - started) / index:.2f}s each)',
                    ending='\r',
                )
        retrieval_seconds = time.time() - started

        per_row = retrieval_seconds / len(rows)
        self.stdout.write(
            f'\nRetrieval: {_fmt(retrieval_seconds)} for {len(rows)} rows '
            f'({per_row:.2f}s each), {len(candidate_ids)} distinct candidate concepts, '
            f'{no_candidates} row(s) retrieved nothing.'
        )

        return candidate_ids
