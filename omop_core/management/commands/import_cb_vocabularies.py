"""Seed the Code Mapping queue with CancerBot's vocabulary terms (#1649).

Every row lands as ``proposed``. CB's proposal becomes the row's destination
when it passes ``destination_problem``; every destination CB named -- the
proposal, each member of a concept set, the value concept of a
measurement/value pair -- is kept as a MappingDestinationCandidate, so the
reviewer sees all of them whichever one is chosen.

Re-running is safe. A row a curator has already approved or rejected is never
touched, and neither is the destination of a row still in review: after the
first import the destination belongs to the reviewer. What a re-run does add
is new candidates and a refreshed trial count. Terms that are in the queue but
missing from the file are reported, never deleted: whether a term was retired
is CB's call, and the export tells CB what the queue still holds.

Dry run unless ``--apply``.
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

        concepts = resolve_concepts(k for row in rows for k in row.candidate_keys())
        stats = {'created': 0, 'refreshed': 0, 'decided_kept': 0, 'with_destination': 0,
                 'without_destination': 0, 'rejected_proposals': 0, 'candidates_added': 0,
                 'candidates_unavailable': 0}
        problems = []

        with transaction.atomic():
            existing = {
                m.source_code.lower(): m
                for m in SourceCodeConceptMapping.objects.filter(source_vocabulary_id=SOURCE_VOCABULARY_ID)
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
                stats['with_destination' if target else 'without_destination'] += 1
                stats['candidates_unavailable'] += sum(k not in concepts for k in row.candidate_keys())
                domain = (target.domain_id if target else row.domain_id) or ''

                mapping = existing.get(row.source_code.lower())
                if mapping is not None and mapping.status != 'proposed':
                    stats['decided_kept'] += 1
                    continue
                if mapping is None:
                    stats['created'] += 1
                    if not options['apply']:
                        continue
                    mapping = SourceCodeConceptMapping.objects.create(
                        source_vocabulary_id=SOURCE_VOCABULARY_ID,
                        source_code=row.source_code,
                        source_code_description=row.description,
                        domain_id=domain if domain in DOMAIN_TO_TABLE else '',
                        omop_table=DOMAIN_TO_TABLE.get(domain, ''),
                        target_concept=target,
                        destination_vocabulary_id=target.vocabulary_id if target else '',
                        source=MAPPING_SOURCE,
                        status='proposed',
                        origin='import',
                        origin_system=ORIGIN_SYSTEM,
                        occurrence_count=row.trial_count,
                        notes=_notes(row, problem),
                    )
                else:
                    stats['refreshed'] += 1
                    if not options['apply']:
                        continue
                    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(
                        source_code_description=row.description,
                        occurrence_count=row.trial_count,
                    )

                prior = {(c.target_vocabulary_id, c.target_concept_code): c
                         for c in mapping.destination_candidates.all()}
                for key, origins in row.candidate_keys().items():
                    concept = concepts.get(key)
                    old = prior.get((key.vocabulary_id, key.concept_code))
                    if old is None:
                        MappingDestinationCandidate.objects.create(
                            mapping=mapping, target_vocabulary_id=key.vocabulary_id,
                            target_concept_code=key.concept_code, target_concept=concept,
                            origins=sorted(origins),
                        )
                        stats['candidates_added'] += 1
                    elif set(old.origins) | origins != set(old.origins):
                        old.origins = sorted(set(old.origins) | origins)
                        old.save(update_fields=['origins'])

            if not options['apply']:
                transaction.set_rollback(True)

        verb = 'Imported' if options['apply'] else 'Dry run -- would import'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {len(rows):,} CB terms: ' + ', '.join(f'{k}={v:,}' for k, v in stats.items())))
        for line in problems:
            self.stdout.write(f'  proposal not used: {line}')
        if absent:
            self.stdout.write(self.style.WARNING(
                f'{len(absent):,} CB terms are in the queue but not in the file (left untouched):'))
            for code in absent:
                self.stdout.write(f'  absent: {code}')
