"""Repoint measurements stranded on the old generic-lab placeholder concept.

A placeholder named "Generic Lab Measurement" was seeded at concept_id 3000963 and
used whenever a lab had no resolvable LOINC concept. The seed reasoned that
vocabulary_id='None' and concept_code='0' kept it out of LOINC lookups. True, and
beside the point: the collision is on the *id*. Athena owns 3000963 as "Hemoglobin
[Mass/volume] in Blood" (LOINC 718-7), so loading a real vocabulary silently turned
every unmapped lab ever written into a haemoglobin result — and derivation, which
keys on the concept rather than the source value, projected them as such.

The code no longer writes 3000963 (see CONCEPT_GENERIC_LAB). This repairs rows
already written.

Each affected row is resolved from its own `measurement_source_value`:

  - the source value names exactly one standard LOINC concept  → point at it
  - anything else                                              → point at 0

The second case is not a cop-out. OMOP reserves concept_id 0 for "no matching
concept", and a row that says 0 is honestly unmapped; a row that says 3000963 is
lying about being haemoglobin. `measurement_source_value` is preserved either way,
which is what a later vocabulary load needs to resolve it properly.

A row is left alone when its source value identifies the concept it already
points at — either its code, or its display name. Staging holds 773 genuine
haemoglobin results whose source value is the *name*, 'Hemoglobin [Mass/volume] in
Blood', because the writer had no code to hand. Demoting those to 0 would destroy
correct data to fix incorrect data. The check is against whatever concept 3000963
currently resolves to, so it stays right if a vocabulary redefines it again.

Dry run by default. Nothing is deleted, ever.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from omop_core.models import Concept, Measurement

STRANDED_CONCEPT_ID = 3000963
STRANDED_CODE = '718-7'
NO_MATCHING_CONCEPT = 0


class Command(BaseCommand):
    help = (
        'Repoint measurements written against the retired generic-lab placeholder '
        'concept 3000963, which a real vocabulary load redefines as haemoglobin.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the changes. Without this the command only reports.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']

        concept = Concept.objects.filter(concept_id=STRANDED_CONCEPT_ID).first()
        if concept is None:
            self.stdout.write('Concept 3000963 is absent; nothing to repair.')
            return
        self.stdout.write(
            f'Concept {STRANDED_CONCEPT_ID} currently reads '
            f'{concept.concept_name!r} ({concept.vocabulary_id} {concept.concept_code}).'
        )
        if concept.concept_code == '0':
            self.stdout.write(self.style.WARNING(
                '  It is still the local placeholder, so no vocabulary has redefined '
                'it yet. Repairing now is still correct and prevents the surprise.'
            ))

        # Source values that legitimately identify this concept: its code, and
        # its display name. Anything else is a foreign code wearing the wrong
        # concept, which is what this command exists to repair.
        # A LOINC long common name reads 'Analyte [Property] ... in System', so the
        # text before the first '[' is the analyte. Staging holds 757 genuine
        # haemoglobin results whose source value is just 'Hemoglobin' — the writer
        # had the analyte but not the code. Matching the full name alone would
        # demote every one of them.
        name = (concept.concept_name or '').casefold()
        own_identity = {
            (concept.concept_code or '').casefold(),
            name,
            name.split('[')[0].strip(),
            STRANDED_CODE,
        }
        own_identity.discard('')

        stranded = Measurement.objects.filter(
            measurement_concept_id=STRANDED_CONCEPT_ID
        ).exclude(measurement_source_value__isnull=True)

        by_source = dict(
            stranded.values_list('measurement_source_value')
            .annotate(n=Count('*'))
            .values_list('measurement_source_value', 'n')
        )
        kept = {s: n for s, n in by_source.items()
                if (s or '').casefold() in own_identity}
        by_source = {s: n for s, n in by_source.items()
                     if (s or '').casefold() not in own_identity}
        total = sum(by_source.values())
        if not total:
            self.stdout.write(self.style.SUCCESS('No stranded rows. Nothing to do.'))
            return

        # One query for every candidate code, not one per distinct source value.
        codes = {s for s in by_source if s}
        resolved = {
            c.concept_code: c.concept_id
            for c in Concept.objects.filter(
                vocabulary_id='LOINC', concept_code__in=codes, standard_concept='S'
            ).only('concept_id', 'concept_code')
        }

        remapped = {s: resolved[s] for s in by_source if s in resolved}
        unmapped_rows = sum(n for s, n in by_source.items() if s not in resolved)

        self.stdout.write('')
        if kept:
            self.stdout.write('')
            self.stdout.write('Left alone — the source value names this concept:')
            for s_, n in sorted(kept.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f'    {str(s_):<38} {n:>8,} rows')
        self.stdout.write('')
        self.stdout.write(f'Stranded rows              : {total:,}')
        self.stdout.write(f'  distinct source values   : {len(by_source):,}')
        self.stdout.write(f'  resolvable to a LOINC id : {len(remapped):,} source value(s)')
        self.stdout.write(f'  falling back to concept 0: {unmapped_rows:,} row(s)')
        self.stdout.write('')
        for source, count in sorted(by_source.items(), key=lambda kv: -kv[1])[:10]:
            target = resolved.get(source, NO_MATCHING_CONCEPT)
            self.stdout.write(f'    {str(source):<14} {count:>8,} rows  ->  concept {target}')

        if not apply_changes:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Dry run. Re-run with --apply to write.'))
            return

        updated = 0
        with transaction.atomic():
            for source, target in list(remapped.items()) + [(None, None)]:
                if source is None:
                    # Everything still on the stranded concept could not be
                    # resolved; 0 is the honest answer for those.
                    qs = Measurement.objects.filter(
                        measurement_concept_id=STRANDED_CONCEPT_ID,
                        measurement_source_value__in=list(by_source),
                    )
                    updated += qs.update(measurement_concept_id=NO_MATCHING_CONCEPT)
                    continue
                qs = Measurement.objects.filter(
                    measurement_concept_id=STRANDED_CONCEPT_ID,
                    measurement_source_value=source,
                )
                updated += qs.update(measurement_concept_id=target)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Repointed {updated:,} measurement(s).'))
        self.stdout.write(
            'PatientRecord is derived, so affected patients need a refresh: '
            'python manage.py populate_patient_record'
        )
