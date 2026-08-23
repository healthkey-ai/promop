"""Propose concept mappings for PatientRecord fields that have none.

Retrieval is lexical; the choice is not.

Trigram similarity is the only practical way to narrow several million concepts
to a handful, but it compares spelling and cannot tell ``clonal_plasma_cells``
from "Polyclonal plasma cells" — which it scores 0.75 while meaning the opposite.
So this command retrieves candidates and stops; the pick between them is a
judgement about meaning, recorded in ``suggested_mappings.py`` and seeded from
there.

Three modes:

* ``--emit-candidates PATH`` — retrieve and write the shortlist per field, for
  someone (or something) to judge.
* ``--from-reviewed`` — seed the judged choices as **proposed** rows.
* default — seed the top lexical match as **proposed**, for fields nobody has
  judged yet. Weakest mode, and the notes say so.

An approved ``FieldConceptMapping`` makes a field writable. Producing those by
hand for 127 fields is the bottleneck in the writable-UI work, and most of the
labour is not judgement — it is finding plausible candidates in a vocabulary of
several million concepts. This does that part and stops there.

Everything it writes is **proposed**, never approved, so nothing it suggests can
make a field writable on its own. A reviewer confirms or rejects each one in the
mapping interface. That is the whole safety property: a wrong suggestion costs a
click, while a wrong *approval* would write clinical facts against the wrong
concept.

Two things it records that a curator would otherwise have to work out:

* the alternatives it considered and their scores, so a reviewer can see whether
  the top match won clearly or narrowly;
* whether derivation actually reads the field back. A mapping makes a field
  writable; it does not make anything read the value. Approving a mapping for a
  field nothing derives produces a write that succeeds and a value that never
  returns (#648), which is worse than a read-only box.
"""
import re

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from omop_core.models import Concept, FieldConceptMapping

# Domains a clinical fact can live in here. The write path handles measurement
# and observation; anything else cannot be acted on even once approved.
# The *question* a fact answers, so answer concepts are excluded: LOINC LA codes
# live in Meas Value and belong in value_as_concept, not observation_concept.
_DOMAINS = ('Measurement', 'Observation')
_VOCABULARIES = ('LOINC', 'SNOMED')

# Score below which a candidate is not worth a reviewer's attention. Chosen from
# the observed distribution: exact-phrase matches land near 0.6+, while anything
# under ~0.35 was noise sharing a common word like "status" or "date".
_MIN_SCORE = 0.50

# Field-name fragments that carry no meaning for a vocabulary search — they
# describe the column's shape rather than the clinical concept.
_NOISE = {
    'status', 'value', 'depr', 'options', 'str', 'flag', 'code',
    'result', 'details', 'type',
}


def _search_text(field_name: str) -> str:
    """Turn a column name into something worth searching a vocabulary for.

    ``no_hepatitis_b_status`` searches for "hepatitis b": the ``no_`` prefix is a
    negation the projection applies, not part of the concept, and ``status`` is
    shape rather than meaning.
    """
    tokens = [t for t in field_name.split('_') if t]
    if tokens and tokens[0] == 'no':
        tokens = tokens[1:]
    kept = [t for t in tokens if t not in _NOISE]
    return ' '.join(kept or tokens)


def _derived_fields() -> set:
    """Fields some extractor populates.

    Delegates rather than parsing here. Counting only literal ``data['x'] =``
    assignments called 24 of BehaviorTab's 27 fields unread when they are in fact
    derived through lookup tables -- which turned this command's "[no extractor]"
    warning into noise on exactly the fields it was meant to protect.
    """
    from omop_core.services.patient_record_service import derived_fields

    return set(derived_fields())



def _meaning_warnings(search_text: str, concept_name: str) -> list:
    """Places where a high lexical score most often hides a different meaning.

    Trigram similarity compares spelling, not sense, and the failure that matters
    is not a low score -- it is a high one on the wrong concept.
    ``clonal_plasma_cells`` matches "Polyclonal plasma cells" at 0.75 while
    meaning the opposite of it, because "polyclonal" contains "clonal".

    Flagging the shape rather than trying to judge the medicine: a reviewer reads
    the two names and decides.
    """
    warnings = []
    searched = set(search_text.split())
    for word in concept_name.lower().replace(',', ' ').split():
        for token in searched:
            if len(token) > 3 and token in word and token != word:
                warnings.append(
                    f'"{word}" merely contains "{token}" — check this is not the '
                    f'opposite concept'
                )
    return sorted(set(warnings))


