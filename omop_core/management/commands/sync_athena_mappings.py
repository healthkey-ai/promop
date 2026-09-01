"""Sync Athena 'Maps to' relationships from concept_relationship into
SourceCodeConceptMapping.

FHIR importers resolve source codes via SourceCodeConceptMapping (SCCM).
Athena's concept_relationship (CR) table holds millions of 'Maps to' rows
that the resolver cannot see. This command imports the subset that matters
— source vocabularies we actually receive codes in — so that
resolve_source_code() has a single lookup table.

Re-runnable: safe to call after every vocabulary refresh.  Existing SCCM
rows are left untouched (get_or_create); new Athena relationships are
inserted as approved rows with origin_system='athena'.
"""
import logging

from django.core.management.base import BaseCommand

from omop_core.models import ConceptRelationship, SourceCodeConceptMapping

logger = logging.getLogger(__name__)

# Source vocabularies we receive codes in and want Athena mappings for.
# Standard vocabularies (SNOMED, LOINC) are excluded: their concepts are
# already standard and self-resolve, so importing their 'Maps to' edges
# would create millions of identity mappings that add no value.
SOURCE_VOCABULARIES = {
    'ICD10CM', 'ICD10', 'ICD10GM', 'ICD10CA', 'ICD9CM',
    'ICD10PCS', 'ICD9Proc',
    'CPT4', 'HCPCS', 'CDT',
    'RxNorm', 'RxNorm Extension', 'NDC', 'ATC',
    'HemOnc', 'Read', 'CTV3',
    'OPCS4', 'OPS', 'CCAM',
    'MedDRA', 'MeSH', 'Nebraska Lexicon',
    'dm+d', 'CVX', 'ICDO3',
}

# Domain → OMOP table, matching source_vocabularies.DOMAIN_TO_TABLE.
DOMAIN_TO_TABLE = {
    'Drug': 'drug_exposure',
    'Procedure': 'procedure',
    'Condition': 'condition',
    'Observation': 'observation',
    'Measurement': 'measurement',
}


class Command(BaseCommand):
    help = 'Sync Athena Maps-to relationships into SourceCodeConceptMapping.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created without writing.',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Max rows to process (0 = unlimited).',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        limit = options['limit']

        # Athena rows: source IS NULL (HealthKey-written rows have source='HealthKey').
        qs = (
            ConceptRelationship.objects
            .filter(relationship_id='Maps to', source__isnull=True)
            .filter(concept_1__vocabulary_id__in=SOURCE_VOCABULARIES)
            .select_related('concept_1', 'concept_2')
        )
        if limit:
            qs = qs[:limit]

        created_count = 0
        skipped_count = 0
        for cr in qs.iterator(chunk_size=2000):
            c1 = cr.concept_1
            c2 = cr.concept_2
            if not c1 or not c2:
                skipped_count += 1
                continue

            omop_table = DOMAIN_TO_TABLE.get(c2.domain_id, '')

            if dry_run:
                created_count += 1
                continue

            _obj, created = SourceCodeConceptMapping.objects.get_or_create(
                source_vocabulary_id=c1.vocabulary_id,
                source_code=c1.concept_code[:100],
                defaults={
                    'domain_id': c2.domain_id or '',
                    'source_code_description': (c1.concept_name or '')[:255],
                    'source_concept': c1,
                    'target_concept': c2,
                    'destination_vocabulary_id': c2.vocabulary_id or '',
                    'omop_table': omop_table,
                    'status': 'approved',
                    'origin': 'import',
                    'origin_system': 'athena',
                    'source': 'Athena',
                    'occurrence_count': 0,
                },
            )
            if created:
                created_count += 1
            else:
                skipped_count += 1

        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {created_count} SCCM rows from Athena CR. '
            f'Skipped {skipped_count} (already mapped or missing concept).'
        ))
