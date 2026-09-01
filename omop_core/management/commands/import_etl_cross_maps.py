"""Import ETL cross-vocabulary maps into SourceCodeConceptMapping.

The ETL repo carries two static JSON files for concept resolution:

  - cpt_to_snomed_map.json — CPT4 procedure codes → SNOMED concept IDs
  - snomed_to_rxnorm_map.json — SNOMED medication codes → RxNorm codes

This command reads those files and loads them as approved
SourceCodeConceptMapping rows so curators can see, review, and update
them through the Mapping Hub UI, and the ETL can eventually query
promop's API instead of carrying its own static files.

Re-runnable: existing SCCM rows are left untouched (get_or_create).
When an existing row maps to a different target than the ETL file,
a WARNING is logged so a curator can review.
"""
import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand

from omop_core.models import Concept, SourceCodeConceptMapping

logger = logging.getLogger(__name__)

DEFAULT_CPT_SNOMED = Path.home() / 'etl/airflow/dags/services/fhir_parsing/codesystems/cpt_to_snomed_map.json'
DEFAULT_SNOMED_RXNORM = Path.home() / 'etl/airflow/dags/services/fhir_parsing/codesystems/snomed_to_rxnorm_map.json'


class Command(BaseCommand):
    help = 'Import ETL cross-vocabulary maps (CPT→SNOMED, SNOMED→RxNorm) into SourceCodeConceptMapping.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cpt-snomed-file', type=str, default=str(DEFAULT_CPT_SNOMED),
            help='Path to cpt_to_snomed_map.json',
        )
        parser.add_argument(
            '--snomed-rxnorm-file', type=str, default=str(DEFAULT_SNOMED_RXNORM),
            help='Path to snomed_to_rxnorm_map.json',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report counts without writing.',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Max rows to process per file (0 = unlimited).',
        )
        parser.add_argument(
            '--skip-cpt', action='store_true',
            help='Skip CPT→SNOMED import.',
        )
        parser.add_argument(
            '--skip-rxnorm', action='store_true',
            help='Skip SNOMED→RxNorm import.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        limit = options['limit']

        if not options['skip_cpt']:
            self._import_cpt_snomed(options['cpt_snomed_file'], dry_run, limit)

        if not options['skip_rxnorm']:
            self._import_snomed_rxnorm(options['snomed_rxnorm_file'], dry_run, limit)

    # Domain → OMOP table, matching sync_athena_mappings.DOMAIN_TO_TABLE.
    DOMAIN_TO_TABLE = {
        'Drug': 'drug_exposure',
        'Procedure': 'procedure',
        'Condition': 'condition',
        'Observation': 'observation',
        'Measurement': 'measurement',
    }

    def _import_cpt_snomed(self, file_path, dry_run, limit):
        path = Path(file_path)
        if not path.exists():
            self.stderr.write(self.style.ERROR(f'CPT→SNOMED file not found: {path}'))
            return

        with open(path) as f:
            data = json.load(f)

        entries = list(data.values())
        if limit:
            entries = entries[:limit]

        # Filter to entries that have a mapping (snomedId != '0').
        mapped_entries = []
        no_mapping = 0
        for entry in entries:
            snomed_id_str = entry.get('snomedId', '0')
            if snomed_id_str == '0' or not snomed_id_str:
                no_mapping += 1
            else:
                mapped_entries.append(entry)

        # Pre-fetch all target SNOMED concepts in one query.
        target_ids = {int(e['snomedId']) for e in mapped_entries}
        target_concepts = {
            c.concept_id: c
            for c in Concept.objects.filter(concept_id__in=target_ids)
        }

        # Pre-fetch all source CPT concepts in one query.
        source_ids = {int(e['cptConceptId']) for e in mapped_entries}
        source_concepts = {
            c.concept_id: c
            for c in Concept.objects.filter(concept_id__in=source_ids)
        }

        created = 0
        skipped = 0
        conflicts = 0
        missing_target = 0

        for entry in mapped_entries:
            target_concept_id = int(entry['snomedId'])
            source_concept_id = int(entry['cptConceptId'])
            source_code = entry['cptCode'][:100]
            description = (entry.get('cptDescriptor') or '')[:255]

            target_concept = target_concepts.get(target_concept_id)
            if not target_concept:
                missing_target += 1
                if not dry_run:
                    logger.warning(
                        'CPT %s: target SNOMED concept_id %d not in DB',
                        source_code, target_concept_id,
                    )
                continue

            if dry_run:
                created += 1
                continue

            source_concept = source_concepts.get(source_concept_id)

            # Derive domain from target concept instead of hardcoding.
            domain_id = target_concept.domain_id or 'Procedure'
            omop_table = self.DOMAIN_TO_TABLE.get(domain_id, 'procedure')

            obj, was_created = SourceCodeConceptMapping.objects.get_or_create(
                source_vocabulary_id='CPT4',
                source_code=source_code,
                defaults={
                    'domain_id': domain_id,
                    'source_code_description': description,
                    'source_concept': source_concept,
                    'target_concept': target_concept,
                    'destination_vocabulary_id': 'SNOMED',
                    'omop_table': omop_table,
                    'status': 'approved',
                    'origin': 'import',
                    'origin_system': 'etl-cross-map',
                    'source': 'ETL',
                    'occurrence_count': 0,
                },
            )
            if was_created:
                created += 1
            else:
                skipped += 1
                if obj.target_concept_id and obj.target_concept_id != target_concept_id:
                    conflicts += 1
                    logger.warning(
                        'CPT %s: existing target_concept=%s differs from ETL target=%d',
                        source_code, obj.target_concept_id, target_concept_id,
                    )
                else:
                    logger.debug('CPT %s: already mapped, skipping', source_code)

        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'CPT→SNOMED: {verb} {created} rows. '
            f'Skipped {skipped} existing, {no_mapping} unmapped (snomedId=0), '
            f'{missing_target} missing target concept, {conflicts} conflicts.'
        ))

    def _import_snomed_rxnorm(self, file_path, dry_run, limit):
        path = Path(file_path)
        if not path.exists():
            self.stderr.write(self.style.ERROR(f'SNOMED→RxNorm file not found: {path}'))
            return

        with open(path) as f:
            data = json.load(f)

        # Pre-fetch all RxNorm concepts to avoid N+1.
        # The JSON values are RxNorm concept codes, not concept_ids.
        rxnorm_codes = set(data.values())
        rxnorm_concepts = {
            c.concept_code: c
            for c in Concept.objects.filter(
                vocabulary_id='RxNorm',
                concept_code__in=rxnorm_codes,
            )
        }

        # Also pre-fetch SNOMED concepts for source_concept FK.
        snomed_codes = set(data.keys())
        snomed_concepts = {
            c.concept_code: c
            for c in Concept.objects.filter(
                vocabulary_id='SNOMED',
                concept_code__in=snomed_codes,
            )
        }

        created = 0
        skipped = 0
        conflicts = 0
        missing_target = 0

        entries = list(data.items())
        if limit:
            entries = entries[:limit]

        for snomed_code, rxnorm_code in entries:
            rxnorm_concept = rxnorm_concepts.get(rxnorm_code)
            if not rxnorm_concept:
                missing_target += 1
                if not dry_run:
                    logger.warning(
                        'SNOMED %s: RxNorm code %s not found in DB',
                        snomed_code, rxnorm_code,
                    )
                continue

            if dry_run:
                created += 1
                continue

            snomed_concept = snomed_concepts.get(snomed_code)

            obj, was_created = SourceCodeConceptMapping.objects.get_or_create(
                source_vocabulary_id='SNOMED',
                source_code=snomed_code[:100],
                defaults={
                    'domain_id': 'Drug',
                    'source_code_description': (snomed_concept.concept_name if snomed_concept else '')[:255],
                    'source_concept': snomed_concept,
                    'target_concept': rxnorm_concept,
                    'destination_vocabulary_id': 'RxNorm',
                    'omop_table': 'drug_exposure',
                    'status': 'approved',
                    'origin': 'import',
                    'origin_system': 'etl-cross-map',
                    'source': 'ETL',
                    'occurrence_count': 0,
                },
            )
            if was_created:
                created += 1
            else:
                skipped += 1
                if obj.target_concept_id and obj.target_concept_id != rxnorm_concept.concept_id:
                    conflicts += 1
                    logger.warning(
                        'SNOMED %s: existing target_concept=%s differs from ETL RxNorm concept=%d',
                        snomed_code, obj.target_concept_id, rxnorm_concept.concept_id,
                    )
                else:
                    logger.debug('SNOMED %s: already mapped, skipping', snomed_code)

        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'SNOMED→RxNorm: {verb} {created} rows. '
            f'Skipped {skipped} existing, {missing_target} missing target concept, '
            f'{conflicts} conflicts.'
        ))
