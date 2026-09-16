"""Import the reviewed, generated HealthTree crossmap artifact into SCCM."""
import json
from collections import defaultdict
from pathlib import Path

from django.core.management.base import CommandError
from django.db import transaction
from omop_core.management.embedding_command import EmbeddingLoadCommand

from omop_core.models import Concept, SourceCodeConceptMapping, MappingDestinationCandidate
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


DEFAULT_ARTIFACT = Path(__file__).resolve().parents[3] / 'docs' / 'ht-code-concept-mapping.md'


class Command(EmbeddingLoadCommand):
    help = 'Bulk-import the generated HealthTree crossmap artifact into SCCM.'

    def add_arguments(self, parser):
        parser.add_argument('--one-root', help='Read all resolver destinations directly from HealthTree One.')
        parser.add_argument('--artifact', default=str(DEFAULT_ARTIFACT), help='Generated HealthTree Markdown or JSON artifact path.')
        parser.add_argument('--dry-run', action='store_true', help='Report without writing rows.')
        parser.add_argument('--limit', type=int, default=0, help='Maximum artifact rows to process (0 = all).')
        parser.add_argument('--batch-size', type=int, default=1000, help='Rows per bulk insert batch.')

    def handle(self, **options):
        try:
            if options.get('one_root'):
                from .build_healthtree_crossmap_artifact import Command as Builder, FHIR_ROOT
                mappings = list(Builder()._read_project(Path(options['one_root']) / FHIR_ROOT))
                for row in mappings:
                    row.update(status='approved', origins=['HT-One'])
            else:
                mappings = self._read_artifact(Path(options['artifact']))
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f'Cannot read HealthTree mappings: {exc}') from exc

        # Group by source, but retain every distinct destination. An ambiguous
        # import has no automatically chosen winner; the curator must choose.
        grouped = defaultdict(dict)
        metadata = {}
        for row in mappings:
            key = (row['source_vocabulary_id'], row['source_code'])
            metadata.setdefault(key, row)
            for candidate in row.get('candidates') or [row]:
                target_key = (candidate['target_vocabulary_id'], str(candidate['target_concept_code']))
                dest = grouped[key].setdefault(target_key, {'origins': set()})
                dest['origins'].update(candidate.get('origins', row.get('origins', ['HT-One'])))
        if options['limit']:
            grouped = dict(list(grouped.items())[:options['limit']])
        target_keys = {target for candidates in grouped.values() for target in candidates}
        targets = {
            (c.vocabulary_id, c.concept_code): c
            for c in Concept.objects.filter(
                vocabulary_id__in={v for v, _ in target_keys},
                concept_code__in={c for _, c in target_keys},
            ).only('concept_id', 'concept_code', 'vocabulary_id', 'domain_id',
                   'standard_concept', 'invalid_reason')
        }
        source_ids = {r.get('source_concept_id') for r in metadata.values()} - {None}
        sources = set(Concept.objects.filter(concept_id__in=source_ids).values_list('concept_id', flat=True))
        source_filter = dict(
            source_vocabulary_id__in={v for v, _ in grouped},
            source_code__in={c for _, c in grouped},
        )
        stats = {'created': 0, 'existing': 0, 'destinations': 0, 'unavailable': 0}
        with transaction.atomic():
            existing = {(m.source_vocabulary_id, m.source_code): m
                        for m in SourceCodeConceptMapping.objects.filter(**source_filter)}
            pending = []
            for key, candidates in grouped.items():
                row = metadata[key]
                stats['destinations'] += len(candidates)
                stats['unavailable'] += sum(
                    target not in targets or targets[target].standard_concept != 'S'
                    or bool(targets[target].invalid_reason) for target in candidates
                )
                if key in existing:
                    stats['existing'] += 1
                    continue
                target = targets.get(next(iter(candidates))) if len(candidates) == 1 else None
                if target and (target.standard_concept != 'S' or target.invalid_reason):
                    target = None
                domain = target.domain_id if target else row['domain_id']
                origins = sorted({o for c in candidates.values() for o in c['origins']})
                origin = 'HT-One' if 'HT-One' in origins else (origins[0] if origins else 'HT-One')
                pending.append(SourceCodeConceptMapping(
                    source_vocabulary_id=key[0], source_code=key[1],
                    domain_id=domain, source_code_description=row.get('source_code_description', ''),
                    source_concept_id=row.get('source_concept_id') if row.get('source_concept_id') in sources else None,
                    target_concept=target,
                    destination_vocabulary_id=target.vocabulary_id if target else '',
                    omop_table=DOMAIN_TO_TABLE.get(domain, ''),
                    status=row.get('status', 'approved') if target else 'proposed',
                    origin='import', origin_system=origin, source=origin,
                ))
                stats['created'] += 1
            if not options['dry_run']:
                SourceCodeConceptMapping.objects.bulk_create(pending, batch_size=options['batch_size'], ignore_conflicts=True)
                stored = {(m.source_vocabulary_id, m.source_code): m
                          for m in SourceCodeConceptMapping.objects.filter(**source_filter)}
                prior = {(c.mapping_id, c.target_vocabulary_id, c.target_concept_code): c
                         for c in MappingDestinationCandidate.objects.filter(mapping_id__in=[m.pk for m in stored.values()])}
                additions, updates = [], []
                for key, candidates in grouped.items():
                    mapping = stored[key]
                    for (vocab, code), candidate in candidates.items():
                        target = targets.get((vocab, code))
                        target_id = target.pk if target else None
                        old = prior.get((mapping.pk, vocab, code))
                        origins = sorted(set(candidate['origins']) | set(old.origins if old else []))
                        if old:
                            if old.target_concept_id != target_id or old.origins != origins:
                                old.target_concept_id, old.origins = target_id, origins
                                updates.append(old)
                        else:
                            additions.append(MappingDestinationCandidate(
                                mapping=mapping, target_vocabulary_id=vocab,
                                target_concept_code=code, target_concept_id=target_id, origins=origins,
                            ))
                MappingDestinationCandidate.objects.bulk_create(additions, batch_size=options['batch_size'], ignore_conflicts=True)
                MappingDestinationCandidate.objects.bulk_update(updates, ['target_concept', 'origins'], batch_size=options['batch_size'])
        verb = 'Would load' if options['dry_run'] else 'Loaded'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {stats["destinations"]:,} distinct destinations; '
            f'created {stats["created"]:,} source mappings; existing {stats["existing"]:,}; '
            f'unavailable targets {stats["unavailable"]:,} (retained for review).'
        ))

    @staticmethod
    def _read_artifact(path):
        text = path.read_text()
        if path.suffix == '.json':
            return json.loads(text)['mappings']
        mappings = []
        for line in text.splitlines():
            if not line.startswith('| ') or line.startswith('| ---') or 'Source system' in line:
                continue
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            if len(cells) != 8:
                continue
            source_vocab, source_code, target_vocab, target_code, domain, status, origins, _candidate_count = cells
            if int(_candidate_count) > 1:
                raise CommandError('Markdown omits alternative destinations. Use --one-root or a JSON artifact with candidates.')
            mappings.append({
                'source_vocabulary_id': source_vocab,
                'source_code': source_code.replace('\\|', '|'),
                'source_code_description': '',
                'source_concept_id': None,
                'target_vocabulary_id': target_vocab,
                'target_concept_code': target_code,
                'domain_id': domain,
                'status': status,
                'origins': [origin for origin in origins.split(', ') if origin],
            })
        return mappings