class Command(BaseCommand):
    help = 'Propose (never approve) concept mappings for unmapped PatientRecord fields.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be proposed; write nothing.')
        parser.add_argument('--emit-candidates', metavar='PATH',
                            help='Write the retrieved shortlist per field as JSON '
                                 'and stop. Nothing is proposed.')
        parser.add_argument('--from-reviewed', action='store_true',
                            help='Seed the judged choices in suggested_mappings.py '
                                 'rather than the top lexical match.')
        parser.add_argument('--derived-only', action='store_true',
                            help='Only fields derivation already reads back — the '
                                 'ones where a mapping row is the whole fix.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Only consider the first N unmapped fields.')
        parser.add_argument('--min-score', type=float, default=_MIN_SCORE)
        parser.add_argument('--field', action='append', default=[],
                            help='Restrict to these fields (repeatable).')

    def handle(self, *args, **options):
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        descriptor = build_writable_field_descriptor()
        derived = _derived_fields()

        wanted = set(options['field'])
        fields = [f for f, e in sorted(descriptor.items())
                  if e['kind'] == 'unmapped' and (not wanted or f in wanted)]
        if options['derived_only']:
            fields = [f for f in fields if f in derived]
        if options['limit']:
            fields = fields[:options['limit']]

        if options['emit_candidates']:
            return self._emit(fields, derived, options)
        if options['from_reviewed']:
            return self._seed_reviewed(fields, derived, options)

        # A reviewer's decision outranks anything suggested here, so rows that
        # have been approved or rejected are never touched.
        settled = set(
            FieldConceptMapping.objects
            .exclude(status='proposed')
            .values_list('field_name', flat=True)
        )

        proposed = skipped = no_match = 0
        for field in fields:
            if field in settled:
                skipped += 1
                continue

            candidates = self._candidates(_search_text(field), options['min_score'])
            if not candidates:
                no_match += 1
                self.stdout.write(f'  {field:<44} no candidate')
                continue

            best = candidates[0]
            reads_back = field in derived
            self.stdout.write(
                f'  {field:<44} {best["score"]:.2f}  '
                f'{best["vocabulary_id"]} {best["concept_code"]}  '
                f'{best["concept_name"][:44]}'
                f'{"" if reads_back else "   [no extractor]"}'
            )
            if not options['dry_run']:
                self._propose(field, best, candidates[1:4], reads_back)
            proposed += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'proposed {proposed}, no candidate {no_match}, '
            f'left alone (already reviewed) {skipped}'
            + ('   [dry run — nothing written]' if options['dry_run'] else '')
        ))

    def _emit(self, fields, derived, options):
        """Write the shortlist per field, for judging. Proposes nothing."""
        import json

        out = {}
        for field in fields:
            text = _search_text(field)
            out[field] = {
                'search_text': text,
                'derivation_reads_it_back': field in derived,
                'candidates': [
                    {k: (float(v) if k == 'score' else v) for k, v in c.items()}
                    for c in self._candidates(text, options['min_score'])
                ],
            }
        with open(options['emit_candidates'], 'w') as fh:
            json.dump(out, fh, indent=2)
        with_any = sum(1 for v in out.values() if v['candidates'])
        self.stdout.write(self.style.SUCCESS(
            f'wrote {len(out)} fields ({with_any} with candidates) '
            f'to {options["emit_candidates"]}'
        ))

    def _seed_reviewed(self, fields, derived, options):
        """Seed the judged choices — a decision about meaning, not spelling."""
        from omop_core.suggested_mappings import REVIEWED_SUGGESTIONS

        settled = set(
            FieldConceptMapping.objects
            .exclude(status='proposed')
            .values_list('field_name', flat=True)
        )
        seeded = missing = 0
        for field in fields:
            choice = REVIEWED_SUGGESTIONS.get(field)
            if choice is None or field in settled:
                continue
            concept = Concept.objects.filter(
                vocabulary_id=choice['vocabulary_id'],
                concept_code=choice['concept_code'],
            ).first()
            if concept is None:
                missing += 1
                self.stdout.write(
                    f'  {field:<42} {choice["vocabulary_id"]} '
                    f'{choice["concept_code"]} NOT LOADED'
                )
                continue
            self.stdout.write(
                f'  {field:<42} {concept.vocabulary_id} {concept.concept_code}  '
                f'{concept.concept_name[:40]}'
                f'{"" if field in derived else "   [no extractor]"}'
            )
            if not options['dry_run']:
                self._propose_reviewed(field, concept, choice, field in derived)
            seeded += 1
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'seeded {seeded} reviewed suggestions, {missing} concept not loaded'
            + ('   [dry run — nothing written]' if options['dry_run'] else '')
        ))

    @transaction.atomic
    def _propose_reviewed(self, field, concept, choice, reads_back):
        note = (
            f'SUGGESTED, NOT REVIEWED BY A CLINICIAN. Chosen for meaning rather '
            f'than spelling: {choice["rationale"]} '
        )
        if not reads_back:
            note += (
                'WARNING: nothing in patient_record_service assigns this field, '
                'so approving this would produce a write whose value never comes '
                'back (#648). Write the extractor first. '
            )
        note += 'Set source_value before approving; derivation matches on it.'

        FieldConceptMapping.objects.update_or_create(
            field_name=field,
            defaults={
                'concept': concept,
                'vocabulary_id': concept.vocabulary_id,
                'concept_code': concept.concept_code,
                'omop_table': choice.get('omop_table', 'observation'),
                'value_kind': choice.get('value_kind', ''),
                'source_value': '',
                'status': 'proposed',
                'notes': note,
            },
        )

    def _candidates(self, text, min_score):
        """Best-scoring concepts for a phrase, by trigram similarity.

        Similarity rather than substring match: "tobacco smoking" should find
        "Tobacco smoking status" without either containing the other exactly, and
        an ordering is what makes the result reviewable.
        """
        if not text:
            return []
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT concept_id, concept_name, vocabulary_id, concept_code,
                       domain_id, similarity(LOWER(concept_name), %s) AS score
                FROM concept
                WHERE vocabulary_id = ANY(%s)
                  AND domain_id = ANY(%s)
                  AND (invalid_reason IS NULL OR invalid_reason = '')
                  -- Standard concepts only. LOINC LP codes are parts, meant for
                  -- composing a term rather than being one, and a non-standard
                  -- concept is not what a fact should point at.
                  AND standard_concept = 'S'
                  AND LOWER(concept_name) %% %s
                ORDER BY score DESC, LENGTH(concept_name) ASC
                LIMIT 8
                """,
                [text, list(_VOCABULARIES), list(_DOMAINS), text],
            )
            rows = cur.fetchall()
        keys = ('concept_id', 'concept_name', 'vocabulary_id', 'concept_code',
                'domain_id', 'score')
        return [dict(zip(keys, r)) for r in rows if r[5] >= min_score]

    @transaction.atomic
    def _propose(self, field, best, runners_up, reads_back):
        alternatives = '; '.join(
            f'{c["vocabulary_id"]} {c["concept_code"]} {c["concept_name"]} ({c["score"]:.2f})'
            for c in runners_up
        ) or 'none above threshold'

        # A search result, not a recommendation. The score measures spelling.
        note = (
            f'SUGGESTED, NOT REVIEWED. Trigram score {best["score"]:.2f} against '
            f'"{_search_text(field)}" — this measures spelling, not meaning, so '
            f'read the concept name before approving. '
            f'Also considered: {alternatives}. '
        )
        for warning in _meaning_warnings(_search_text(field), best['concept_name']):
            note += f'CAUTION: {warning}. '
        if reads_back:
            note += (
                'Derivation reads this field, so approving a correct mapping '
                'should make it writable and round-trip.'
            )
        else:
            note += (
                'WARNING: nothing in patient_record_service assigns this field, '
                'so approving this mapping would produce a write whose value '
                'never comes back (#648). Write the extractor first.'
            )

        FieldConceptMapping.objects.update_or_create(
            field_name=field,
            defaults={
                'concept': Concept.objects.filter(concept_id=best['concept_id']).first(),
                'vocabulary_id': best['vocabulary_id'],
                'concept_code': best['concept_code'],
                'omop_table': ('measurement' if best['domain_id'] == 'Measurement'
                               else 'observation'),
                # Deliberately blank. Derivation matches on this, and guessing it
                # is what would turn a reviewable suggestion into a silent
                # write-into-a-void. The reviewer sets it.
                'source_value': '',
                'status': 'proposed',
                'notes': note,
            },
        )
