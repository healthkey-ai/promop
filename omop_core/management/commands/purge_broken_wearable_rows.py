"""
Delete wearable Measurement/Observation rows written under the pre-#413 mapping.

Replaces the earlier `remap_wearable_concepts`, which tried to *migrate* these
rows. Migration turned out to be both riskier and unnecessary:

  - The cross-table move copied six columns and deleted the source, silently
    dropping measurement_datetime, is_erroneous, provider, visit, and the
    source/unit/value concept columns with no way to recover them.
  - Wearable rows are reproducible. Re-uploading the device export regenerates
    them correctly under the corrected mapping, so there is nothing to preserve.

So this command deletes rather than moves, and the operator re-uploads.

What counts as "broken":

  1. Rows whose concept is one of the retired 900xxxx local mints.
  2. Rows carrying a concept_code that the pre-#413 mapping used wrongly, where
     that code is UNIQUELY wearable.

Deliberately NOT matched by code: body_mass (29463-7) and spo2 (59408-5). Both
codes were always correct, and both are written by the vitals and FHIR ingestion
paths too — matching them would delete non-wearable clinical data. Rows for those
metrics are only removed when they sit on a retired mint, which is unambiguous.

Also NOT matched: hrv_sdnn (80404-7), despite #438 establishing that Garmin
values under that code are RMSSD and belong on HK-WEAR-HRV-RMSSD. Apple values
under the same code are correct SDNN, and a wearable OMOP row records nothing
about which device produced it (#442), so the two cannot be separated here.
Deleting by code would destroy correct Apple data. Garmin patients get correct
HRV only by re-uploading their export after #438; the historical rows stay
mis-filed until #442 makes them addressable.

Usage:
    python manage.py purge_broken_wearable_rows              # dry run (default)
    python manage.py purge_broken_wearable_rows --apply
    python manage.py purge_broken_wearable_rows --apply --keep-mints
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q, ProtectedError

from omop_core.models import Concept, Measurement, Observation, PatientRecord
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

# Retired local mints from seed_omop_concepts, pre-#413.
RETIRED_MINT_BY_METRIC = {
    'steps':            9001019,
    'active_minutes':   9001020,
    'resting_hr':       9001021,
    'hrv_sdnn':         9001022,
    'respiratory_rate': 9001023,
    'sleep_duration':   9001024,
}
RETIRED_MINT_IDS = sorted(RETIRED_MINT_BY_METRIC.values())

# Concept codes the pre-#413 mapping used for a wearable metric, each of which
# resolved to the wrong concept or to nothing at all. Every entry here is unique
# to wearables — see the module docstring for the two deliberate exclusions.
BROKEN_CODES = {
    '77592-4':  'active_minutes — resolved to Moderate physical activity [IPAQ], a survey item',
    '41909-3':  'walking_speed — resolved to Deprecated Body mass index (BMI)',
    '89270-3':  'walking_hr_avg — resolved to Body mass index (BMI) [Ratio] Estimated',
    '41982-0':  'basal_energy — resolved to Percentage of body fat Measured',
    '55424-6':  'active_energy — pedometer-scoped, too narrow for watch-derived energy',
    '96340-0':  'flights_climbed — not a valid LOINC code',
    '96341-8':  'walking_step_length — not a valid LOINC code',
    '96343-4':  'walking_double_support_pct — not a valid LOINC code',
}

# Rows deleted per statement, bounding transaction size and lock duration.
CHUNK_SIZE = 5000


class Command(BaseCommand):
    help = 'Delete wearable OMOP rows written under the pre-#413 mapping (see #413).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Delete the rows. Without this the command only reports.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Explicitly request a dry run (the default).')
        parser.add_argument(
            '--keep-mints', action='store_true',
            help='Leave the retired 900xxxx mint concepts in place after purging.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be deleted. Re-run with --apply.\n'))

        totals = {
            'measurements_deleted': 0, 'observations_deleted': 0,
            'mints_deleted': 0, 'mints_still_referenced': 0,
            'patient_records_marked_stale': 0,
        }
        wrote_anything = False

        try:
            # Suppress the signal chain: QuerySet.delete() fires post_delete per
            # row and omop_core/signals.py turns each Measurement deletion into a
            # full refresh_patient_record. Unsuppressed, deleting ~127k rows means
            # ~127k complete patient re-derivations. Derivation is handled once at
            # the end by marking the affected records stale.
            with suppress_patient_record_refresh():
                person_ids = self._affected_person_ids()
                self.stdout.write(
                    f'Affected persons: {len(person_ids)}\n')

                totals['measurements_deleted'] = self._purge(
                    Measurement, 'measurement_concept_id', 'measurement_source_value',
                    'measurement_id', apply_changes)
                totals['observations_deleted'] = self._purge(
                    Observation, 'observation_concept_id', 'observation_source_value',
                    'observation_id', apply_changes)
                wrote_anything = apply_changes and (
                    totals['measurements_deleted'] or totals['observations_deleted'])

                if not options['keep_mints']:
                    self._delete_retired_mints(totals, apply_changes)

                totals['patient_records_marked_stale'] = self._mark_stale(
                    person_ids, apply_changes)
        finally:
            # Printed on every exit path, including an exception partway through.
            # Deletions commit per chunk, so a failure can still leave written
            # state behind — under the old all-or-nothing transaction this
            # reminder was only needed on success.
            self._print_summary(totals, apply_changes, wrote_anything)

    # ------------------------------------------------------------------

    def _row_filter(self, concept_field, source_field):
        return (
            Q(**{f'{concept_field}__in': RETIRED_MINT_IDS})
            | Q(**{f'{source_field}__in': sorted(BROKEN_CODES)})
        )

    def _affected_person_ids(self):
        """person_ids touched by the purge, collected BEFORE any deletion."""
        ids = set(
            Measurement.objects.filter(
                self._row_filter('measurement_concept_id', 'measurement_source_value')
            ).values_list('person_id', flat=True).distinct()
        )
        ids |= set(
            Observation.objects.filter(
                self._row_filter('observation_concept_id', 'observation_source_value')
            ).values_list('person_id', flat=True).distinct()
        )
        return ids

    def _purge(self, model, concept_field, source_field, pk_field, apply_changes):
        qs = model.objects.filter(self._row_filter(concept_field, source_field))
        total = qs.count()
        label = model.__name__.lower()

        # Break the count down so the operator can see what is going and why.
        for mint_id in RETIRED_MINT_IDS:
            n = model.objects.filter(**{concept_field: mint_id}).count()
            if n:
                self.stdout.write(f'  {label:12s} on retired mint {mint_id}: {n}')
        for code, reason in sorted(BROKEN_CODES.items()):
            n = model.objects.filter(**{source_field: code}).count()
            if n:
                self.stdout.write(f'  {label:12s} code {code}: {n}  ({reason})')

        if not apply_changes or not total:
            return total

        deleted = 0
        while True:
            ids = list(
                model.objects.filter(self._row_filter(concept_field, source_field))
                .values_list(pk_field, flat=True)[:CHUNK_SIZE]
            )
            if not ids:
                break
            with transaction.atomic():
                n, _ = model.objects.filter(**{f'{pk_field}__in': ids}).delete()
            deleted += len(ids)
            self.stdout.write(f'    deleted {deleted}/{total} {label} rows')
        return deleted

    def _delete_retired_mints(self, totals, apply_changes):
        self.stdout.write('')
        for metric, mint_id in sorted(RETIRED_MINT_BY_METRIC.items()):
            if not Concept.objects.filter(concept_id=mint_id).exists():
                continue

            if not apply_changes:
                blockers = self._reference_sample(mint_id)
                if blockers:
                    self.stdout.write(
                        f'  mint {mint_id} ({metric}): referenced by {blockers} — '
                        f'may not be removable')
                    totals['mints_still_referenced'] += 1
                else:
                    self.stdout.write(f'  mint {mint_id} ({metric}): would be removed')
                    totals['mints_deleted'] += 1
                continue

            try:
                with transaction.atomic():
                    # Only 97 of the 112 FK fields pointing at Concept use PROTECT.
                    # 13 use DO_NOTHING and 2 SET_NULL, so ProtectedError alone does
                    # not cover them: a DO_NOTHING reference lets the DELETE through
                    # and Postgres raises at COMMIT of the OUTER transaction, past
                    # any handler here. Forcing constraints IMMEDIATE makes the
                    # violation surface on the DELETE itself, where it is catchable.
                    with connection.cursor() as cur:
                        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
                    Concept.objects.filter(concept_id=mint_id).delete()
            except (ProtectedError, IntegrityError) as exc:
                self.stdout.write(self.style.WARNING(
                    f'  mint {mint_id} ({metric}): still referenced, left in place '
                    f'({type(exc).__name__})'))
                totals['mints_still_referenced'] += 1
            else:
                self.stdout.write(f'  mint {mint_id} ({metric}): removed')
                totals['mints_deleted'] += 1

    def _reference_sample(self, concept_id):
        """Names of tables/fields still referencing this concept, best-effort.

        Checks the columns a wearable concept could plausibly occupy rather than
        all 112 FK fields — an unindexed EXISTS across every one would mean a
        sequential scan of several large tables. Apply-time SET CONSTRAINTS
        IMMEDIATE is the authoritative check; this exists so the dry run does not
        promise a deletion that then fails.
        """
        checks = (
            (Measurement, 'measurement_concept_id'),
            (Measurement, 'measurement_source_concept_id'),
            (Measurement, 'unit_concept_id'),
            (Measurement, 'value_as_concept_id'),
            (Observation, 'observation_concept_id'),
            (Observation, 'observation_source_concept_id'),
            (Observation, 'value_as_concept_id'),
        )
        found = []
        for model, field in checks:
            if model.objects.filter(**{field: concept_id}).exists():
                found.append(f'{model.__name__}.{field}')
        return ', '.join(found)

    def _mark_stale(self, person_ids, apply_changes):
        """Force re-derivation of the affected PatientRecords.

        Without this, `manage.py backfill_patient_records` reports "No stale
        records found" because derivation_version is untouched — a false
        all-clear that leaves summaries computed from now-deleted rows. Zeroing
        the version puts them back in the ordinary backfill path, so neither the
        operator nor a scheduled job has to remember --all.
        """
        qs = PatientRecord.objects.filter(person_id__in=person_ids)
        if not apply_changes:
            return qs.count()
        return qs.update(derivation_version=0)

    def _print_summary(self, totals, apply_changes, wrote_anything):
        self.stdout.write('')
        verb = 'Would delete' if not apply_changes else 'Deleted'
        self.stdout.write(self.style.SUCCESS(
            '{v} — measurement rows: {m}  |  observation rows: {o}  |  '
            'retired mints: {mints}  |  PatientRecords marked stale: {stale}'.format(
                v=verb, m=totals['measurements_deleted'],
                o=totals['observations_deleted'], mints=totals['mints_deleted'],
                stale=totals['patient_records_marked_stale'])))
        if totals['mints_still_referenced']:
            self.stdout.write(self.style.WARNING(
                f"{totals['mints_still_referenced']} retired mint(s) still referenced "
                f'and left in place.'))
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Nothing was deleted. Re-run with --apply.'))
        elif wrote_anything:
            self.stdout.write(self.style.WARNING(
                'Re-upload the device exports to regenerate this data, then run '
                '`manage.py backfill_patient_records` to re-derive PatientRecords.'))
