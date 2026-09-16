"""Populate missing line-scoped intent and discontinuation OMOP facts.

This is intentionally restricted to synthetic-cohort enrichment.  Real-world
clinical data must supply these assertions explicitly; a treatment boundary
does not establish a clinical discontinuation reason.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from omop_core.models import Observation, PatientRecord
from omop_core.services.episode_service import upsert_therapy_line_assertion
from omop_core.services.patient_record_service import refresh_patient_record
from omop_oncology.models import Episode


DEFAULT_ORG_SLUGS = 'synthea-mm,synthea-fl,synthea-bc'

_PHASE_INTENTS = {
    'induction': 'INDUCTION',
    'consolidation': 'CONSOLIDATION',
    'maintenance': 'MAINTENANCE',
    'transplant': 'TRANSPLANT',
    'car t-cell': 'SALVAGE',
    'bridging': 'BRIDGING',
}


def _phase_intent(episode):
    source = (episode.episode_source_value or '').lower()
    for phase, intent in _PHASE_INTENTS.items():
        if f'({phase})' in source:
            return intent
    return 'DEFINITIVE_LOCAL'


def _discontinuation(outcome):
    if (outcome or '').strip().lower() == 'progressive disease':
        return 'Progression'
    return 'Completion'


class Command(BaseCommand):
    help = ('Populate synthetic LOT-N-intent and LOT-N-discontinuation Observations '
            'from Episode phase and outcome, then refresh PatientRecord.')

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument('--org-slugs', default=None,
                           help=f'Comma-separated synthetic org slugs (default: {DEFAULT_ORG_SLUGS}).')
        scope.add_argument('--person-ids', default=None,
                           help='Comma-separated person IDs.')
        scope.add_argument('--all', action='store_true',
                           help='All patients with Episodes; use only for synthetic staging data.')
        parser.add_argument('--min-person-id', type=int, default=None,
                            help='Inclusive lower person-ID bound; enables resumable batches.')
        parser.add_argument('--max-person-id', type=int, default=None,
                            help='Inclusive upper person-ID bound; enables resumable batches.')
        parser.add_argument('--only-missing', action='store_true',
                            help='Skip people whose required synthetic line assertions already exist.')
        parser.add_argument('--skip-refresh', action='store_true',
                            help='Write OMOP assertions without rebuilding PatientRecord in this run.')
        parser.add_argument('--fast', action='store_true',
                            help='Use a set-based PostgreSQL insert for a large synthetic staging backfill.')
        parser.add_argument('--confirm', action='store_true', help='Required to write rows.')
        parser.add_argument('--dry-run', action='store_true', help='Report planned changes without writing.')

    def handle(self, *args, **options):
        if not options['confirm'] and not options['dry_run']:
            raise CommandError('Re-run with --confirm to write, or --dry-run to inspect changes.')
        if options['fast']:
            if not options['all'] or not options['skip_refresh']:
                raise CommandError('--fast requires --all and --skip-refresh.')
            if options['dry_run']:
                self.stdout.write('Fast dry run: no rows written.')
                return
            self._fast_backfill()
            return

        records = PatientRecord.objects.filter(person_id__in=Episode.objects.values('person_id'))
        if options['person_ids']:
            person_ids = [int(value) for value in options['person_ids'].split(',') if value.strip()]
            records = records.filter(person_id__in=person_ids)
        elif not options['all']:
            slugs = [s.strip() for s in (options['org_slugs'] or DEFAULT_ORG_SLUGS).split(',') if s.strip()]
            records = records.filter(organization__slug__in=slugs)
        if options['min_person_id'] is not None:
            records = records.filter(person_id__gte=options['min_person_id'])
        if options['max_person_id'] is not None:
            records = records.filter(person_id__lte=options['max_person_id'])

        records = list(records.select_related('person').order_by('person_id'))
        person_ids = [record.person_id for record in records]
        episodes_by_person = {}
        for episode in Episode.objects.filter(person_id__in=person_ids).order_by('person_id', 'episode_number'):
            if episode.episode_number and episode.episode_start_date:
                episodes_by_person.setdefault(episode.person_id, []).append(episode)
        source_values_by_person = {}
        outcome_by_person_line = {}
        for observation in Observation.objects.filter(person_id__in=person_ids).only(
            'person_id', 'observation_source_value', 'value_as_string', 'observation_date', 'observation_id',
        ).order_by('observation_date', 'observation_id'):
            source = observation.observation_source_value or ''
            source_values_by_person.setdefault(observation.person_id, set()).add(source)
            if source.startswith('LOT-') and source.endswith('-outcome'):
                try:
                    line = int(source[4:-8])
                except ValueError:
                    continue
                outcome_by_person_line[(observation.person_id, line)] = observation.value_as_string

        created_or_updated = 0
        refreshed = 0
        for record in records:
            episodes = episodes_by_person.get(record.person_id, [])
            if not episodes:
                continue
            existing_sources = source_values_by_person.get(record.person_id, set())
            if options['only_missing']:
                final_line = episodes[-1].episode_number
                required = {f'LOT-{episode.episode_number}-intent' for episode in episodes}
                required.update(
                    f'LOT-{episode.episode_number}-discontinuation'
                    for episode in episodes
                    if episode.episode_number < final_line and episode.episode_end_date
                )
                if required.issubset(existing_sources):
                    continue
            changed = False
            for episode in episodes:
                if not episode.episode_number or not episode.episode_start_date:
                    continue
                intent = _phase_intent(episode)
                if options['dry_run']:
                    changed = True
                    continue
                changed |= upsert_therapy_line_assertion(
                    record.person, line_number=episode.episode_number, kind='intent',
                    value=intent, assertion_date=episode.episode_start_date,
                )
                # A final line has no inferred discontinuation: its treatment may
                # still be ongoing.  Completed preceding lines do.
                if episode.episode_number < episodes[-1].episode_number and episode.episode_end_date:
                    outcome = outcome_by_person_line.get((record.person_id, episode.episode_number))
                    changed |= upsert_therapy_line_assertion(
                        record.person, line_number=episode.episode_number, kind='discontinuation',
                        value=_discontinuation(outcome), assertion_date=episode.episode_end_date,
                    )
            if changed:
                created_or_updated += 1
                if not options['dry_run'] and not options['skip_refresh']:
                    refresh_patient_record(record.person)
                    refreshed += 1

        label = 'Would enrich' if options['dry_run'] else 'Enriched'
        self.stdout.write(self.style.SUCCESS(
            f'{label} {created_or_updated} patient(s); refreshed={refreshed}.'
        ))

    def _fast_backfill(self):
        """Insert all missing synthetic assertions in two set-based statements."""
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(731905)")
                cursor.execute("""
                    WITH candidates AS (
                        SELECT e.person_id, e.episode_number, e.episode_start_date,
                               CASE
                                   WHEN e.episode_source_value ILIKE '%%(induction)%%' THEN 'INDUCTION'
                                   WHEN e.episode_source_value ILIKE '%%(consolidation)%%' THEN 'CONSOLIDATION'
                                   WHEN e.episode_source_value ILIKE '%%(maintenance)%%' THEN 'MAINTENANCE'
                                   WHEN e.episode_source_value ILIKE '%%(transplant)%%' THEN 'TRANSPLANT'
                                   WHEN e.episode_source_value ILIKE '%%(car t-cell)%%' THEN 'SALVAGE'
                                   WHEN e.episode_source_value ILIKE '%%(bridging)%%' THEN 'BRIDGING'
                                   ELSE 'DEFINITIVE_LOCAL'
                               END AS assertion_value
                        FROM episode e
                        WHERE e.episode_number IS NOT NULL AND e.episode_start_date IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM observation o
                              WHERE o.person_id = e.person_id
                                AND o.observation_source_value = 'LOT-' || e.episode_number || '-intent'
                          )
                    ), numbered AS (
                        SELECT *, row_number() OVER (ORDER BY person_id, episode_number) AS seq FROM candidates
                    )
                    INSERT INTO observation (
                        observation_id, person_id, observation_concept_id, observation_date,
                        observation_type_concept_id, value_as_string, observation_source_value, is_erroneous
                    )
                    SELECT (SELECT COALESCE(MAX(observation_id), 0) FROM observation) + seq,
                           person_id, 0, episode_start_date, 32817, assertion_value,
                           'LOT-' || episode_number || '-intent', FALSE
                    FROM numbered
                """)
                intents = cursor.rowcount
                cursor.execute("""
                    WITH candidates AS (
                        SELECT e.person_id, e.episode_number, e.episode_end_date,
                               CASE WHEN EXISTS (
                                   SELECT 1 FROM observation outcome
                                   WHERE outcome.person_id = e.person_id
                                     AND outcome.observation_source_value = 'LOT-' || e.episode_number || '-outcome'
                                     AND outcome.value_as_string = 'Progressive Disease'
                               ) THEN 'Progression' ELSE 'Completion' END AS assertion_value
                        FROM episode e
                        WHERE e.episode_number IS NOT NULL AND e.episode_end_date IS NOT NULL
                          AND e.episode_number < (
                              SELECT MAX(next_episode.episode_number) FROM episode next_episode
                              WHERE next_episode.person_id = e.person_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM observation o
                              WHERE o.person_id = e.person_id
                                AND o.observation_source_value = 'LOT-' || e.episode_number || '-discontinuation'
                          )
                    ), numbered AS (
                        SELECT *, row_number() OVER (ORDER BY person_id, episode_number) AS seq FROM candidates
                    )
                    INSERT INTO observation (
                        observation_id, person_id, observation_concept_id, observation_date,
                        observation_type_concept_id, value_as_string, observation_source_value, is_erroneous
                    )
                    SELECT (SELECT COALESCE(MAX(observation_id), 0) FROM observation) + seq,
                           person_id, 0, episode_end_date, 32817, assertion_value,
                           'LOT-' || episode_number || '-discontinuation', FALSE
                    FROM numbered
                """)
                discontinuations = cursor.rowcount
        self.stdout.write(self.style.SUCCESS(
            f'Fast OMOP load complete. intents={intents} discontinuations={discontinuations}; refreshed=0.'
        ))
