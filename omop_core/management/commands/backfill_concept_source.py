"""
Fill in Concept.source='HealthKey' for locally-minted concept rows.

`Concept.source` records provenance: NULL means the row came from an external
vocabulary release (Athena), 'HealthKey' means it was authored or minted locally
— per the field's own definition, that covers HK-* vocabularies, FHIR-upload
quarantine rows, and HealthKey-curated loads.

The column was added after most of these rows existed and was never populated,
so on staging only 19 of 1,979,422 rows carry it while 224 are demonstrably
local. The practical damage is to the vocabulary mirror:
`/api/v1/vocab-releases/{id}/snapshot/concept/?source=external` is meant to give
consumers genuine vocabulary content and currently hands them locally-invented
concepts labelled as external.

This command only writes the `source` column. It does not touch concept
identity, codes, domains, or any clinical row.

Why this is worth doing before the rest of #415: once `source` means what it
claims, the illegitimate rows become a one-line query rather than the id-range
guesswork this command has to use --

    SELECT * FROM concept WHERE source='HealthKey' AND vocabulary_id NOT LIKE 'HK-%'

A local row in an *external* vocabulary is always wrong: it shadows a real
concept and makes resolution nondeterministic, which is the defect behind #413
and #415. That query is the worklist for the remaining phases, and it should
eventually return zero rows.

Usage:
    python manage.py backfill_concept_source                      # dry run
    python manage.py backfill_concept_source --apply
    python manage.py backfill_concept_source --apply --rule seed_mint
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from omop_core.models import Concept

logger = logging.getLogger(__name__)

LOCAL_SOURCE = 'HealthKey'

# Vocabularies that are not Athena vocabulary releases. Their own Vocabulary
# rows say as much:
#   LOCAL  reference=''            version='synthetic enrichment'
#   sct    reference='synthetic'   version='synthetic'
#   FHIR   reference='FHIR import' version='local'
# 'sct' is the FHIR system-URI form of SNOMED (http://snomed.info/sct), which
# only appears when something minted a concept from a FHIR Coding rather than
# resolving it — Athena's SNOMED rows use vocabulary_id 'SNOMED'.
_LOCAL_VOCABULARIES = ['LOCAL', 'sct', 'FHIR']

# OHDSI reserves concept_id >= 2e9 for locally-authored concepts.
LOCAL_CONCEPT_ID_MIN = 2_000_000_000

# Each rule identifies rows that are locally minted, with the evidence for that
# claim. They are deliberately separate and separately reportable: the operator
# should be able to review each group in the dry run before applying, rather
# than trusting one opaque predicate.
RULES = [
    (
        'hk_vocabulary',
        Q(vocabulary__vocabulary_id__startswith='HK-'),
        "Rows in an HK-* vocabulary — deliberate HealthKey concepts",
    ),
    (
        'local_vocabulary',
        Q(vocabulary_id__in=_LOCAL_VOCABULARIES),
        f"Rows in a non-Athena vocabulary ({', '.join(_LOCAL_VOCABULARIES)})",
    ),
    (
        'seed_mint',
        Q(concept_id__gte=9_000_000, concept_id__lte=9_099_999),
        "legacy 900xxxx-range mints from the retired concept seeder",
    ),
    (
        'fhir_ingest_block',
        Q(concept_id__gte=392_021_000, concept_id__lte=392_022_000),
        "Contiguous block minted by FHIR ingestion (all valid_start 1970-01-01)",
    ),
    (
        'ohdsi_custom_range',
        Q(concept_id__gte=LOCAL_CONCEPT_ID_MIN),
        "concept_id in the OHDSI custom range, which Athena never allocates",
    ),
]
_RULE_NAMES = [name for name, _, _ in RULES]


class Command(BaseCommand):
    help = "Set Concept.source='HealthKey' on locally-minted concepts (see #415)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the change. Without this the command only reports.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Explicitly request a dry run (the default).')
        parser.add_argument(
            '--rule', action='append', default=None,
            choices=_RULE_NAMES,
            help='Limit to one rule. Repeatable. Default: all rules.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')

        selected = options['rule'] or _RULE_NAMES
        rules = [r for r in RULES if r[0] in selected]

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be written. Re-run with --apply.\n'))

        # Union of all selected rules, so a row matching two rules is counted
        # and updated once rather than twice.
        combined = Q()
        for _, predicate, _ in rules:
            combined |= predicate

        untagged = Concept.objects.filter(combined).filter(source__isnull=True)

        self.stdout.write('Rows matched per rule (rules overlap; totals are de-duplicated):')
        for name, predicate, description in rules:
            n = Concept.objects.filter(predicate).filter(source__isnull=True).count()
            self.stdout.write(f'  {name:22s} {n:6d}   {description}')

        total = untagged.count()
        self.stdout.write('')
        self.stdout.write('Breakdown by vocabulary:')
        for row in (untagged.values('vocabulary_id')
                    .annotate(n=Count('concept_id'))
                    .order_by('-n')):
            self.stdout.write(f"  {row['vocabulary_id']:16s} {row['n']}")

        updated = 0
        if apply_changes and total:
            with transaction.atomic():
                updated = untagged.update(source=LOCAL_SOURCE)
            logger.info('backfill_concept_source updated=%d', updated)

        self.stdout.write('')
        verb = 'Would tag' if not apply_changes else 'Tagged'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {total if not apply_changes else updated} row(s) as source='{LOCAL_SOURCE}'"))

        self._report_invariant(untagged if not apply_changes else None)

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Nothing was written. Re-run with --apply.'))

    def _report_invariant(self, would_tag_qs):
        """Report local rows sitting in an external vocabulary.

        In dry-run mode this must project the state AFTER the backfill, not
        before it: reporting only rows already tagged would say "invariant
        holds" right up until the moment the update makes it false. The preview
        an operator acts on has to describe the world the command will create.

        This is the set that should eventually be empty: a locally-authored row
        claiming a code in LOINC/SNOMED/CVX/HemOnc shadows the genuine concept
        and makes concept_by_vocab's unordered .first() nondeterministic. After
        this backfill the set is queryable directly instead of by id range,
        which is the point of running it first.
        """
        tagged = Q(source=LOCAL_SOURCE)
        if would_tag_qs is not None:
            # Dry run: include the rows this command would tag.
            tagged |= Q(concept_id__in=would_tag_qs.values('concept_id'))
        offenders = (
            Concept.objects
            .filter(tagged)
            .exclude(vocabulary__vocabulary_id__startswith='HK-')
            .exclude(vocabulary_id__in=_LOCAL_VOCABULARIES)
        )
        n = offenders.count()
        self.stdout.write('')
        if not n:
            self.stdout.write(self.style.SUCCESS(
                'Invariant holds: no HealthKey-sourced row sits in an external vocabulary.'))
            return
        verb = 'would sit' if would_tag_qs is not None else 'sit'
        self.stdout.write(self.style.WARNING(
            f'{n} HealthKey-sourced row(s) {verb} in an EXTERNAL vocabulary — each one '
            f'shadows a real concept and should be retired or remapped to the genuine '
            f'Athena concept. This is the worklist for the rest of #415:'))
        for row in (offenders.values('vocabulary_id')
                    .annotate(n=Count('concept_id'))
                    .order_by('-n')):
            self.stdout.write(f"  {row['vocabulary_id']:16s} {row['n']}")
