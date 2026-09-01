"""Sync Athena 'Maps to' relationships from concept_relationship into
SourceCodeConceptMapping.

FHIR importers resolve source codes via SourceCodeConceptMapping (SCCM).
Athena's concept_relationship (CR) table holds millions of 'Maps to' rows
that the resolver cannot see. This command imports the subset that matters
— source vocabularies we actually receive codes in — so that
resolve_source_code() has a single lookup table.

**Patient-scoped by default.** Only CR rows whose target concept already
appears in a clinical table for at least one patient are imported. This
keeps the SCCM table focused on concepts that matter for the current
patient population (oncology-heavy today) rather than importing hundreds of
thousands of mappings to concepts nobody has data for.  Pass ``--all`` to
import every CR mapping in the five clinical domains instead.

Re-runnable: safe to call after every vocabulary refresh.  Existing SCCM
rows are left untouched (get_or_create); new Athena relationships are
inserted as approved rows with origin_system='athena'.

1:N mappings (one source code -> multiple targets in CR) are handled by
keeping the first target encountered. SCCM enforces a unique constraint on
(source_vocabulary_id, source_code) because resolve_source_code() needs a
single answer. Additional targets for the same source code are logged at
WARNING so curators can review them.
"""
import logging
from collections import defaultdict

from django.db import connection
from django.core.management.base import BaseCommand

from omop_core.models import ConceptRelationship, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

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

# Maps each clinical table to the concept_id column that holds the
# standard concept for a fact row.
_CLINICAL_CONCEPT_COLUMNS = {
    'drug_exposure': 'drug_concept_id',
    'condition_occurrence': 'condition_concept_id',
    'measurement': 'measurement_concept_id',
    'observation': 'observation_concept_id',
    'procedure_occurrence': 'procedure_concept_id',
}


def _patient_scoped_concept_ids():
    """Concept IDs actually used by patients across the five clinical tables.

    Returns a set of integer concept_id values.  Concept 0 (no matching
    concept) is excluded — it is not a real mapping target.
    """
    ids = set()
    with connection.cursor() as cur:
        for table, col in _CLINICAL_CONCEPT_COLUMNS.items():
            cur.execute(
                f'SELECT DISTINCT {col} FROM {table} WHERE {col} != 0'  # noqa: S608
            )
            ids.update(row[0] for row in cur.fetchall())
    return ids


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
        parser.add_argument(
            '--all', action='store_true',
            help=(
                'Import all Athena mappings in the five clinical domains, '
                'not just those targeting concepts used by current patients. '
                'Default behaviour is patient-scoped: only CR rows whose '
                'target concept appears in at least one clinical table row '
                'are imported.'
            ),
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        limit = options['limit']
        import_all = options['all']
        clinical_domains = set(DOMAIN_TO_TABLE.keys())

        qs = (
            ConceptRelationship.objects
            .filter(relationship_id='Maps to')
            .filter(concept_1__vocabulary_id__in=SOURCE_VOCABULARIES)
            .filter(concept_2__domain_id__in=clinical_domains)
            .select_related('concept_1', 'concept_2')
        )

        # Patient-scoped filter (default): only import CR rows whose target
        # concept is already used by at least one patient.  This keeps SCCM
        # focused on the ~36K mappings that matter for the current oncology
        # population rather than all ~688K in the five clinical domains.
        used_concept_ids = None
        if not import_all:
            used_concept_ids = _patient_scoped_concept_ids()
            self.stdout.write(
                f'Patient-scoped: {len(used_concept_ids):,} distinct concepts '
                f'found across clinical tables.'
            )
            if not used_concept_ids:
                self.stdout.write(self.style.WARNING(
                    'No patient data found in clinical tables. '
                    'Nothing to import. Use --all to import all clinical-domain mappings.'
                ))
                return
            qs = qs.filter(concept_2_id__in=used_concept_ids)
        else:
            self.stdout.write(
                f'Importing all clinical-domain mappings: '
                f'{sorted(clinical_domains)}'
            )

        if limit:
            qs = qs[:limit]

        created_count = 0
        skipped_count = 0
        # Track source codes we've already seen in this run, so we can detect
        # 1:N Athena mappings (same source code -> multiple targets).
        seen_keys = {}  # (vocab_id, code) -> first target concept_id
        multi_target = defaultdict(list)  # keys with >1 target
        for cr in qs.iterator(chunk_size=2000):
            c1 = cr.concept_1
            c2 = cr.concept_2
            if not c1 or not c2:
                skipped_count += 1
                continue

            key = (c1.vocabulary_id, c1.concept_code[:100])
            if key in seen_keys:
                # 1:N: this source code already has a target. Log extra target.
                multi_target[key].append(c2.concept_id)
                skipped_count += 1
                continue
            seen_keys[key] = c2.concept_id

            # Use target concept's domain to determine OMOP table — a source
            # vocabulary can span multiple domains, but the target's domain
            # tells us where the mapped fact should land.
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

        if multi_target:
            logger.warning(
                '%d source codes had multiple Athena Maps-to targets; '
                'first target kept, extras skipped. '
                'Review these in concept_relationship if needed.',
                len(multi_target),
            )
            # Log the first few for debugging.
            for (vocab, code), extras in list(multi_target.items())[:10]:
                logger.warning(
                    '  %s:%s -> kept %s, skipped %s',
                    vocab, code, seen_keys[(vocab, code)], extras,
                )

        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {created_count} SCCM rows from Athena CR. '
            f'Skipped {skipped_count} (already mapped, 1:N extra, or missing concept).'
        ))
        if multi_target:
            self.stdout.write(self.style.WARNING(
                f'{len(multi_target)} source codes had multiple Athena targets '
                f'(first kept, extras in concept_relationship).'
            ))
