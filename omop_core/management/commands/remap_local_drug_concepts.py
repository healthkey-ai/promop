"""
Repoint drug_exposure rows off locally-minted drug concepts onto Standard ones.

Six drug concepts were minted locally instead of resolved against Athena. Their
`concept_code` is the drug's *name* ("Olaparib") where real HemOnc drug codes are
numeric ("366"), so something failed to resolve a drug and invented a concept
from the display string.

The clinical content is correct — the patient did receive the drug — but the
representation severs it from the vocabulary. The genuine HemOnc concept for
olaparib participates in 63 relationships ("Targeted therapy of" x24, brand
names, FDA/EMA/PMDA indications); the mint participates in zero. A query for
"patients on any PARP inhibitor" traverses concept_relationship from the
Standard concept and finds nothing, because the exposures point at a row with no
edges. The data is present but analytically invisible.

The fix follows OMOP's own conventions:

  drug_concept_id        -> the STANDARD concept (RxNorm / RxNorm Extension)
  drug_source_concept_id -> the HemOnc concept, preserving what was recorded

RxNorm is the standard vocabulary for the Drug domain, so the HemOnc drug
concepts are standard_concept=NULL by design — they exist to be mapped onward.
Pointing drug_concept_id at HemOnc would swap one non-standard concept for
another.

The mapping below is a reviewed constant, NOT resolved by name at runtime.
Name matching is how the retired concept seeder came to map LOINC 10839-9 (Troponin I)
to a concept named Troponin T. Each Standard target here was taken from the
vocabulary's own 'Maps to' edge, which is also what settles the "Ado-" question:
HemOnc "Trastuzumab emtansine" maps to a Standard concept named
"ado-Trastuzumab emtansine", so they are the same drug per the vocabulary rather
than per a string comparison.

Usage:
    python manage.py remap_local_drug_concepts              # dry run (default)
    python manage.py remap_local_drug_concepts --apply
    python manage.py remap_local_drug_concepts --apply --keep-mints
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from omop_core.models import Concept, DrugExposure, PatientRecord
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

# (local mint, HemOnc source concept, Standard target). Verified against the
# vocabulary: each Standard target is the 'Maps to' edge from the HemOnc concept,
# and each HemOnc concept_code is numeric where the mint's is a drug name.
#
#   mint         HemOnc     Standard   drug
DRUG_REMAP = [
    (2006492703, 35802866, 902726),    # Ado-Trastuzumab Emtansine -> ado-Trastuzumab emtansine
    (2012334076, 35803216, 45892579),  # Olaparib                  -> olaparib
    (2018816589, 35803231, 45892075),  # Palbociclib               -> palbociclib
    (2018910366, 35802975, 1310317),   # Cyclophosphamide          -> cyclophosphamide
    (2019696819, 35803229, 1378382),   # Paclitaxel                -> paclitaxel
    (2022763720, 42542260, 902724),    # Trastuzumab Deruxtecan    -> Fam-trastuzumab deruxtecan
]

MINT_IDS = [m for m, _, _ in DRUG_REMAP]


class Command(BaseCommand):
    help = 'Repoint drug_exposure off locally-minted drug concepts (see #427).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the changes. Without this the command only reports.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Explicitly request a dry run (the default).')
        parser.add_argument(
            '--keep-mints', action='store_true',
            help='Leave the mint concepts in place after remapping.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be written. Re-run with --apply.\n'))

        totals = {'exposures': 0, 'mints_deleted': 0, 'mints_still_referenced': 0,
                  'records_marked_stale': 0}
        wrote_anything = False

        try:
            # drug_exposure changes below use queryset .update()/.delete(); the
            # former sends no post_save at all, but deleting a concept could
            # cascade elsewhere, and PatientRecord derivation is deferred to the
            # backfill either way. Suppressing keeps that uniform and avoids the
            # per-row refresh storm found in #413.
            with suppress_patient_record_refresh():
                person_ids = set(
                    DrugExposure.objects.filter(drug_concept_id__in=MINT_IDS)
                    .values_list('person_id', flat=True).distinct()
                )
                for mint_id, hemonc_id, standard_id in DRUG_REMAP:
                    n = self._remap_one(mint_id, hemonc_id, standard_id, apply_changes)
                    totals['exposures'] += n
                    wrote_anything |= bool(apply_changes and n)

                if not options['keep_mints']:
                    self._delete_mints(totals, apply_changes)

                totals['records_marked_stale'] = self._mark_stale(person_ids, apply_changes)
        finally:
            self._summarise(totals, apply_changes, wrote_anything)

    # ------------------------------------------------------------------

    def _remap_one(self, mint_id, hemonc_id, standard_id, apply_changes):
        mint = Concept.objects.filter(concept_id=mint_id).first()
        standard = Concept.objects.filter(concept_id=standard_id).first()
        hemonc = Concept.objects.filter(concept_id=hemonc_id).first()

        rows = DrugExposure.objects.filter(drug_concept_id=mint_id)
        n = rows.count()
        name = mint.concept_name if mint else f'(mint {mint_id} absent)'

        if standard is None or hemonc is None:
            self.stdout.write(self.style.ERROR(
                f'  {name[:28]:30s} SKIPPED — target concept missing '
                f'(standard={standard_id}, hemonc={hemonc_id}). '
                f'Is the Athena vocabulary loaded?'))
            return 0
        if not n:
            return 0

        if standard.standard_concept != 'S':
            self.stdout.write(self.style.ERROR(
                f'  {name[:28]:30s} SKIPPED — target {standard_id} is not Standard '
                f'(standard_concept={standard.standard_concept!r})'))
            return 0

        arrow = f'-> {standard_id} {standard.concept_name[:24]}'
        if not apply_changes:
            self.stdout.write(f'  {name[:28]:30s} {n:5d} rows {arrow}')
            return n

        with transaction.atomic():
            rows.update(drug_concept_id=standard_id, drug_source_concept_id=hemonc_id)
        self.stdout.write(f'  {name[:28]:30s} {n:5d} rows {arrow}')
        return n

    def _delete_mints(self, totals, apply_changes):
        self.stdout.write('')
        for mint_id in MINT_IDS:
            mint = Concept.objects.filter(concept_id=mint_id).first()
            if mint is None:
                continue

            if not apply_changes:
                # Project the state AFTER the remap: the remap claims every
                # drug_exposure row on this mint, so what matters is whether
                # anything ELSE still points at it.
                others = DrugExposure.objects.filter(
                    drug_source_concept_id=mint_id).count()
                if others:
                    self.stdout.write(
                        f'  mint {mint_id}: {others} row(s) would remain via '
                        f'drug_source_concept_id — not removable')
                    totals['mints_still_referenced'] += 1
                else:
                    self.stdout.write(f'  mint {mint_id}: would be removed')
                    totals['mints_deleted'] += 1
                continue

            try:
                with transaction.atomic():
                    # Only 97 of the 112 FK fields pointing at Concept use
                    # PROTECT; 13 use DO_NOTHING, whose violation would surface
                    # at COMMIT of the outer transaction rather than here.
                    with connection.cursor() as cur:
                        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
                    Concept.objects.filter(concept_id=mint_id).delete()
            except (ProtectedError, IntegrityError) as exc:
                self.stdout.write(self.style.WARNING(
                    f'  mint {mint_id}: still referenced, left in place '
                    f'({type(exc).__name__})'))
                totals['mints_still_referenced'] += 1
            else:
                self.stdout.write(f'  mint {mint_id}: removed')
                totals['mints_deleted'] += 1

    def _mark_stale(self, person_ids, apply_changes):
        """Force re-derivation of affected PatientRecords.

        Therapy fields derive from drug_exposure, and queryset .update() sends
        no signals, so nothing would otherwise notice the concepts changed.
        Zeroing derivation_version puts these records in the ordinary
        backfill_patient_records path rather than relying on --all.
        """
        qs = PatientRecord.objects.filter(person_id__in=person_ids)
        if not apply_changes:
            return qs.count()
        return qs.update(derivation_version=0)

    def _summarise(self, totals, apply_changes, wrote_anything):
        self.stdout.write('')
        verb = 'Would remap' if not apply_changes else 'Remapped'
        self.stdout.write(self.style.SUCCESS(
            '{v} — drug_exposure rows: {e}  |  mints removed: {m}  |  '
            'PatientRecords marked stale: {s}'.format(
                v=verb, e=totals['exposures'], m=totals['mints_deleted'],
                s=totals['records_marked_stale'])))
        if totals['mints_still_referenced']:
            self.stdout.write(self.style.WARNING(
                f"{totals['mints_still_referenced']} mint(s) still referenced and left in place."))
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Nothing was written. Re-run with --apply.'))
        elif wrote_anything:
            self.stdout.write(self.style.WARNING(
                'Run `manage.py backfill_patient_records` to re-derive the affected records.'))
