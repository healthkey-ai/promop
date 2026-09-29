"""Seed the Code Mapping queue with CancerBot's vocabulary terms (#1649).

Every row lands as ``proposed``. CB's proposal becomes the row's destination
when it passes ``destination_problem``; every destination CB named -- the
proposal, each member of a concept set, the value concept of a
measurement/value pair -- is kept as a MappingDestinationCandidate, so the
reviewer sees all of them whichever one is chosen.

Re-running is safe:

* a row a curator has approved or rejected is not touched, nor are its
  candidates;
* a row still in review that a person has edited (``updated_by`` set) keeps its
  destination and description; only its trial count and candidates refresh;
* a row nobody has edited is brought up to date: its description follows the
  CB title, and a destination that could not be resolved before is filled in
  once the concept is on this instance;
* candidates are re-resolved, so a vocabulary loaded since the last run shows.

Terms that are in the queue but missing from the file are reported, never
deleted: whether a term was retired is CB's call, and the export tells CB what
the queue still holds.

Dry run unless ``--apply``; the dry run computes the same counts.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import MappingDestinationCandidate, SourceCodeConceptMapping
from omop_core.services.cb_vocabulary import (
    MAPPING_SOURCE, ORIGIN_SYSTEM, SOURCE_VOCABULARY_ID,
    destination_problem, read_proposals, resolve_concepts,
)
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


def _notes(row, problem):
    parts = [f'CB proposal: match={row.match or "-"}, confidence={row.confidence or "-"}, '
             f'verified={row.verified or "-"}']
    if row.proposal:
        parts.append(f'proposed {row.proposal}')
    if problem:
        parts.append(f'not used as destination: {problem}')
    if row.note:
        parts.append(row.note)
    return '\n'.join(parts)


def _destination_fields(target, row):
    domain = (target.domain_id if target else row.domain_id) or ''
    domain = domain if domain in DOMAIN_TO_TABLE else ''
    return {
        'target_concept': target,
        'destination_vocabulary_id': target.vocabulary_id if target else '',
        'domain_id': domain,
        'omop_table': DOMAIN_TO_TABLE.get(domain, ''),
    }


class Command(BaseCommand):
    help = "Seed the Code Mapping queue with CancerBot's vocabulary terms (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help="CB's proposal CSV.")
        parser.add_argument('--apply', action='store_true', help='Write. Without it, report only.')

    def handle(self, **options):
        try:
            rows = read_proposals(options['file'])
        except (OSError, ValueError) as exc:
            raise CommandError(f'Cannot read CB proposals: {exc}') from exc
        apply = options['apply']

        concepts = resolve_concepts(k for row in rows for k in row.candidate_keys())
        stats = dict.fromkeys(
            ('created', 'refreshed', 'destination_filled', 'decided_kept', 'rejected_proposals',
             'candidates_added', 'candidates_updated', 'candidates_unavailable'), 0)
        problems = []

        with transaction.atomic():
            existing = {
                m.source_code.lower(): m
                for m in SourceCodeConceptMapping.objects
                .filter(source_vocabulary_id=SOURCE_VOCABULARY_ID)
                .prefetch_related('destination_candidates')
            }
            file_keys = {row.source_code.lower() for row in rows}
            absent = sorted(m.source_code for key, m in existing.items() if key not in file_keys)

            for row in rows:
                target = concepts.get(row.proposal) if row.proposal else None
                problem = destination_problem(target) if row.proposal else ''
                if problem:
                    stats['rejected_proposals'] += 1
                    problems.append(f'{row.source_code}: {row.proposal} {problem}')
                    target = None
                stats['candidates_unavailable'] += sum(k not in concepts for k in row.candidate_keys())

                mapping = existing.get(row.source_code.lower())
                if mapping is not None and mapping.status != 'proposed':
                    stats['decided_kept'] += 1
                    continue

                if mapping is None:
                    stats['created'] += 1
                    prior = {}
                    if apply:
                        mapping = SourceCodeConceptMapping.objects.create(
                            source_vocabulary_id=SOURCE_VOCABULARY_ID,
                            source_code=row.source_code,
                            source_code_description=row.description,
                            source=MAPPING_SOURCE,
                            status='proposed',
                            origin='import',
                            origin_system=ORIGIN_SYSTEM,
                            occurrence_count=row.trial_count,
                            notes=_notes(row, problem),
                            **_destination_fields(target, row),
                        )
                else:
                    stats['refreshed'] += 1
                    prior = {(c.target_vocabulary_id, c.target_concept_code): c
                             for c in mapping.destination_candidates.all()}
                    updates = {'occurrence_count': row.trial_count}
                    if mapping.updated_by_id is None:
                        updates['source_code_description'] = row.description
                        if mapping.target_concept_id is None and target is not None:
                            stats['destination_filled'] += 1
                            updates.update(_destination_fields(target, row))
                            updates['notes'] = _notes(row, problem)
                    if apply:
                        SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(**updates)

                for key, origins in row.candidate_keys().items():
                    concept = concepts.get(key)
                    old = prior.get((key.vocabulary_id, key.concept_code))
                    if old is None:
                        stats['candidates_added'] += 1
                        if apply:
                            MappingDestinationCandidate.objects.create(
                                mapping=mapping, target_vocabulary_id=key.vocabulary_id,
                                target_concept_code=key.concept_code, target_concept=concept,
                                origins=sorted(origins),
                            )
                        continue
                    merged = sorted(set(old.origins) | origins)
                    concept_id = concept.concept_id if concept else None
                    if merged != sorted(old.origins) or old.target_concept_id != concept_id:
                        stats['candidates_updated'] += 1
                        if apply:
                            old.origins, old.target_concept_id = merged, concept_id
                            old.save(update_fields=['origins', 'target_concept'])

        verb = 'Imported' if apply else 'Dry run -- would import'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {len(rows):,} CB terms: ' + ', '.join(f'{k}={v:,}' for k, v in stats.items())))
        for line in problems:
            self.stdout.write(f'  proposal not used: {line}')
        if absent:
            self.stdout.write(self.style.WARNING(
                f'{len(absent):,} CB terms are in the queue but not in the file (left untouched):'))
            for code in absent:
                self.stdout.write(f'  absent: {code}')
