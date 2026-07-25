"""
backfill_fl_lot1_outcomes

One-time backfill for SYNTHEA-FL patients that have a 1L therapy Episode but
no LOT-1-outcome Observation.  Root cause: the FL generator emitted Rituximab
maintenance as a primary MedicationStatement without a therapy-outcome extension;
the FHIR importer unconditionally overwrote the induction outcome with None.

Every affected patient received Rituximab maintenance, which is given only to
responders (CR or PR), so we can safely assign a responder outcome:
  CR ≈ 63%  (55/(55+32) from the original FL cohort outcome weights)
  PR ≈ 37%  (32/(55+32))

The obs_date is set to the end of the patient's LOT-1 drug_exposure (the last
drug stop_date among all drugs tagged to the 1L Episode), falling back to
the 1L Episode end_date or start_date.

Usage:
    python manage.py backfill_fl_lot1_outcomes --confirm
    python manage.py backfill_fl_lot1_outcomes --org-slugs synthea-fl --limit 20 --confirm
    python manage.py backfill_fl_lot1_outcomes --dry-run
"""
import random

from django.core.management.base import BaseCommand

from omop_core.models import Concept, DrugExposure, Observation, PatientRecord
from omop_core.services.episode_service import _upsert_outcome_observation
from omop_core.services.mappings import CONCEPT_EHR_TYPE
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode, EpisodeEvent

DEFAULT_ORG_SLUGS = 'synthea-fl'

# Confirmed-responder weights for maintenance patients (CR:PR = 55:32).
_RESPONDER_OUTCOMES = ['Complete Response', 'Partial Response']
_RESPONDER_WEIGHTS  = [55, 32]


def _pick_outcome(seed):
    """Deterministically assign CR/PR from patient pk so the command is idempotent."""
    rng = random.Random(seed)
    return rng.choices(_RESPONDER_OUTCOMES, weights=_RESPONDER_WEIGHTS, k=1)[0]


def _lot1_obs_date(person):
    """Return the best obs_date for a LOT-1-outcome: end of the 1L episode."""
    ep = Episode.objects.filter(person=person, episode_number=1).first()
    if ep is None:
        return None
    # Try to derive from drug_exposures linked via EpisodeEvent.
    de_ids = list(EpisodeEvent.objects.filter(episode_id=ep.episode_id).values_list('event_id', flat=True))
    if de_ids:
        last_stop = (
            DrugExposure.objects
            .filter(drug_exposure_id__in=de_ids, drug_exposure_end_date__isnull=False)
            .order_by('-drug_exposure_end_date')
            .values_list('drug_exposure_end_date', flat=True)
            .first()
        )
        if last_stop:
            return last_stop
    return ep.episode_end_date or ep.episode_start_date


class Command(BaseCommand):
    help = 'Backfill LOT-1-outcome observations for SYNTHEA-FL patients missing them.'

    def add_arguments(self, parser):
        parser.add_argument('--org-slugs', default=DEFAULT_ORG_SLUGS,
                            help=f'Comma-separated org slugs (default: {DEFAULT_ORG_SLUGS}).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Cap the number of patients processed (smoke tests).')
        parser.add_argument('--confirm', action='store_true',
                            help='Required to write to the database.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be done without writing.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if not dry_run and not options['confirm']:
            self.stdout.write(self.style.WARNING(
                'Dry run — pass --confirm to write, or --dry-run to suppress this warning.'
            ))
            return

        slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]

        # Patients with 1L therapy but no LOT-1-outcome observation yet.
        candidates = list(
            PatientRecord.objects
            .filter(
                organization__slug__in=slugs,
                first_line_therapy__isnull=False,
                first_line_outcome__isnull=True,
            )
            .select_related('person', 'organization')
            .order_by('id')
        )
        if options['limit']:
            candidates = candidates[:options['limit']]

        self.stdout.write(f'Candidates: {len(candidates)} patient(s) with 1L therapy and no outcome.')

        if not candidates:
            self.stdout.write(self.style.SUCCESS('Nothing to do.'))
            return

        # Fetch required concepts once.
        ehr_type_concept  = Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first()
        no_match_concept  = Concept.objects.filter(concept_id=0).first()

        if not dry_run and (ehr_type_concept is None or no_match_concept is None):
            self.stderr.write(self.style.ERROR(
                'Required concepts missing (EHR type=32817, no-match=0). Aborting.'
            ))
            return

        written = skipped = already_has = 0
        cr_count = pr_count = 0

        for rec in candidates:
            person = rec.person

            # Double-check: skip if an observation already exists (race guard).
            if Observation.objects.filter(
                person=person, observation_source_value='LOT-1-outcome'
            ).exists():
                already_has += 1
                continue

            outcome  = _pick_outcome(rec.pk)
            obs_date = _lot1_obs_date(person)

            if dry_run:
                self.stdout.write(
                    f'  [DRY] person_id={person.person_id}  outcome={outcome}  obs_date={obs_date}'
                )
                written += 1
                if outcome == 'Complete Response':
                    cr_count += 1
                else:
                    pr_count += 1
                continue

            try:
                _upsert_outcome_observation(
                    person, 1, outcome,
                    ehr_type_concept, no_match_concept,
                    obs_date,
                )
                written += 1
                if outcome == 'Complete Response':
                    cr_count += 1
                else:
                    pr_count += 1
            except Exception as exc:
                skipped += 1
                self.stderr.write(self.style.WARNING(
                    f'  person_id={person.person_id} observation write failed: {exc}'
                ))

        self.stdout.write(
            f'Observations written: {written}  (CR={cr_count}, PR={pr_count})'
            f'  skipped={skipped}  already_had_outcome={already_has}'
        )

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — no DB changes made.'))
            return

        # Refresh PatientRecord so first_line_outcome is populated.
        # Refresh anyone who now has an observation — either just-written or pre-existing.
        needs_refresh = written + already_has
        self.stdout.write(f'Refreshing PatientRecord for {needs_refresh} patient(s)...')
        refresh_ok = refresh_fail = 0
        with suppress_patient_record_refresh():
            for rec in candidates:
                if Observation.objects.filter(
                    person=rec.person, observation_source_value='LOT-1-outcome'
                ).exists():
                    try:
                        refresh_patient_record(rec.person)
                        refresh_ok += 1
                    except Exception as exc:
                        refresh_fail += 1
                        self.stderr.write(self.style.WARNING(
                            f'  person_id={rec.person_id} refresh failed: {exc}'
                        ))

        self.stdout.write(
            f'PatientRecord refresh: ok={refresh_ok}  failed={refresh_fail}'
        )
        self.stdout.write(self.style.SUCCESS('Backfill complete.'))
