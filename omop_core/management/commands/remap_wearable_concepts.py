"""
Remap wearable Measurement/Observation rows onto the corrected concepts.

Deliberately a management command rather than a data migration: it rewrites
clinical rows in bulk (~190k on staging) and moves rows between the measurement
and observation tables. `start.sh` runs `migrate` on every deploy, so putting
this in a migration would fire it automatically on deploy with no dry-run and no
operator present. Run it explicitly instead.

It fixes three things left behind by the pre-#413 mapping:

  1. Rows pointing at the retired 900xxxx local mints are repointed at the real
     Athena concept for the same metric.
  2. Rows carrying a wrong or invented concept_code get the corrected code.
  3. Rows sitting in the wrong table are moved, because four wearable concepts
     are Observation-domain (steps, active_minutes, sleep_duration,
     flights_climbed) and were previously all written to measurement.

Usage:
    python manage.py remap_wearable_concepts --dry-run     # default, reports only
    python manage.py remap_wearable_concepts --apply
    python manage.py remap_wearable_concepts --apply --metric steps
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import Concept, Measurement, Observation
from omop_core.services.mappings import (
    WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB,
)

logger = logging.getLogger(__name__)

# concept_ids of the retired local mints (seed_omop_concepts, pre-#413), keyed
# by the metric each one carried. Scoped per-metric so remapping one metric
# cannot sweep up rows belonging to another.
RETIRED_MINT_BY_METRIC = {
    'steps':            9001019,
    'active_minutes':   9001020,
    'resting_hr':       9001021,
    'hrv_sdnn':         9001022,
    'respiratory_rate': 9001023,
    'sleep_duration':   9001024,
}
RETIRED_MINT_IDS = frozenset(RETIRED_MINT_BY_METRIC.values())

# Pre-#413 concept_code for each metric, where it differed from the corrected
# one. Only codes that are UNIQUELY wearable appear here.
#
# Deliberately excluded: body_mass (29463-7) and spo2 (59408-5). Both codes are
# correct already, and both are written by the vitals/FHIR ingestion paths as
# well — matching on them would sweep up non-wearable rows. Those metrics are
# only remapped via RETIRED_MINT_IDS, which is unambiguous.
OLD_CODES = {
    'active_minutes':             ['77592-4'],   # was the IPAQ survey concept
    'walking_speed':              ['41909-3'],   # was Deprecated BMI
    'walking_hr_avg':             ['89270-3'],   # was BMI [Ratio] Estimated
    'basal_energy':               ['41982-0'],   # was Percentage of body fat
    'active_energy':              ['55424-6'],   # was pedometer-scoped
    'flights_climbed':            ['96340-0'],   # not a valid LOINC code
    'walking_step_length':        ['96341-8'],   # not a valid LOINC code
    'walking_double_support_pct': ['96343-4'],   # not a valid LOINC code
}


class Command(BaseCommand):
    help = 'Remap wearable OMOP rows onto the corrected concepts (see #413).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write changes. Without this the command reports and rolls back.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Explicitly request a dry run (the default).')
        parser.add_argument(
            '--metric', action='append', default=None,
            help='Limit to one metric key. Repeatable.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')

        metrics = options['metric'] or sorted(WEARABLE_CONCEPT_CODE)
        unknown = [m for m in metrics if m not in WEARABLE_CONCEPT_CODE]
        if unknown:
            raise CommandError(f'Unknown metric key(s): {unknown}')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — no changes will be written. Re-run with --apply.\n'))

        totals = {'repointed': 0, 'recoded': 0, 'moved': 0, 'skipped_no_concept': 0}

        with transaction.atomic():
            for metric in metrics:
                self._remap_metric(metric, totals)

            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            'Repointed: {repointed}  |  Recoded: {recoded}  |  '
            'Moved between tables: {moved}  |  Skipped (no target concept): '
            '{skipped_no_concept}'.format(**totals)))
        if not apply_changes:
            self.stdout.write(self.style.WARNING('Rolled back (dry run).'))

    # ------------------------------------------------------------------

    def _remap_metric(self, metric, totals):
        new_code = WEARABLE_CONCEPT_CODE[metric]
        vocabulary_id = WEARABLE_CONCEPT_VOCAB[metric]

        target = (
            Concept.objects
            .filter(vocabulary_id=vocabulary_id, concept_code=new_code)
            .exclude(concept_id__in=RETIRED_MINT_IDS)
            .order_by('concept_id')
            .first()
        )
        if target is None:
            self.stdout.write(self.style.ERROR(
                f'  {metric:28s} SKIPPED — no concept for '
                f'({vocabulary_id}, {new_code}). Run seed_omop_concepts first.'))
            totals['skipped_no_concept'] += 1
            return

        old_codes = OLD_CODES.get(metric, [])
        mint_id = RETIRED_MINT_BY_METRIC.get(metric)
        wants_observation = target.domain_id == 'Observation'

        # Rows to fix: this metric's retired mint, or a superseded code.
        m_qs = Measurement.objects.filter(
            _wearable_row_filter('measurement', mint_id, old_codes))
        o_qs = Observation.objects.filter(
            _wearable_row_filter('observation', mint_id, old_codes))

        m_count = m_qs.count()
        o_count = o_qs.count()
        if not (m_count or o_count):
            return

        if wants_observation:
            moved = self._move_measurements_to_observation(m_qs, target, new_code)
            recoded = o_qs.update(
                observation_concept=target, observation_source_value=new_code)
            totals['moved'] += moved
            totals['recoded'] += recoded
            self.stdout.write(
                f'  {metric:28s} -> Observation {target.concept_id} '
                f'({new_code}): moved {moved}, recoded {recoded}')
        else:
            moved = self._move_observations_to_measurement(o_qs, target, new_code)
            recoded = m_qs.update(
                measurement_concept=target, measurement_source_value=new_code)
            totals['moved'] += moved
            totals['recoded'] += recoded
            self.stdout.write(
                f'  {metric:28s} -> Measurement {target.concept_id} '
                f'({new_code}): moved {moved}, recoded {recoded}')

    def _move_measurements_to_observation(self, qs, target, new_code):
        from patient_portal.api.views import _next_pk_batch

        rows = list(qs.values(
            'measurement_id', 'person_id', 'measurement_date',
            'measurement_type_concept_id', 'value_as_number', 'unit_source_value'))
        if not rows:
            return 0

        new_ids = _next_pk_batch(Observation, 'observation_id', len(rows))
        pending = []
        for row, oid in zip(rows, new_ids):
            obs = Observation(
                observation_id=oid,
                person_id=row['person_id'],
                observation_concept=target,
                observation_date=row['measurement_date'],
                observation_type_concept_id=row['measurement_type_concept_id'],
                value_as_number=row['value_as_number'],
                observation_source_value=new_code,
                unit_source_value=row['unit_source_value'],
            )
            obs._skip_patient_record_refresh = True
            pending.append(obs)

        Observation.objects.bulk_create(pending)
        Measurement.objects.filter(
            measurement_id__in=[r['measurement_id'] for r in rows]).delete()
        return len(pending)

    def _move_observations_to_measurement(self, qs, target, new_code):
        from patient_portal.api.views import _next_pk_batch

        rows = list(qs.values(
            'observation_id', 'person_id', 'observation_date',
            'observation_type_concept_id', 'value_as_number', 'unit_source_value'))
        if not rows:
            return 0

        new_ids = _next_pk_batch(Measurement, 'measurement_id', len(rows))
        pending = []
        for row, mid in zip(rows, new_ids):
            m = Measurement(
                measurement_id=mid,
                person_id=row['person_id'],
                measurement_concept=target,
                measurement_date=row['observation_date'],
                measurement_type_concept_id=row['observation_type_concept_id'],
                value_as_number=row['value_as_number'],
                measurement_source_value=new_code,
                unit_source_value=row['unit_source_value'],
            )
            m._skip_patient_record_refresh = True
            pending.append(m)

        Measurement.objects.bulk_create(pending)
        Observation.objects.filter(
            observation_id__in=[r['observation_id'] for r in rows]).delete()
        return len(pending)


def _wearable_row_filter(table, mint_id, old_codes):
    """Q() matching this metric's retired mint or its superseded concept_codes.

    Scoped to a single mint_id: matching the whole retired range would let one
    metric claim another metric's rows.
    """
    from django.db.models import Q

    if table == 'measurement':
        concept_field, source_field = 'measurement_concept_id', 'measurement_source_value'
    else:
        concept_field, source_field = 'observation_concept_id', 'observation_source_value'

    q = Q(pk__in=[])  # matches nothing
    if mint_id is not None:
        q |= Q(**{concept_field: mint_id})
    if old_codes:
        q |= Q(**{f'{source_field}__in': old_codes})
    return q
