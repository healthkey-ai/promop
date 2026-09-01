"""Import ETL FHIR cross-map files into SourceCodeConceptMapping.

The ETL project maintains two JSON cross-map files used during FHIR parsing to
resolve source codes to OMOP concepts:

- ``cpt_to_snomed_map.json`` — CPT-4 codes → SNOMED concepts
- ``snomed_to_rxnorm_map.json`` — SNOMED codes → RxNorm concepts

These mappings are invisible in the Code Mapping UI because they were never
written to SourceCodeConceptMapping (SCCM).  This command imports them so that
the mappings appear in the UI and ``resolve_source_code()`` can use them.

**Patient-scoped by default.** Only mappings whose target concept already
appears in a clinical table for at least one patient are imported.  Pass
``--all`` to import every valid mapping regardless.

Re-runnable: existing SCCM rows are left untouched (get_or_create); new
mappings are inserted as approved rows with ``origin_system='HK-ETL'``.

Only standard concepts (``standard_concept='S'``) are accepted as targets.
Non-standard targets are skipped with a count reported at the end.
"""
import json
import logging

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import (
    DOMAIN_TO_TABLE,
    patient_scoped_concept_ids,
)

logger = logging.getLogger(__name__)

VALID_TYPES = ('cpt-to-snomed', 'snomed-to-rxnorm')


