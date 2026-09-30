"""Seed the Code Mapping queue with CancerBot's vocabulary terms (#1649).

Every row lands as ``proposed``. CB's proposal becomes the row's destination
when it passes ``destination_problem``; every destination CB named -- the
proposal, each member of a concept set, the value concept of a
measurement/value pair -- is kept as a MappingDestinationCandidate, so the
reviewer sees all of them whichever one is chosen.

Re-running is safe. After the first import a row belongs to the reviewer:
a re-run never sets a destination, a description or a status. It refreshes
the trial count and the candidates -- adding new ones and re-resolving old
ones, so a vocabulary loaded since the last run shows -- and only on rows
still in review that still carry this importer's provenance. A row a curator
has approved, rejected or cleared, or re-pointed while it stayed proposed, is
left as it is. A row without a destination whose proposal has since become
usable is listed, for a person to pick.

Deleting a CB row in the UI clears it rather than removing it (catalog
sources are never hard-deleted), so the cleared row stays and the importer
keeps skipping it. Whether a term exists at all is CB's decision, made in the
file; ``rejected`` is how a reviewer says it has no mapping.

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
            ('created', 'refreshed', 'destination_available', 'kept', 'rejected_proposals',
             'candidates_added', 'candidates_updated', 'candidates_unavailable'), 0)
        problems, available = [], []

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
                # A clear through the UI blanks origin_system and a curator's
                # re-point replaces it: either way the row is no longer ours.
                if mapping is not None and (mapping.status != 'proposed'
                                            or mapping.origin_system != ORIGIN_SYSTEM):
                    stats['kept'] += 1
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
                    if mapping.target_concept_id is None and target is not None:
                        stats['destination_available'] += 1
                        available.append(f'{row.source_code}: {row.proposal}')
                    if apply:
                        SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(
                            occurrence_count=row.trial_count)

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
        for line in available:
            self.stdout.write(f'  destination now available, pick it in the UI: {line}')
        if absent:
            self.stdout.write(self.style.WARNING(
                f'{len(absent):,} CB terms are in the queue but not in the file (left untouched):'))
            for code in absent:
                self.stdout.write(f'  absent: {code}')
