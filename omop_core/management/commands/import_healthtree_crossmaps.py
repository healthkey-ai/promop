"""Import code-held HealthTree crosswalks into SourceCodeConceptMapping.

HealthTree One and HealthTree Next both contain the same curated resolver
datasets.  Keeping those mappings only in the applications makes them
invisible to PROMOP's mapping review UI and ingestion resolver.  This command
imports rows with their source-project provenance. A legacy ICD-10 entry can
name multiple SNOMED destinations even though SCCM permits one target per
source code. For those entries, the importer selects the candidate used most
often in HealthTree's dataset and marks the result proposed for review.

"""
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

PROJECTS = {
    'one': ('HT-One', Path.home() / 'one'),
    'next': ('HT-Next', Path.home() / 'healthtree-platform'),
}
FHIR_ROOT = Path('functions/main/firestore/apps/curehub')


class Command(BaseCommand):
    help = 'Import HealthTree One/Next code crosswalks into SCCM.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--project', choices=(*PROJECTS, 'all'), default='all',
            help='Project to import (default: all; HT-One is processed first).',
        )
        parser.add_argument('--one-root', help='Path to the HealthTree One repository.')
        parser.add_argument('--next-root', help='Path to the HealthTree Next repository.')
        parser.add_argument('--dry-run', action='store_true', help='Report without writing rows.')
        parser.add_argument('--limit', type=int, default=0, help='Maximum entries per dataset (0 = all).')

    def handle(self, **options):
        projects = ('one', 'next') if options['project'] == 'all' else (options['project'],)
        roots = {
            'one': Path(options['one_root']) if options['one_root'] else PROJECTS['one'][1],
            'next': Path(options['next_root']) if options['next_root'] else PROJECTS['next'][1],
        }
        for project in projects:
            origin, _default_root = PROJECTS[project]
            root = roots[project]
            if not root.is_dir():
                raise CommandError(f'HealthTree {project} repository not found: {root}')
            self.stdout.write(f'Importing {origin} crosswalks from {root}')
            self._import_project(root / FHIR_ROOT, origin, options)

    def _import_project(self, root, origin, options):
        datasets = (
            (
                'ICD10→SNOMED', root / 'FHIR/resourcesTypes/r4/Condition/_icd10ToSnomedMappings.json',
                self._icd10_entries,
            ),
            ('CPT4→SNOMED', root / 'FHIR/codeSystems/cptToSnomedMap.json', self._cpt_entries),
            ('SNOMED→RxNorm', root / 'FHIR/codeSystems/snomedToRxNormMap.json', self._rxnorm_entries),
            (
                'MedDRA→SNOMED',
                root / 'medicalResources/_DocumentReferenceAI/linesOfTherapy/adverseEvents/_utils/MDRToSnomed.json',
                self._meddra_entries,
            ),
        )
        for label, path, entry_builder in datasets:
            data = self._load_json(path)
            entries, proposed = entry_builder(data)
            if options['limit']:
                entries = entries[:options['limit']]
            stats = self._import_entries(entries, origin, options['dry_run'])
            suffix = f'; proposed {proposed:,} ambiguous selections' if proposed else ''
            verb = 'Would create' if options['dry_run'] else 'Created'
            self.stdout.write(
                f'{origin} {label}: {verb} {stats["created"]:,}; '
                f'existing {stats["existing"]:,}; missing target {stats["missing"]:,}'
                f'{suffix}.'
            )

    @staticmethod
    def _load_json(path):
        try:
            with path.open() as source:
                return json.load(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Cannot read {path}: {exc}') from exc

    @staticmethod
    def _icd10_entries(data):
        usage = Counter(destination for destinations in data.values() for destination in destinations)
        entries = []
        proposed = 0
        for code, destinations in data.items():
            if not destinations:
                continue
            if len(destinations) == 1:
                target = destinations[0]
                status = 'approved'
            else:
                target = min(destinations, key=lambda candidate: (-usage[candidate], str(candidate)))
                status = 'proposed'
                proposed += 1
            entries.append(('ICD10', code, target, 'SNOMED', 'Condition', '', None, status))
        return entries, proposed

    @staticmethod
    def _cpt_entries(data):
        entries = []
        for entry in data.values():
            destination = entry.get('snomedId')
            if destination and destination != '0':
                entries.append((
                    'CPT4', entry.get('cptCode', ''), destination, 'SNOMED', 'Procedure',
                    entry.get('cptDescriptor', ''), entry.get('cptConceptId'), 'approved',
                ))
        return entries, 0

    @staticmethod
    def _rxnorm_entries(data):
        return [
            ('SNOMED', source, destination, 'RxNorm', 'Drug', '', None, 'approved')
            for source, destination in data.items()
        ], 0

    @staticmethod
    def _meddra_entries(data):
        return [
            ('MedDRA', entry['mdr_code'], entry['snomed_code'], 'SNOMED', 'Condition',
             entry.get('mdr_name', ''), None, 'approved')
            for entry in data if entry.get('mdr_code') and entry.get('snomed_code')
        ], 0

    def _import_entries(self, entries, origin, dry_run):
        target_codes = {}
        for (
            _source_vocab, _source_code, target_code, target_vocab,
            _domain, _description, _source_id, _status,
        ) in entries:
            target_codes.setdefault(target_vocab, set()).add(str(target_code))
        all_target_codes = set().union(*target_codes.values()) if target_codes else set()
        targets = {
            (concept.vocabulary_id, concept.concept_code): concept
            for concept in Concept.objects.filter(
                vocabulary_id__in=target_codes,
                standard_concept='S',
            ).filter(concept_code__in=all_target_codes).only(
                'concept_id', 'concept_code', 'vocabulary_id', 'domain_id',
            )
        }
        cpt_ids = {
            int(source_id) for *_prefix, source_id, _status in entries
            if source_id and str(source_id).isdigit()
        }
        sources = {
            concept.concept_id: concept
            for concept in Concept.objects.filter(concept_id__in=cpt_ids).only('concept_id')
        }
        stats = {'created': 0, 'existing': 0, 'missing': 0}
        for (
            source_vocab, source_code, target_code, target_vocab, domain,
            description, source_id, status,
        ) in entries:
            target = targets.get((target_vocab, str(target_code)))
            if not target:
                stats['missing'] += 1
                continue
            if dry_run:
                stats['created'] += 1
                continue
            _mapping, created = SourceCodeConceptMapping.objects.get_or_create(
                source_vocabulary_id=source_vocab,
                source_code=str(source_code)[:100],
                defaults={
                    'domain_id': target.domain_id or domain,
                    'source_code_description': str(description or '')[:255],
                    'source_concept': (
                        sources.get(int(source_id))
                        if source_id and str(source_id).isdigit() else None
                    ),
                    'target_concept': target,
                    'destination_vocabulary_id': target_vocab,
                    'omop_table': DOMAIN_TO_TABLE.get(target.domain_id or domain, ''),
                    'status': status,
                    'origin': 'import',
                    'origin_system': origin,
                    'source': origin,
                    'occurrence_count': 0,
                },
            )
            stats['created' if created else 'existing'] += 1
        return stats
