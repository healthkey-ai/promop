"""
Management command: fix_synthea_bc_data

Fixes data quality issues in the synthea-bc cohort so PRism analytics charts
have meaningful data:

  1. Fix LOT1 carboplatin episodes → AC-T (HemOnc concept)
  2. Delete LOT2/3 individual-drug episodes (paclitaxel, trastuzumab) and
     create proper BC regimen episodes (T-DXd, T-DM1, Capecitabine, etc.)
  3. Create Death records for ~20% of patients → enables Landmark OS
  4. Update patient_record.death_date from Death table via raw SQL
  5. Refresh PatientRecord for all synthea-bc patients

Usage:
    python manage.py fix_synthea_bc_data --confirm
    python manage.py fix_synthea_bc_data --org-slug synthea-bc --confirm
"""

import random
import datetime

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from omop_core.models import (
    Organization, PatientRecord, Person, Concept,
    DrugExposure, Observation,
)
from omop_core.services.mappings import CONCEPT_EHR_TYPE
from omop_core.services.pk import next_pk
from omop_core.services.patient_record_service import refresh_patient_record


# ── LOT concept IDs (all HemOnc, confirmed in staging DB) ─────────────────
_AC_T     = 35101507
_TC       = 35804232
_THP      = 1525210
_TAMOX    = 35804221
_MC       = 35804254
_T_DXD    = 42542261
_T_DM1    = 35805230
_CAPECIT  = 35804227
_OLAPARIB = 35804269
_SG       = 912024
_ERIBULIN = 35804265

_CARBOPLATIN_RXNORM = 1344905   # RxNorm — the wrong LOT1 concept we're replacing

# LOT1 concept_id → [(LOT2 concept_id, weight), ...]
_LOT2_PLANS = {
    _THP:   [(_T_DXD, 70), (_T_DM1, 30)],
    _TC:    [(_T_DXD, 60), (_T_DM1, 40)],
    _TAMOX: [(_CAPECIT, 60), (_OLAPARIB, 40)],
    _AC_T:  [(_T_DXD, 40), (_SG, 40), (_CAPECIT, 20)],
    # carboplatin → will be re-mapped to _AC_T first, then treated as AC-T for LOT2
    None:   [(_T_DXD, 40), (_SG, 30), (_CAPECIT, 30)],   # NULL concept fallback
    _MC:    [(_CAPECIT, 50), (_SG, 50)],
}

# LOT2 concept_id → [(LOT3 concept_id, weight), ...]
_LOT3_PLANS = {
    _T_DXD:   [(_ERIBULIN, 50), (_SG, 50)],
    _T_DM1:   [(_ERIBULIN, 60), (_CAPECIT, 40)],
    _CAPECIT: [(_ERIBULIN, 60), (_SG, 40)],
    _OLAPARIB:[(_SG, 50), (_CAPECIT, 50)],
    _SG:      [(_ERIBULIN, 50), (_CAPECIT, 50)],
}

# concept_id → (min_months, max_months) — varies by outcome bucket
_DURATIONS = {
    _T_DXD:   {'good': (8, 18), 'stable': (5, 12), 'poor': (3, 8)},
    _T_DM1:   {'good': (6, 15), 'stable': (4, 10), 'poor': (2, 6)},
    _CAPECIT: {'good': (6, 12), 'stable': (3, 8),  'poor': (2, 5)},
    _OLAPARIB:{'good': (8, 18), 'stable': (4, 12), 'poor': (3, 8)},
    _SG:      {'good': (5, 12), 'stable': (3, 8),  'poor': (2, 5)},
    _ERIBULIN:{'good': (4, 10), 'stable': (3, 7),  'poor': (2, 4)},
}
_DEFAULT_DURATION = {'good': (4, 10), 'stable': (3, 7), 'poor': (2, 4)}

_OUTCOME_CODES = {
    'Complete Response': '182840001',
    'Partial Response':  '182841002',
    'Stable Disease':    '182843004',
    'Progressive Disease':'182842009',
}

