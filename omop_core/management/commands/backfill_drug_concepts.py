"""
Resolve drug_concept_id=0 on DrugExposure rows where the drug_source_value
matches a known concept in the loaded vocabulary.

Resolution order per source value:
  1. Strip parenthetical brand name, e.g. "Lenalidomide (Revlimid)" → "Lenalidomide"
  2. RxNorm Ingredient (case-insensitive exact match)
  3. HemOnc Regimen  (case-insensitive exact match)
  4. CVX vaccine      (case-insensitive exact match on concept_code or concept_name)

Unresolved source values are reported at the end for manual review.
No quarantine minting — this command only links to existing Athena concepts.
"""
import re
import logging
from django.core.management.base import BaseCommand
from django.db.models import Count

from omop_core.models import Concept, DrugExposure

logger = logging.getLogger(__name__)

_BRAND_RE = re.compile(r'\s*\([^)]+\)\s*$')

# Explicit CVX mapping for the HealthTree vaccine source values.
_CVX_MAP = {
    'COVID-19, mRNA, LNP-S, PF': 'SARS-COV-2 (COVID-19) vaccine, mRNA, spike protein, LNP, preservative free, 30 mcg/0.3mL dose',
    'Pneumococcal conjugate PCV 13': 'pneumococcal conjugate vaccine, 13 valent',
    'Influenza, seasonal, injectable': 'Influenza, split virus, quadrivalent, injectable, preservative free',
}


class Command(BaseCommand):
    help = 'Resolve drug_concept_id=0 rows against loaded vocabularies'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Report what would change without writing')
        parser.add_argument('--limit', type=int, default=0,
                            help='Limit to the N most frequent source values')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        limit = options['limit']

        # Gather distinct source values with their row counts.
        qs = (
            DrugExposure.objects
            .filter(drug_concept_id=0)
            .values('drug_source_value')
            .annotate(n=Count('drug_exposure_id'))
            .order_by('-n')
        )
        if limit:
            qs = qs[:limit]

        source_values = [(r['drug_source_value'], r['n']) for r in qs]
        if not source_values:
            self.stdout.write('No drug_exposure rows with drug_concept_id=0.')
            return

        self.stdout.write(f'{len(source_values)} distinct source values, '
                          f'{sum(n for _, n in source_values):,} total rows')

        resolved = {}    # source_value → Concept
        unresolved = {}  # source_value → row count

        for sv, count in source_values:
            concept = self._resolve(sv)
            if concept:
                resolved[sv] = (concept, count)
            else:
                unresolved[sv] = count

        # Report
        self.stdout.write(f'\nResolved: {len(resolved)} source values '
                          f'({sum(c for _, c in resolved.values()):,} rows)')
        for sv, (concept, count) in sorted(resolved.items(), key=lambda x: -x[1][1]):
            self.stdout.write(
                f'  {count:>5}  {sv:.50s}  →  {concept.concept_id} '
                f'{concept.vocabulary_id}/{concept.concept_name}'
            )

        self.stdout.write(f'\nUnresolved: {len(unresolved)} source values '
                          f'({sum(unresolved.values()):,} rows)')
        for sv, count in sorted(unresolved.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {count:>5}  {sv}')

        if dry_run:
            self.stdout.write('\n--dry-run: no changes written.')
            return

        # Apply
        total_updated = 0
        for sv, (concept, _count) in resolved.items():
            updated = DrugExposure.objects.filter(
                drug_concept_id=0, drug_source_value=sv
            ).update(drug_concept_id=concept.concept_id)
            total_updated += updated

        self.stdout.write(f'\nUpdated {total_updated:,} drug_exposure rows.')

    def _resolve(self, source_value):
        """Try to find a matching Concept for a drug_source_value."""
        if not source_value:
            return None

        # 1. Strip brand name in parentheses: "Lenalidomide (Revlimid)" → "Lenalidomide"
        generic = _BRAND_RE.sub('', source_value).strip()

        # 2. RxNorm Ingredient — try generic name first, then raw source value
        for name in (generic, source_value):
            concept = (
                Concept.objects
                .filter(concept_name__iexact=name,
                        vocabulary_id='RxNorm',
                        concept_class_id='Ingredient',
                        invalid_reason__isnull=True)
                .first()
            )
            if concept:
                return concept

        # 3. HemOnc Regimen
        concept = (
            Concept.objects
            .filter(concept_name__iexact=source_value,
                    vocabulary_id='HemOnc',
                    concept_class_id='Regimen',
                    invalid_reason__isnull=True)
            .first()
        )
        if concept:
            return concept

        # 4. CVX vaccine (explicit mapping)
        cvx_name = _CVX_MAP.get(source_value)
        if cvx_name:
            concept = (
                Concept.objects
                .filter(concept_name__iexact=cvx_name,
                        vocabulary_id='CVX',
                        invalid_reason__isnull=True)
                .first()
            )
            if concept:
                return concept

        return None