class Command(BaseCommand):
    help = 'Import ETL FHIR cross-map JSON files into SourceCodeConceptMapping.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--type', required=True, choices=VALID_TYPES,
            help='Cross-map type: cpt-to-snomed or snomed-to-rxnorm.',
        )
        parser.add_argument(
            '--file', required=True,
            help='Path to the JSON cross-map file.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report counts without writing.',
        )
        parser.add_argument(
            '--all', action='store_true',
            help=(
                'Import all valid mappings, not just those targeting concepts '
                'used by current patients.'
            ),
        )

    def handle(self, **options):
        map_type = options['type']
        file_path = options['file']
        dry_run = options['dry_run']
        import_all = options['all']

        try:
            with open(file_path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise CommandError(f'Cannot read {file_path}: {e}')

        self.stdout.write(f'Loaded {len(data):,} entries from {file_path}')

        if map_type == 'cpt-to-snomed':
            self._import_cpt_to_snomed(data, dry_run, import_all)
        else:
            self._import_snomed_to_rxnorm(data, dry_run, import_all)

    def _get_patient_scope(self, import_all):
        """Return the set of patient-scoped concept IDs, or None if --all."""
        if import_all:
            self.stdout.write('Importing all valid mappings (--all).')
            return None
        used = patient_scoped_concept_ids()
        self.stdout.write(
            f'Patient-scoped: {len(used):,} distinct concepts '
            f'found across clinical tables.'
        )
        if not used:
            self.stdout.write(self.style.WARNING(
                'No patient data found. Use --all to import all mappings.'
            ))
        return used

    def _import_cpt_to_snomed(self, data, dry_run, import_all):
        """Import CPT→SNOMED cross-map.

        JSON structure: {"10004": {"cptConceptId": "1034968", "cptCode": "10004",
        "cptDescriptor": "...", "snomedId": "0", "snomedDescriptor": ""}, ...}

        snomedId is a SNOMED *code* (not concept_id) despite the field name.
        Entries with snomedId="0" have no SNOMED mapping and are skipped.
        """
        used_ids = self._get_patient_scope(import_all)
        if used_ids is not None and not used_ids:
            return

        # Collect all SNOMED codes we need to look up
        snomed_codes = set()
        cpt_concept_ids = set()
        for entry in data.values():
            snomed_code = entry.get('snomedId', '0')
            if snomed_code and snomed_code != '0':
                snomed_codes.add(snomed_code)
            cpt_id = entry.get('cptConceptId')
            if cpt_id:
                try:
                    cpt_concept_ids.add(int(cpt_id))
                except (ValueError, TypeError):
                    pass

        # Batch-lookup SNOMED concepts by (vocabulary_id, concept_code)
        snomed_concepts = {}
        if snomed_codes:
            qs = Concept.objects.filter(
                vocabulary_id='SNOMED',
                concept_code__in=snomed_codes,
            ).only('concept_id', 'concept_code', 'domain_id', 'concept_name',
                   'standard_concept')
            for c in qs.iterator(chunk_size=5000):
                snomed_concepts[c.concept_code] = c

        # Batch-lookup CPT source concepts by concept_id
        cpt_concepts = {}
        if cpt_concept_ids:
            qs = Concept.objects.filter(
                concept_id__in=cpt_concept_ids,
            ).only('concept_id', 'concept_code', 'concept_name')
            for c in qs.iterator(chunk_size=5000):
                cpt_concepts[c.concept_id] = c

        self.stdout.write(
            f'Resolved {len(snomed_concepts):,} SNOMED targets, '
            f'{len(cpt_concepts):,} CPT source concepts.'
        )

        created = 0
        existed = 0
        conflicts = 0
        no_target = 0
        no_source = 0
        not_standard = 0
        out_of_scope = 0

        for entry in data.values():
            snomed_code = entry.get('snomedId', '0')
            if not snomed_code or snomed_code == '0':
                no_target += 1
                continue

            target = snomed_concepts.get(snomed_code)
            if not target:
                no_target += 1
                continue

            # Only accept standard concepts as targets
            if target.standard_concept != 'S':
                not_standard += 1
                continue

            # Patient-scoped filter
            if used_ids is not None and target.concept_id not in used_ids:
                out_of_scope += 1
                continue

            cpt_code = entry.get('cptCode', '')
            cpt_desc = entry.get('cptDescriptor', '')
            cpt_id = entry.get('cptConceptId')
            try:
                source_concept = cpt_concepts.get(int(cpt_id)) if cpt_id else None
            except (ValueError, TypeError):
                source_concept = None

            omop_table = DOMAIN_TO_TABLE.get(target.domain_id, '')

            if dry_run:
                created += 1
                continue

            obj, was_created = SourceCodeConceptMapping.objects.get_or_create(
                source_vocabulary_id='CPT4',
                source_code=cpt_code[:100],
                defaults={
                    'domain_id': target.domain_id or '',
                    'source_code_description': (cpt_desc or '')[:255],
                    'source_concept': source_concept,
                    'target_concept': target,
                    'destination_vocabulary_id': 'SNOMED',
                    'omop_table': omop_table,
                    'status': 'approved',
                    'origin': 'import',
                    'origin_system': 'HK-ETL',
                    'source': 'ETL',
                    'occurrence_count': 0,
                },
            )
            if was_created:
                created += 1
            else:
                existed += 1
                if obj.target_concept_id and obj.target_concept_id != target.concept_id:
                    conflicts += 1
                    logger.warning(
                        'CPT %s: existing target=%s differs from cross-map target=%d',
                        cpt_code, obj.target_concept_id, target.concept_id,
                    )

        self._report(dry_run, 'CPT4→SNOMED', created, existed,
                     no_target, no_source, not_standard, out_of_scope, conflicts)

    def _import_snomed_to_rxnorm(self, data, dry_run, import_all):
        """Import SNOMED→RxNorm cross-map.

        JSON structure: {"102002": "483117", "120006": "98297", ...}
        Keys are SNOMED codes, values are RxNorm codes.
        """
        used_ids = self._get_patient_scope(import_all)
        if used_ids is not None and not used_ids:
            return

        snomed_codes = set(data.keys())
        rxnorm_codes = set(data.values())

        # Batch-lookup SNOMED source concepts
        snomed_concepts = {}
        if snomed_codes:
            qs = Concept.objects.filter(
                vocabulary_id='SNOMED',
                concept_code__in=snomed_codes,
            ).only('concept_id', 'concept_code', 'concept_name', 'domain_id')
            for c in qs.iterator(chunk_size=5000):
                snomed_concepts[c.concept_code] = c

        # Batch-lookup RxNorm target concepts
        rxnorm_concepts = {}
        if rxnorm_codes:
            qs = Concept.objects.filter(
                vocabulary_id='RxNorm',
                concept_code__in=rxnorm_codes,
            ).only('concept_id', 'concept_code', 'domain_id', 'concept_name',
                   'standard_concept')
            for c in qs.iterator(chunk_size=5000):
                rxnorm_concepts[c.concept_code] = c

        self.stdout.write(
            f'Resolved {len(snomed_concepts):,} SNOMED sources, '
            f'{len(rxnorm_concepts):,} RxNorm targets.'
        )

        created = 0
        existed = 0
        conflicts = 0
        no_target = 0
        no_source = 0
        not_standard = 0
        out_of_scope = 0

        for snomed_code, rxnorm_code in data.items():
            source = snomed_concepts.get(snomed_code)
            if not source:
                no_source += 1
                continue

            target = rxnorm_concepts.get(rxnorm_code)
            if not target:
                no_target += 1
                continue

            # Only accept standard concepts as targets
            if target.standard_concept != 'S':
                not_standard += 1
                continue

            # Patient-scoped filter
            if used_ids is not None and target.concept_id not in used_ids:
                out_of_scope += 1
                continue

            omop_table = DOMAIN_TO_TABLE.get(target.domain_id, '')

            if dry_run:
                created += 1
                continue

            obj, was_created = SourceCodeConceptMapping.objects.get_or_create(
                source_vocabulary_id='SNOMED',
                source_code=snomed_code[:100],
                defaults={
                    'domain_id': target.domain_id or '',
                    'source_code_description': (source.concept_name or '')[:255],
                    'source_concept': source,
                    'target_concept': target,
                    'destination_vocabulary_id': 'RxNorm',
                    'omop_table': omop_table,
                    'status': 'approved',
                    'origin': 'import',
                    'origin_system': 'HK-ETL',
                    'source': 'ETL',
                    'occurrence_count': 0,
                },
            )
            if was_created:
                created += 1
            else:
                existed += 1
                if obj.target_concept_id and obj.target_concept_id != target.concept_id:
                    conflicts += 1
                    logger.warning(
                        'SNOMED %s: existing target=%s differs from cross-map RxNorm target=%d',
                        snomed_code, obj.target_concept_id, target.concept_id,
                    )

        self._report(dry_run, 'SNOMED→RxNorm', created, existed,
                     no_target, no_source, not_standard, out_of_scope, conflicts)

    def _report(self, dry_run, label, created, existed,
                no_target, no_source, not_standard, out_of_scope,
                conflicts=0):
        verb = 'Would create' if dry_run else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {created:,} SCCM rows from {label} cross-map.'
        ))
        parts = []
        if existed:
            parts.append(f'{existed:,} already existed')
        if no_target:
            parts.append(f'{no_target:,} missing target concept')
        if no_source:
            parts.append(f'{no_source:,} missing source concept')
        if not_standard:
            parts.append(f'{not_standard:,} non-standard target')
        if out_of_scope:
            parts.append(f'{out_of_scope:,} out of patient scope')
        if parts:
            self.stdout.write(f'Skipped: {", ".join(parts)}.')
        if conflicts:
            self.stdout.write(self.style.WARNING(
                f'{conflicts:,} existing rows map to a different target '
                f'(see WARNING log for details).'
            ))