_OUTCOME_WEIGHTS_LOT2 = [('Complete Response', 15), ('Partial Response', 25),
                          ('Stable Disease', 30), ('Progressive Disease', 30)]
_OUTCOME_WEIGHTS_LOT3 = [('Complete Response', 5),  ('Partial Response', 15),
                          ('Stable Disease', 30), ('Progressive Disease', 50)]


def _add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _duration(concept_id, outcome, rng):
    bucket = ('good' if outcome in ('Complete Response', 'Partial Response')
               else 'stable' if outcome == 'Stable Disease' else 'poor')
    lo, hi = _DURATIONS.get(concept_id, _DEFAULT_DURATION)[bucket]
    return rng.randint(lo, hi)


def _pick_weighted(options_weights, rng):
    opts, wts = zip(*options_weights)
    return rng.choices(opts, weights=wts, k=1)[0]


class Command(BaseCommand):
    help = "Fix synthea-bc episodes (proper LOT2/3, death records, condition aliases)"

    def add_arguments(self, parser):
        parser.add_argument('--org-slug', default='synthea-bc',
                            help='Organization slug to fix (default: synthea-bc)')
        parser.add_argument('--seed', type=int, default=42,
                            help='RNG seed for reproducible output')
        parser.add_argument('--lot2-fraction', type=float, default=0.30,
                            help='Fraction of patients to receive LOT2 (default: 0.30)')
        parser.add_argument('--lot3-fraction', type=float, default=0.50,
                            help='Fraction of LOT2 patients to receive LOT3 (default: 0.50)')
        parser.add_argument('--deceased-fraction', type=float, default=0.20,
                            help='Fraction of patients to mark as deceased (default: 0.20)')
        parser.add_argument('--confirm', action='store_true',
                            help='Actually apply changes (omit for dry-run)')

    def handle(self, *args, **options):
        dry_run = not options['confirm']
        if dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --confirm to apply changes"))

        org = Organization.objects.get(slug=options['org_slug'])
        records = list(PatientRecord.objects.filter(organization=org)
                       .select_related('person').order_by('person_id'))
        if not records:
            self.stdout.write(self.style.ERROR(f"No patients in org '{options['org_slug']}'"))
            return
        self.stdout.write(f"Found {len(records)} patients in '{options['org_slug']}'")

        rng = random.Random(options['seed'])

        try:
            from omop_oncology.models import Episode, EpisodeEvent
        except ImportError:
            self.stdout.write(self.style.ERROR("omop_oncology not available"))
            return

        pids = [r.person_id for r in records]

        # ── Pre-fetch concepts ──────────────────────────────────────────────
        all_cids = [_AC_T, _TC, _THP, _TAMOX, _MC,
                    _T_DXD, _T_DM1, _CAPECIT, _OLAPARIB, _SG, _ERIBULIN,
                    CONCEPT_EHR_TYPE]
        concept_map = {c.concept_id: c
                       for c in Concept.objects.filter(concept_id__in=all_cids)}
        outcome_concepts = {name: Concept.objects.filter(concept_code=code).first()
                            for name, code in _OUTCOME_CODES.items()}
        no_match = Concept.objects.filter(concept_id=0).first()
        ehr_type = concept_map.get(CONCEPT_EHR_TYPE)

        # ── Step 1: Fix LOT1 carboplatin episodes → AC-T ───────────────────
        carb_episodes = Episode.objects.filter(
            person_id__in=pids,
            episode_number=1,
            episode_source_concept_id=_CARBOPLATIN_RXNORM,
        )
        ac_t_concept = concept_map.get(_AC_T)
        carb_count = carb_episodes.count()
        self.stdout.write(f"\nStep 1: Fix {carb_count} LOT1 carboplatin episodes → AC-T")
        if not dry_run and ac_t_concept:
            carb_episodes.update(
                episode_source_concept_id=_AC_T,
                episode_object_concept_id=_AC_T,
            )
            # Also fix associated DrugExposure concepts
            carb_ep_ids = list(carb_episodes.values_list('episode_id', flat=True))
            if carb_ep_ids:
                de_ids = list(EpisodeEvent.objects.filter(
                    episode_id__in=carb_ep_ids).values_list('event_id', flat=True))
                DrugExposure.objects.filter(drug_exposure_id__in=de_ids).update(
                    drug_concept_id=_AC_T,
                    drug_source_concept_id=_AC_T,
                    drug_source_value='AC-T',
                )

        # ── Step 2: Delete LOT2/3 episodes and associated data ─────────────
        lot23_eps = Episode.objects.filter(
            person_id__in=pids, episode_number__gte=2)
        lot23_ep_ids = list(lot23_eps.values_list('episode_id', flat=True))
        de_ids_to_del = list(EpisodeEvent.objects.filter(
            episode_id__in=lot23_ep_ids).values_list('event_id', flat=True))
        self.stdout.write(
            f"\nStep 2: Delete {lot23_eps.count()} LOT2/3 episodes, "
            f"{len(de_ids_to_del)} DrugExposures, LOT-2/3 Observations")
        if not dry_run:
            EpisodeEvent.objects.filter(episode_id__in=lot23_ep_ids).delete()
            Episode.objects.filter(episode_id__in=lot23_ep_ids).delete()
            DrugExposure.objects.filter(drug_exposure_id__in=de_ids_to_del).delete()
            Observation.objects.filter(
                person_id__in=pids,
                observation_source_value__in=['LOT-2-outcome', 'LOT-3-outcome'],
            ).delete()

        # ── Step 3: Determine which patients get LOT2 ─────────────────────
        # Build LOT1 concept map per patient
        lot1_concept: dict[int, int | None] = {}
        for ep in Episode.objects.filter(person_id__in=pids, episode_number=1).select_related('episode_source_concept'):
            cid = ep.episode_source_concept_id
            # Remap carboplatin to AC-T (already updated above, but handle race)
            if cid == _CARBOPLATIN_RXNORM:
                cid = _AC_T
            lot1_concept[ep.person_id] = cid

        # LOT1 end date per patient
        lot1_end: dict[int, datetime.date] = {}
        for ep in Episode.objects.filter(person_id__in=pids, episode_number=1):
            if ep.episode_end_date:
                lot1_end[ep.person_id] = ep.episode_end_date

        # Record diagnosis_date for patients without LOT1
        diag_date: dict[int, datetime.date] = {
            r.person_id: r.diagnosis_date
            for r in records if r.diagnosis_date
        }

        # Choose LOT2 patients: all who have LOT1, then random up to lot2_fraction
        has_lot1 = [pid for pid in pids if pid in lot1_concept]
        n_lot2 = max(0, round(len(records) * options['lot2_fraction']))
        lot2_pids = set(rng.sample(has_lot1, min(n_lot2, len(has_lot1))))
        self.stdout.write(f"\nStep 3: Create LOT2 for {len(lot2_pids)} patients")

        # ── Step 4: Create LOT2 episodes ──────────────────────────────────
        lot2_info: dict[int, tuple[int, datetime.date]] = {}  # pid → (concept_id, end_date)

        if not dry_run:
            for pid in lot2_pids:
                l1_cid = lot1_concept.get(pid)
                plan = _LOT2_PLANS.get(l1_cid, _LOT2_PLANS[None])
                l2_cid = _pick_weighted(plan, rng)
                l2_concept = concept_map.get(l2_cid)

                l1_end = lot1_end.get(pid)
                diag = diag_date.get(pid)
                if l1_end:
                    start = _add_months(l1_end, rng.randint(1, 2))
                elif diag:
                    start = _add_months(diag, rng.randint(3, 6))
                else:
                    continue

                outcome = _pick_weighted(_OUTCOME_WEIGHTS_LOT2, rng)
                months = _duration(l2_cid, outcome, rng)
                end = _add_months(start, months)

                person = Person.objects.get(person_id=pid)
                de = DrugExposure(
                    drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
                    person=person,
                    drug_concept=l2_concept or no_match,
                    drug_exposure_start_date=start,
                    drug_exposure_end_date=end,
                    drug_type_concept_id=CONCEPT_EHR_TYPE,
                    drug_source_value=(l2_concept.concept_name[:50] if l2_concept else 'Unknown'),
                    drug_source_concept=l2_concept,
                )
                de._skip_patient_record_refresh = True
                de.save()

                ep = Episode(
                    episode_id=next_pk(Episode, 'episode_id'),
                    person=person,
                    episode_concept=ehr_type,
                    episode_object_concept=l2_concept or no_match,
                    episode_type_concept=ehr_type,
                    episode_start_date=start,
                    episode_end_date=end,
                    episode_number=2,
                    episode_source_value='LOT-2',
                    episode_source_concept=l2_concept,
                )
                ep.save()

                EpisodeEvent.objects.get_or_create(
                    episode_id=ep.episode_id,
                    event_id=de.drug_exposure_id,
                    defaults={'episode_event_field_concept': ehr_type},
                )

                oc = outcome_concepts.get(outcome)
                if oc:
                    Observation.objects.create(
                        observation_id=next_pk(Observation, 'observation_id'),
                        person=person,
                        observation_concept=oc,
                        observation_date=end,
                        observation_type_concept_id=CONCEPT_EHR_TYPE,
                        value_as_string=outcome,
                        observation_source_value='LOT-2-outcome',
                    )
                lot2_info[pid] = (l2_cid, end)

        # ── Step 5: Create LOT3 episodes ──────────────────────────────────
        lot3_candidates = list(lot2_pids)
        n_lot3 = max(0, round(len(lot3_candidates) * options['lot3_fraction']))
        lot3_pids = set(rng.sample(lot3_candidates, min(n_lot3, len(lot3_candidates))))
        self.stdout.write(f"Step 5: Create LOT3 for {len(lot3_pids)} patients")

        lot3_end: dict[int, datetime.date] = {}

        if not dry_run:
            for pid in lot3_pids:
                l2_cid, l2_end_date = lot2_info.get(pid, (None, None))
                if not l2_end_date:
                    continue
                plan = _LOT3_PLANS.get(l2_cid, list(_LOT3_PLANS.values())[0])
                l3_cid = _pick_weighted(plan, rng)
                l3_concept = concept_map.get(l3_cid)

                start = _add_months(l2_end_date, rng.randint(1, 2))
                outcome = _pick_weighted(_OUTCOME_WEIGHTS_LOT3, rng)
                months = _duration(l3_cid, outcome, rng)
                end = _add_months(start, months)

                person = Person.objects.get(person_id=pid)
                de = DrugExposure(
                    drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
                    person=person,
                    drug_concept=l3_concept or no_match,
                    drug_exposure_start_date=start,
                    drug_exposure_end_date=end,
                    drug_type_concept_id=CONCEPT_EHR_TYPE,
                    drug_source_value=(l3_concept.concept_name[:50] if l3_concept else 'Unknown'),
                    drug_source_concept=l3_concept,
                )
                de._skip_patient_record_refresh = True
                de.save()

                ep = Episode(
                    episode_id=next_pk(Episode, 'episode_id'),
                    person=person,
                    episode_concept=ehr_type,
                    episode_object_concept=l3_concept or no_match,
                    episode_type_concept=ehr_type,
                    episode_start_date=start,
                    episode_end_date=end,
                    episode_number=3,
                    episode_source_value='LOT-3',
                    episode_source_concept=l3_concept,
                )
                ep.save()

                EpisodeEvent.objects.get_or_create(
                    episode_id=ep.episode_id,
                    event_id=de.drug_exposure_id,
                    defaults={'episode_event_field_concept': ehr_type},
                )

                oc = outcome_concepts.get(outcome)
                if oc:
                    Observation.objects.create(
                        observation_id=next_pk(Observation, 'observation_id'),
                        person=person,
                        observation_concept=oc,
                        observation_date=end,
                        observation_type_concept_id=CONCEPT_EHR_TYPE,
                        value_as_string=outcome,
                        observation_source_value='LOT-3-outcome',
                    )
                lot3_end[pid] = end

        # ── Step 6: Create Death records for ~20% of patients ──────────────
        from omop_core.models import Death
        existing_deaths = set(Death.objects.filter(
            person_id__in=pids).values_list('person_id', flat=True))
        mortality_candidates = [pid for pid in pids if pid not in existing_deaths]
        n_deceased = max(0, round(len(records) * options['deceased_fraction']))
        n_deceased = max(0, n_deceased - len(existing_deaths))
        deceased_pids = set(rng.sample(mortality_candidates,
                                       min(n_deceased, len(mortality_candidates))))
        self.stdout.write(f"\nStep 6: Create Death records for {len(deceased_pids)} patients")

        # Death type concept: EHR record — use a generic administrative concept
        death_type_concept = (Concept.objects.filter(concept_id=32817).first()
                               or Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first())
        today = datetime.date(2026, 7, 12)

        if not dry_run:
            for pid in deceased_pids:
                # Use the latest episode end date as the basis
                last_end = lot3_end.get(pid) or lot2_info.get(pid, (None, None))[1] or lot1_end.get(pid)
                if not last_end:
                    last_end = diag_date.get(pid) or datetime.date(2020, 1, 1)
                death_date = _add_months(last_end, rng.randint(2, 18))
                # Don't set death in the future
                if death_date > today:
                    death_date = _add_months(last_end, rng.randint(1, 6))
                if death_date > today:
                    death_date = today

                person = Person.objects.get(person_id=pid)
                Death.objects.create(
                    person=person,
                    death_date=death_date,
                    death_type_concept=death_type_concept,
                )

        # ── Step 7: Propagate death_date to patient_record via raw SQL ─────
        self.stdout.write("\nStep 7: Update patient_record.death_date from Death table")
        if not dry_run:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE patient_record pr
                    SET death_date = d.death_date
                    FROM death d
                    WHERE d.person_id = pr.person_id
                      AND pr.person_id = ANY(%s)
                """, [list(pids)])
            self.stdout.write(f"  Updated death_date for {len(deceased_pids)} patients")

        # ── Step 8: Refresh PatientRecord for all patients ─────────────────
        self.stdout.write(f"\nStep 8: Refresh PatientRecord for {len(records)} patients")
        if not dry_run:
            refreshed = 0
            for record in records:
                try:
                    with transaction.atomic():
                        refresh_patient_record(record.person)
                    refreshed += 1
                    if refreshed % 10 == 0:
                        self.stdout.write(f"  {refreshed}/{len(records)} refreshed")
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(
                        f"  person_id={record.person_id}: {exc}"))
            self.stdout.write(f"  Done: {refreshed}/{len(records)} refreshed")

            # Re-apply death_date after refresh (refresh doesn't touch it)
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE patient_record pr
                    SET death_date = d.death_date
                    FROM death d
                    WHERE d.person_id = pr.person_id
                      AND pr.person_id = ANY(%s)
                """, [list(pids)])

        # ── Summary ────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(f"""
------------------------------------------------------------
{'DRY RUN — ' if dry_run else ''}Done
  LOT1 carboplatin fixed    : {carb_count}
  LOT2/3 episodes deleted   : {len(lot23_ep_ids)}
  LOT2 episodes created     : {len(lot2_pids)}
  LOT3 episodes created     : {len(lot3_pids)}
  Death records created     : {len(deceased_pids)}
  PatientRecords refreshed  : {len(records) if not dry_run else 0}
------------------------------------------------------------"""))
