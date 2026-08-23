"""
Resolve drug_concept_id=0 on DrugExposure rows where the drug_source_value
matches a known concept in the loaded vocabulary.

Resolution order per source value:
  1. Strip parenthetical suffix, e.g. "Lenalidomide (Revlimid)" → "Lenalidomide"
     (intentionally broad — any trailing "(…)" is removed, not just brand names)
  2. RxNorm Ingredient (case-insensitive exact match)
  3. HemOnc Regimen  (case-insensitive exact match)
  4. CVX vaccine      (explicit mapping via _CVX_MAP)

Unresolved source values are reported at the end for manual review.
No quarantine minting — this command only links to existing Athena concepts.

Note: .update() is used for the writes, which does not fire post_save signals.
PatientRecord does not derive any fields from drug_concept_id (only from
drug_source_value), so no refresh_patient_record call is needed here.
"""
import re
import logging
from django.core.management.base import BaseCommand
from django.db.models import Count

from omop_core.models import Concept, DrugExposure

logger = logging.getLogger(__name__)

# Strips any trailing parenthetical — intentionally broad so it catches
# brand names like "(Revlimid)" as well as qualifiers like "(oral)".
_BRAND_RE = re.compile(r'\s*\([^)]+\)\s*$')

# Explicit CVX mapping for the HealthTree vaccine source values.
_CVX_MAP = {
    'COVID-19, mRNA, LNP-S, PF': 'SARS-COV-2 (COVID-19) vaccine, mRNA, spike protein, LNP, preservative free, 30 mcg/0.3mL dose',
    'Pneumococcal conjugate PCV 13': 'pneumococcal conjugate vaccine, 13 valent',
    'Influenza, seasonal, injectable': 'Influenza, split virus, quadrivalent, injectable, preservative free',
}


def _build_concept_dict(vocabulary_id, concept_class_id=None):
    """Prefetch valid concepts into a {lowered_name: Concept} dict."""
    qs = Concept.objects.filter(
        vocabulary_id=vocabulary_id,
        invalid_reason__isnull=True,
    )
    if concept_class_id:
        qs = qs.filter(concept_class_id=concept_class_id)
    return {c.concept_name.lower(): c for c in qs.iterator(chunk_size=5000)}


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

        # Prefetch all candidate concepts into memory — 3 queries total.
        self.stdout.write('Prefetching vocabularies…')
        rxnorm = _build_concept_dict('RxNorm', 'Ingredient')
        hemonc = _build_concept_dict('HemOnc', 'Regimen')
        cvx = _build_concept_dict('CVX')
        self.stdout.write(f'  RxNorm Ingredients: {len(rxnorm):,}, '
                          f'HemOnc Regimens: {len(hemonc):,}, '
                          f'CVX: {len(cvx):,}')

        resolved = {}    # source_value → Concept
        unresolved = {}  # source_value → row count

        for sv, count in source_values:
            concept = self._resolve(sv, rxnorm, hemonc, cvx)
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

    def _resolve(self, source_value, rxnorm, hemonc, cvx):
        """Try to find a matching Concept for a drug_source_value."""
        if not source_value:
            return None

        # 1. Strip trailing parenthetical: "Lenalidomide (Revlimid)" → "Lenalidomide"
        generic = _BRAND_RE.sub('', source_value).strip()

        # 2. RxNorm Ingredient — try generic name first, then raw source value
        for name in (generic, source_value):
            concept = rxnorm.get(name.lower())
            if concept:
                return concept

        # 3. HemOnc Regimen
        concept = hemonc.get(source_value.lower())
        if concept:
            return concept

        # 4. CVX vaccine (explicit mapping)
        cvx_name = _CVX_MAP.get(source_value)
        if cvx_name:
            concept = cvx.get(cvx_name.lower())
            if concept:
                return concept

        return None
