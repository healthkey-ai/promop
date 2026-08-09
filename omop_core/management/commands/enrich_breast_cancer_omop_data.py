"""
Management command: enrich_breast_cancer_omop_data

Backfills OMOP source data for the breast-cancer cohort (ABC Foundation +
BBC Foundation orgs by default) so that patient_record_service.py's live
derivation from OMOP tables has real data to read, instead of always
returning empty for fields whose Measurement/Observation rows are missing
or carry null values.

Addresses three data gaps found while building benchmark_patient_record:
  1. ECOG / Karnofsky / "Stage group.clinical Cancer" Measurement rows exist
     for this cohort but every value column is null.
  2. Observation has zero rows at all for this cohort, so tobacco status
     (_get_behavior_data), tumor/metastasis staging / response-status
     Observations, and sleep duration (_get_wearable_data) always
     return empty.
  3. None of the six wearable Measurement LOINC codes appear for this
     cohort, so _get_wearable_data's Measurement-sourced metrics (steps,
     active minutes, resting HR, HRV, SpO2, respiratory rate) always
     return empty.

This is a stopgap for the *existing* ABC/BBC cohort. It does not fix the
underlying causes — see GitHub issues #200 (missing PatientRecord
extractors for fields with no derivation code at all, e.g. `stage`) and
#201 (import_fhir_bundle should populate these rows for future imports).

After enriching all selected people's OMOP rows, calls refresh_patient_record(person)
for each processed person so `patient_record` reflects the richer data. This
command DOES write to the database (unlike the read-only
benchmark_patient_record command).

Usage:
    python manage.py enrich_breast_cancer_omop_data --dry-run
    python manage.py enrich_breast_cancer_omop_data --confirm
    python manage.py enrich_breast_cancer_omop_data --confirm --org-slugs abc-foundation,bbc-foundation --limit 50
    python manage.py enrich_breast_cancer_omop_data --confirm --person-ids 1341,1410
"""
import random
import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from django.db.utils import InterfaceError, OperationalError

from omop_core.models import (
    Person, PatientRecord, Measurement, Observation, Concept, Vocabulary,
    Domain, ConceptClass, DrugExposure,
)
from omop_core.services.mappings import (
    WEARABLE_CONCEPT_CODE, WEARABLE_MIN_VALID_DAYS, CONCEPT_EHR_TYPE, CONCEPT_GENERIC_LAB,
)
from omop_core.services.pk import next_pk, next_pk_batch
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.lot_regimens import REGIMEN_CONCEPT_IDS, get_regimen_name
from omop_core.signals import suppress_patient_record_refresh


# SNOMED CT codes read by _get_behavior_data / _get_assessment_data but not
# present in this DB's Concept table at all (no SNOMED vocabulary loaded
# here — confirmed via direct query). Used as the concept_id too, since
# there's no existing Athena surrogate id to reuse and these numeric SNOMED
# codes are well outside this DB's existing concept_id range.
_TOBACCO_CODES = {
    '266919005': ('Never smoked tobacco', 0.7),
    '8517006':   ('Ex-smoker', 0.2),
    '77176002':  ('Current smoker', 0.1),
}
_RESPONSE_CODES = {
    '182840001': 'Complete Response',
    '182841002': 'Partial Response',
    '182843004': 'Stable Disease',
    '182842009': 'Progressive Disease',
}

# Rough clinical-stage -> (T, M) mapping, used only to synthesize a plausible
# tumor/metastasis Observation pair consistent with the patient's existing
# patient_record.stage value (LOINC 21905-5 / 21901-4, read by
# _get_assessment_data to set measurable_disease_by_recist_status).
_STAGE_TO_TM = {
    'I': ('T1', 'M0'), 'IA': ('T1', 'M0'), 'IB': ('T1', 'M0'),
    'II': ('T2', 'M0'), 'IIA': ('T2', 'M0'), 'IIB': ('T2', 'M0'),
    'III': ('T3', 'M0'), 'IIIA': ('T3', 'M0'), 'IIIB': ('T3', 'M0'), 'IIIC': ('T4', 'M0'),
    'IV': ('T4', 'M1'),
}

# Plausible ranges for synthesized wearable daily readings.
_WEARABLE_RANGES = {
    'steps': (2000, 12000),
    'active_minutes': (0, 90),
    'resting_hr': (55, 90),
    'hrv_sdnn': (20, 80),
    'spo2': (94.0, 99.0),
    'respiratory_rate': (12, 20),
    'sleep_duration': (5.5, 8.5),
}

_WEARABLE_DAYS = 30

_BC_HISTOLOGY_TYPES = [
    'Invasive ductal carcinoma of breast',
    'Invasive lobular carcinoma of breast',
    'Breast carcinoma, NOS',
]
_BC_MUTATION_LOINCS = [
    ('21636-6', 'BRCA1'),
    ('21637-4', 'BRCA2'),
    ('21667-1', 'TP53'),
    ('62318-1', 'PIK3CA'),
]
_BC_BEHAVIOR_MEASUREMENTS = {
    '72166-2': ['Never smoked tobacco', 'Ex-smoker', 'Current smoker'],
    '63640-7': (0, 40),
    '74013-4': ['No alcohol use', 'Occasional alcohol use', 'Heavy alcohol use'],
    '11286-7': (0, 21),
    '68516-4': ['Never', 'Sometimes', 'Weekly', 'Daily'],
    '89555-7': (0, 300),
    '88365-2': ['Balanced diet', 'Low carb', 'Mediterranean', 'Standard'],
    '93831-6': ['Good', 'Fair', 'Poor'],
    '73985-4': ['Low', 'Moderate', 'High'],
    '93033-9': ['Strong', 'Moderate', 'Limited'],
    '74165-2': ['Employed', 'Unemployed', 'Retired'],
    '82589-3': ['High school', 'College', 'Graduate degree'],
    '45404-1': ['Single', 'Married', 'Divorced', 'Widowed'],
    '76513-1': ['Commercial', 'Medicaid', 'Medicare', 'Self-pay'],
    '63512-8': (0, 6),
    '77243-3': (25000, 220000),
}
_BC_THERAPY_PLANS = {
    'early': [
        {'drugs': ['tamoxifen'], 'duration_days': 120},
        {'drugs': ['docetaxel', 'cyclophosphamide'], 'duration_days': 84},
        {'drugs': ['paclitaxel', 'trastuzumab', 'pertuzumab'], 'duration_days': 84},
    ],
    'advanced': [
        {'drugs': ['doxorubicin', 'cyclophosphamide', 'paclitaxel'], 'duration_days': 126},
        {'drugs': ['trastuzumab deruxtecan'], 'duration_days': 84},
        {'drugs': ['docetaxel', 'trastuzumab', 'pertuzumab'], 'duration_days': 84},
    ],
}


def _get_or_create_snomed_vocabulary():
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='SNOMED',
        defaults={
            'vocabulary_name': 'Systematic Nomenclature of Medicine - Clinical Terms',
            'vocabulary_reference': 'http://www.snomed.org',
            'vocabulary_version': 'SNOMED CT (synthetic, benchmark seed)',
            'vocabulary_concept_id': 0,
        },
    )
    return vocab


def _get_or_create_domain(domain_id):
    domain, _ = Domain.objects.get_or_create(
        domain_id=domain_id, defaults={'domain_name': domain_id, 'domain_concept_id': 0},
    )
    return domain


def _get_or_create_concept_class(concept_class_id):
    concept_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id=concept_class_id,
        defaults={'concept_class_name': concept_class_id, 'concept_class_concept_id': 0},
    )
    return concept_class


def _get_or_create_hemonc_vocabulary():
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='HemOnc',
        defaults={
            'vocabulary_name': 'HemOnc',
            'vocabulary_reference': 'https://hemonc.org',
            'vocabulary_version': 'HemOnc (synthetic, benchmark seed)',
            'vocabulary_concept_id': 0,
        },
    )
    return vocab


def _get_or_create_regimen_concept(concept_id, regimen_key):
    """Get or create the HemOnc regimen Concept for a known concept_id.

    Therapy backfill resolves a regimen's HemOnc concept_id from
    REGIMEN_CONCEPT_IDS; on a DB where that Concept row was never loaded we
    create it so the backfilled DrugExposure references a real regimen concept
    (and derivation can surface it as first_line_therapy_id).
    """
    existing = Concept.objects.filter(concept_id=concept_id).first()
    if existing:
        return existing
    name = get_regimen_name(regimen_key) or ' + '.join(sorted(regimen_key)).title()
    return Concept.objects.create(
        concept_id=concept_id,
        concept_name=name,
        vocabulary=_get_or_create_hemonc_vocabulary(),
        domain=_get_or_create_domain('Drug'),
        concept_class=_get_or_create_concept_class('Regimen'),
        standard_concept='S',
        concept_code=str(concept_id),
        valid_start_date='1970-01-01',
        valid_end_date='2099-12-31',
    )


def _get_or_create_concept(concept_code, concept_name):
    """Get or create a SNOMED Concept row keyed by concept_code, using the
    numeric code itself as concept_id (see module docstring for why).

    Also ensures the Vocabulary/Domain/ConceptClass rows it references exist —
    true on the real staging DB (seeded separately), but not on a freshly
    migrated local/test DB, where these reference tables start empty."""
    concept_id = int(concept_code)
    try:
        existing = Concept.objects.get(concept_id=concept_id)
    except Concept.DoesNotExist:
        _get_or_create_snomed_vocabulary()
        _get_or_create_domain('Observation')
        _get_or_create_concept_class('Clinical Observation')
        return Concept.objects.create(
            concept_id=concept_id,
            concept_name=concept_name,
            domain_id='Observation',
            vocabulary_id='SNOMED',
            concept_class_id='Clinical Observation',
            standard_concept='S',
            concept_code=concept_code,
            valid_start_date='1970-01-01',
            valid_end_date='2099-12-31',
        )
    if existing.concept_code != concept_code:
        raise CommandError(
            f'Concept id {concept_id} already exists for a different concept_code '
            f'({existing.concept_code!r}, expected {concept_code!r}) — refusing to overwrite.'
        )
    return existing


class _IdAllocator:
    """Hands out sequence-backed PKs for OMOP tables with explicit IDs."""

    def __init__(self, model, pk_field):
        self.model = model
        self.pk_field = pk_field

    def take(self):
        return next_pk(self.model, self.pk_field)

    def take_batch(self, count):
        return next_pk_batch(self.model, self.pk_field, count)


class Command(BaseCommand):
    help = (
        'Backfill OMOP Measurement/Observation rows for the breast-cancer '
        'cohort so live OMOP derivation has real data to read, then refresh '
        'patient_record from the enriched data.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slugs', default='abc-foundation,bbc-foundation',
            help='Comma-separated organization slugs to scope the cohort to.',
        )
        parser.add_argument(
            '--person-ids', default='',
            help='Comma-separated person_ids — overrides --org-slugs cohort selection.',
        )
        parser.add_argument(
            '--start-after-person-id',
            type=int,
            default=None,
            help='Resume processing after this person_id.',
        )
        parser.add_argument(
            '--db-retries',
            type=int,
            default=3,
            help='Number of retries per patient for transient database connection failures.',
        )
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print planned inserts/updates without writing.',
        )
        parser.add_argument(
            '--confirm', action='store_true',
            help=(
                'Required to actually write synthetic data. '
                'This command writes randomized synthetic OMOP rows — '
                'pass --confirm to acknowledge this is intentional.'
            ),
        )
        parser.add_argument(
            '--refresh-only',
            action='store_true',
            help='Skip enrichment and only refresh PatientRecord for the selected cohort.',
        )
        parser.add_argument(
            '--refresh-all-org-patients',
            action='store_true',
            help=(
                'With --refresh-only and --org-slugs, refresh all PatientRecords in the '
                'selected orgs instead of only records still matching disease__icontains=breast.'
            ),
        )

    def _write_progress(self, message, stream=None):
        stream = stream or self.stdout
        stream.write(message)
        stream.flush()

    @staticmethod
    def _fmt_elapsed(seconds):
        """Return a human-readable elapsed string, e.g. '2m 14s' or '45s'."""
        seconds = int(seconds)
        if seconds < 60:
            return f'{seconds}s'
        return f'{seconds // 60}m {seconds % 60:02d}s'

    @staticmethod
    def _eta_str(elapsed, done, total):
        """Return an ETA string based on average rate so far, or '' if too early."""
        if done < 2:
            return ''
        avg = elapsed / done
        remaining = avg * (total - done)
        return f'  ETA ~{Command._fmt_elapsed(remaining)}'

    def _run_with_db_retries(self, label, retries, func):
        """Retry func() on transient connection failures.

        close_old_connections() is only called AFTER a failure, not before
        the first attempt — calling it unconditionally up front closes any
        connection currently inside an atomic block (e.g. pytest-django's
        per-test transaction wrapping), breaking tests even when the
        connection was perfectly healthy.
        """
        attempt = 1
        while True:
            try:
                return func()
            except (OperationalError, InterfaceError) as exc:
                close_old_connections()
                if attempt > retries:
                    raise
                self._write_progress(
                    self.style.WARNING(
                        f'    {label}: database connection failed '
                        f'(attempt {attempt}/{retries}); retrying: {exc}'
                    ),
                    stream=self.stderr,
                )
                attempt += 1

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        refresh_only = options['refresh_only']
        refresh_all_org_patients = options['refresh_all_org_patients']
        start_after_person_id = options['start_after_person_id']
        db_retries = options['db_retries']
        job_start = time.monotonic()

        if not dry_run and not refresh_only and not options['confirm']:
            raise CommandError(
                'This command writes synthetic OMOP rows into the database.\n'
                'Pass --confirm to acknowledge this is intentional, or --dry-run to preview.\n'
                'Re-run with: manage.py enrich_breast_cancer_omop_data --confirm ...'
            )

        if options['person_ids']:
            self._write_progress('Selecting patients from --person-ids...')
            refresh_person_ids = [int(x) for x in options['person_ids'].split(',') if x.strip()]
            person_ids = refresh_person_ids
            if start_after_person_id is not None:
                person_ids = [person_id for person_id in person_ids if person_id > start_after_person_id]
        else:
            org_slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
            self._write_progress(
                f'Selecting breast-cancer cohort for org slug(s): {", ".join(org_slugs)}...'
            )
            org_qs = (
                PatientRecord.objects
                .filter(organization__slug__in=org_slugs)
            )
            qs = org_qs.filter(disease__icontains='breast')
            refresh_base_qs = org_qs if refresh_only and refresh_all_org_patients else qs
            refresh_qs = refresh_base_qs.order_by('person_id').values_list('person_id', flat=True)
            refresh_person_ids = list(refresh_qs[:options['limit']]) if options['limit'] else list(refresh_qs)
            if start_after_person_id is not None:
                qs = qs.filter(person_id__gt=start_after_person_id)
            qs = qs.order_by('person_id').values_list('person_id', flat=True)
            person_ids = list(qs[:options['limit']]) if options['limit'] else list(qs)

        if refresh_only:
            person_ids = []

        if not person_ids and not refresh_person_ids:
            raise CommandError('No matching patients found for the given cohort.')

        total = len(person_ids)
        self._write_progress(
            f'Enriching OMOP data for {total} patient(s)'
            f'{" (dry-run)" if dry_run else ""}...'
        )

        self._write_progress('Initializing OMOP ID allocators...')
        measurement_ids = _IdAllocator(Measurement, 'measurement_id')
        observation_ids = _IdAllocator(Observation, 'observation_id')
        drug_exposure_ids = _IdAllocator(DrugExposure, 'drug_exposure_id')

        if not dry_run:
            # Ensure the SNOMED codes _get_behavior_data/_get_assessment_data
            # read actually exist as Concept rows before touching any person.
            self._write_progress('Ensuring required SNOMED concepts exist...')
            for code, (name, _weight) in _TOBACCO_CODES.items():
                _get_or_create_concept(code, name)
            for code, name in _RESPONSE_CODES.items():
                _get_or_create_concept(code, name)

        # Pre-fetch all wearable LOINC concepts in one query. This avoids N+1
        # Concept.objects.get() calls inside _create_missing_wearable_measurements
        # and provides an early abort if any required concept is missing.
        self._write_progress('Pre-fetching wearable LOINC concepts...')
        wearable_concepts = {}
        for metric_key, loinc_code in WEARABLE_CONCEPT_CODE.items():
            concept = Concept.objects.filter(concept_code=loinc_code).first()
            if concept is None:
                raise CommandError(
                    f'Required wearable LOINC concept {loinc_code!r} ({metric_key}) '
                    f'not found in Concept table. Run seed_omop_concepts first.'
                )
            wearable_concepts[metric_key] = concept

        ehr_type_concept_id = CONCEPT_EHR_TYPE
        counts = {
            'perf_backfilled': 0,
            'obs_created': 0,
            'wearable_rows_created': 0,
            'bc_rows_created': 0,
            'therapy_rows_created': 0,
            'refreshed': 0,
        }
        processed_persons = []
        skipped = 0

        # Phase 1: Enrichment
        # refresh_patient_record() is suppressed during this phase so
        # signal-triggered refreshes don't fire on every individual write.
        # All refreshes are deferred to Phase 2 below, after every person's
        # transactions have committed.
        self._write_progress(f'\nPhase 1/2: Enrichment ({total} patients)')
        phase1_start = time.monotonic()

        with suppress_patient_record_refresh():
            for index, person_id in enumerate(person_ids, start=1):
                elapsed_so_far = time.monotonic() - phase1_start
                eta = self._eta_str(elapsed_so_far, index - 1, total)
                self._write_progress(
                    f'  [{index}/{total}  {index / total * 100:5.1f}%]{eta}  person_id={person_id}'
                )
                person_start = time.monotonic()

                try:
                    person = Person.objects.get(person_id=person_id)
                except Person.DoesNotExist:
                    self._write_progress(
                        self.style.WARNING(f'    person_id={person_id}: not found, skipping'),
                        stream=self.stderr,
                    )
                    skipped += 1
                    continue

                try:
                    record = PatientRecord.objects.get(person=person)
                except PatientRecord.DoesNotExist:
                    record = None

                def enrich_person():
                    with transaction.atomic():
                        perf_backfilled = self._backfill_performance_and_stage(
                            person, record, dry_run,
                        )
                        obs_created = self._create_missing_observations(
                            person, record, observation_ids, ehr_type_concept_id, dry_run,
                        )
                        wearable_rows_created = self._create_missing_wearable_measurements(
                            person, measurement_ids, observation_ids, ehr_type_concept_id,
                            dry_run, wearable_concepts,
                        )
                        bc_rows_created = self._create_missing_bc_measurements(
                            person, record, measurement_ids, ehr_type_concept_id, dry_run,
                        )
                        therapy_rows_created = self._create_missing_bc_therapy_rows(
                            person, record, drug_exposure_ids, ehr_type_concept_id, dry_run,
                        )
                        if dry_run:
                            transaction.set_rollback(True)
                        return (
                            perf_backfilled,
                            obs_created,
                            wearable_rows_created,
                            bc_rows_created,
                            therapy_rows_created,
                        )

                (
                    perf_backfilled,
                    obs_created,
                    wearable_rows_created,
                    bc_rows_created,
                    therapy_rows_created,
                ) = self._run_with_db_retries(
                    f'person_id={person_id}',
                    db_retries,
                    enrich_person,
                )
                counts['perf_backfilled'] += perf_backfilled
                counts['obs_created'] += obs_created
                counts['wearable_rows_created'] += wearable_rows_created
                counts['bc_rows_created'] += bc_rows_created
                counts['therapy_rows_created'] += therapy_rows_created
                if not dry_run:
                    processed_persons.append(person)

                person_elapsed = time.monotonic() - person_start
                self._write_progress(
                    f'    done in {person_elapsed:.1f}s - '
                    f'perf/stage={perf_backfilled}  obs={obs_created}  '
                    f'wearable={wearable_rows_created}  bc={bc_rows_created}  therapy={therapy_rows_created}'
                )

        phase1_elapsed = time.monotonic() - phase1_start
        self._write_progress(
            f'\n  Phase 1 complete: {self._fmt_elapsed(phase1_elapsed)} elapsed, '
            f'{len(processed_persons)} enriched, {skipped} skipped.'
        )

        # Phase 2: PatientRecord refresh
        # All enrichment writes above have committed before we reach this point.
        # We now rebuild each patient_record from the freshly written OMOP rows.
        if not dry_run and refresh_person_ids:
            n_refresh = len(refresh_person_ids)
            self._write_progress(
                f'\nPhase 2/2: PatientRecord refresh ({n_refresh} patients)'
            )
            phase2_start = time.monotonic()

            for index, person_id in enumerate(refresh_person_ids, start=1):
                elapsed_so_far = time.monotonic() - phase2_start
                eta = self._eta_str(elapsed_so_far, index - 1, n_refresh)
                self._write_progress(
                    f'  [{index}/{n_refresh}  {index / n_refresh * 100:5.1f}%]{eta}'
                    f'  person_id={person_id}'
                )
                refresh_start = time.monotonic()

                try:
                    person = Person.objects.get(person_id=person_id)
                except Person.DoesNotExist:
                    self._write_progress(
                        self.style.WARNING(f'    person_id={person_id}: not found, skipping refresh'),
                        stream=self.stderr,
                    )
                    continue

                self._run_with_db_retries(
                    f'person_id={person_id} refresh',
                    db_retries,
                    lambda p=person: refresh_patient_record(p),
                )
                counts['refreshed'] += 1
                self._write_progress(
                    f'    refreshed in {time.monotonic() - refresh_start:.1f}s'
                )

            phase2_elapsed = time.monotonic() - phase2_start
            self._write_progress(
                f'\n  Phase 2 complete: {self._fmt_elapsed(phase2_elapsed)} elapsed, '
                f'{counts["refreshed"]} records refreshed.'
            )

        total_elapsed = time.monotonic() - job_start
        self._write_progress(self.style.SUCCESS(
            f'\n{"-" * 60}\n'
            f"{'DRY RUN - nothing written' if dry_run else 'Done'}"
            f'  ({self._fmt_elapsed(total_elapsed)} total)\n'
            f'  Patients selected       : {total}\n'
            f'  Patients enriched       : {len(processed_persons)}\n'
            f'  Patients skipped        : {skipped}\n'
            f'  Perf/stage backfilled   : {counts["perf_backfilled"]}\n'
            f'  Observation rows added  : {counts["obs_created"]}\n'
            f'  Wearable rows added     : {counts["wearable_rows_created"]}\n'
            f'  BC rows added           : {counts["bc_rows_created"]}\n'
            f'  Therapy rows added      : {counts["therapy_rows_created"]}\n'
            f'  PatientRecords refreshed: {counts["refreshed"]}\n'
            f'{"-" * 60}'
        ))

    # ------------------------------------------------------------------

    def _backfill_performance_and_stage(self, person, record, dry_run):
        """Fill null value_as_number/value_as_string on existing ECOG /
        Karnofsky / stage Measurement rows. Idempotent: only touches rows
        that are still null."""
        updated = 0
        measurements = Measurement.objects.filter(
            person=person,
            measurement_concept__concept_name__in=[
                'ECOG Performance Status score',
                'Karnofsky Performance Status score',
                'Stage group.clinical Cancer',
            ],
        ).select_related('measurement_concept')

        for m in measurements:
            name = m.measurement_concept.concept_name
            changed = False
            if name == 'ECOG Performance Status score' and m.value_as_number is None:
                m.value_as_number = random.choice([0, 1, 2])
                changed = True
            elif name == 'Karnofsky Performance Status score' and m.value_as_number is None:
                m.value_as_number = random.choice([70, 80, 90, 100])
                changed = True
            elif name == 'Stage group.clinical Cancer' and not m.value_as_string:
                if record and record.stage:
                    m.value_as_string = f'Stage {record.stage}'
                    changed = True

            if changed:
                updated += 1
                if not dry_run:
                    m.save(update_fields=['value_as_number', 'value_as_string'])

        return updated

    def _create_missing_observations(self, person, record, observation_ids,
                                      ehr_type_concept_id, dry_run):
        """Insert one tobacco-status, one T/M-staging pair, and one
        response-status Observation row per person, skipping any concept the
        person already has an Observation row for (idempotent)."""
        created = 0
        existing_concept_ids = set(
            Observation.objects.filter(person=person)
            .values_list('observation_concept_id', flat=True)
        )
        obs_date = (record.diagnosis_date if record and record.diagnosis_date else date.today())

        def _make(concept_code, value_as_string):
            nonlocal created
            concept = Concept.objects.filter(concept_code=concept_code).first()
            if concept is None:
                if not dry_run:
                    raise CommandError(
                        f'Concept code {concept_code!r} not found — expected it to have '
                        f'been created up front in handle().'
                    )
                created += 1  # dry-run: concept doesn't exist yet, but would be created
                return
            if concept.concept_id in existing_concept_ids:
                return
            created += 1
            if dry_run:
                return
            Observation.objects.create(
                observation_id=observation_ids.take(),
                person=person,
                observation_concept=concept,
                observation_date=obs_date,
                observation_type_concept_id=ehr_type_concept_id,
                value_as_string=value_as_string,
            )
            existing_concept_ids.add(concept.concept_id)

        # Tobacco status — weighted random pick. Guard on the whole category,
        # not just the randomly-chosen code: a rerun that happens to pick a
        # *different* tobacco code than a prior run would otherwise pass the
        # per-concept check in _make() and add a second, contradictory
        # tobacco-status observation instead of being a no-op.
        tobacco_concept_ids = {int(code) for code in _TOBACCO_CODES}
        if not existing_concept_ids & tobacco_concept_ids:
            codes, weights = zip(*[(c, w) for c, (_, w) in _TOBACCO_CODES.items()])
            tobacco_code = random.choices(codes, weights=weights, k=1)[0]
            _make(tobacco_code, None)

        # Tumor/metastasis staging, consistent with the patient's existing
        # stage. Deterministic (not random), so the per-concept check in
        # _make() is sufficient here.
        if record and record.stage and record.stage in _STAGE_TO_TM:
            t_val, m_val = _STAGE_TO_TM[record.stage]
            _make('21905-5', t_val)
            _make('21901-4', m_val)

        # Best response — same category-guard reasoning as tobacco above.
        response_concept_ids = {int(code) for code in _RESPONSE_CODES}
        if not existing_concept_ids & response_concept_ids:
            response_code = random.choices(
                list(_RESPONSE_CODES.keys()), weights=[0.3, 0.35, 0.25, 0.10], k=1,
            )[0]
            _make(response_code, None)

        return created

    def _measurement_concept_for_code(self, loinc_code):
        concept = Concept.objects.filter(concept_code=loinc_code).first()
        if concept:
            return concept
        return Concept.objects.filter(concept_id=CONCEPT_GENERIC_LAB).first()

    @staticmethod
    def _bc_rng(person_id):
        return random.Random(f'bc-enrich:{person_id}')

    def _create_missing_bc_measurements(self, person, record, measurement_ids,
                                        ehr_type_concept_id, dry_run):
        """Backfill missing breast-cancer-specific Measurement rows.

        Covers gaps confirmed on the SYNTHEA-BC cohort:
          - histologic type
          - mutation rows
          - numeric Ki-67
          - lifestyle / behavior measurements
        """
        rng = self._bc_rng(person.person_id)
        created = 0
        measurements_to_create = []
        obs_date = record.diagnosis_date if record and record.diagnosis_date else date.today()

        existing_by_code = {}
        for code, value_num, value_str in Measurement.objects.filter(person=person).values_list(
            'measurement_source_value', 'value_as_number', 'value_as_string'
        ):
            existing_by_code.setdefault(code, []).append((value_num, value_str))

        def _queue_measurement(loinc_code, value_as_number=None, value_as_string=None):
            nonlocal created
            concept = self._measurement_concept_for_code(loinc_code)
            if concept is None:
                return
            created += 1
            if dry_run:
                return
            measurements_to_create.append(Measurement(
                person=person,
                measurement_concept=concept,
                measurement_date=obs_date,
                measurement_type_concept_id=ehr_type_concept_id,
                measurement_source_value=loinc_code,
                value_as_number=value_as_number,
                value_as_string=value_as_string,
            ))

        if '59847-4' not in existing_by_code:
            histology = _BC_HISTOLOGY_TYPES[person.person_id % len(_BC_HISTOLOGY_TYPES)]
            _queue_measurement('59847-4', value_as_string=histology)

        has_numeric_ki67 = any(value_num is not None for value_num, _ in existing_by_code.get('85319-2', []))
        if not has_numeric_ki67:
            _queue_measurement('85319-2', value_as_number=rng.randint(5, 75))

        has_mutation = any(existing_by_code.get(code) for code, _gene in _BC_MUTATION_LOINCS)
        if not has_mutation:
            mutation_code, gene_name = _BC_MUTATION_LOINCS[person.person_id % len(_BC_MUTATION_LOINCS)]
            variant = [
                f'{gene_name} pathogenic variant',
                f'{gene_name} exon deletion',
                f'{gene_name} missense variant',
            ][person.person_id % 3]
            _queue_measurement(mutation_code, value_as_string=variant)

        for loinc_code, choices in _BC_BEHAVIOR_MEASUREMENTS.items():
            if loinc_code in existing_by_code:
                continue
            if isinstance(choices, tuple):
                value_as_number = rng.randint(choices[0], choices[1])
                _queue_measurement(loinc_code, value_as_number=value_as_number)
            else:
                value_as_string = choices[person.person_id % len(choices)]
                _queue_measurement(loinc_code, value_as_string=value_as_string)

        if measurements_to_create:
            for measurement, measurement_id in zip(
                measurements_to_create,
                measurement_ids.take_batch(len(measurements_to_create)),
            ):
                measurement.measurement_id = measurement_id
            Measurement.objects.bulk_create(measurements_to_create, batch_size=500)

        return created

    def _create_missing_bc_therapy_rows(self, person, record, drug_exposure_ids,
                                        ehr_type_concept_id, dry_run):
        """Backfill missing DrugExposure rows for patients with no therapies at all."""
        if DrugExposure.objects.filter(person=person).exists():
            return 0

        rng = self._bc_rng(person.person_id)
        stage = (record.stage or '') if record else ''
        therapy_bucket = 'advanced' if any(s in stage for s in ('III', 'IV')) else 'early'
        regimen = rng.choice(_BC_THERAPY_PLANS[therapy_bucket])
        start_date = record.diagnosis_date if record and record.diagnosis_date else date.today()

        regimen_key = frozenset(drug.lower().strip() for drug in regimen['drugs'])
        concept_id = REGIMEN_CONCEPT_IDS.get(regimen_key)
        concept = Concept.objects.filter(concept_id=concept_id).first() if concept_id else None
        if concept is None:
            concept = (
                Concept.objects
                .filter(concept_name__in=[drug.title() for drug in regimen['drugs']])
                .order_by('concept_id')
                .first()
            )
        # Known HemOnc regimen but its Concept row isn't loaded on this DB —
        # create it so backfill always yields a resolvable regimen concept.
        if concept is None and concept_id:
            concept = _get_or_create_regimen_concept(concept_id, regimen_key)

        if concept is None:
            return 0

        if dry_run:
            return 1

        exposure = DrugExposure(
            drug_exposure_id=drug_exposure_ids.take(),
            person=person,
            drug_concept=concept,
            drug_exposure_start_date=start_date,
            drug_exposure_end_date=start_date + timedelta(days=regimen['duration_days']),
            drug_type_concept_id=ehr_type_concept_id,
            drug_source_value=concept.concept_name[:50],
        )
        exposure.save()
        return 1

    def _create_missing_wearable_measurements(self, person, measurement_ids, observation_ids,
                                                ehr_type_concept_id, dry_run, wearable_concepts):
        """Insert up to 30 days of synthetic daily readings for each wearable
        metric the person doesn't already have WEARABLE_MIN_VALID_DAYS worth
        of (idempotent — tops up rather than duplicating). Sleep duration is
        Observation-sourced per _get_wearable_data; everything else is
        Measurement-sourced.

        wearable_concepts: {metric_key: Concept} pre-fetched in handle() — avoids
        N+1 Concept.objects.get() calls inside the per-person enrichment loop."""
        created = 0
        today = date.today()
        measurements_to_create = []
        observations_to_create = []

        for metric_key, loinc_code in WEARABLE_CONCEPT_CODE.items():
            if metric_key == 'sleep_duration':
                continue

            concept = wearable_concepts[metric_key]  # pre-fetched — no DB query here
            existing_days = set(
                Measurement.objects.filter(
                    person=person, measurement_concept=concept,
                ).values_list('measurement_date', flat=True)
            )
            if len(existing_days) >= WEARABLE_MIN_VALID_DAYS:
                continue

            lo, hi = _WEARABLE_RANGES[metric_key]
            is_float = metric_key in ('hrv_sdnn', 'spo2')
            for day_offset in range(_WEARABLE_DAYS):
                d = today - timedelta(days=day_offset)
                if d in existing_days:
                    continue
                value = random.uniform(lo, hi) if is_float else random.randint(lo, hi)
                created += 1
                if dry_run:
                    continue
                measurements_to_create.append(Measurement(
                    person=person,
                    measurement_concept=concept,
                    measurement_date=d,
                    measurement_type_concept_id=ehr_type_concept_id,
                    value_as_number=value,
                ))

        sleep_concept = wearable_concepts['sleep_duration']  # pre-fetched — no DB query here
        existing_sleep_days = set(
            Observation.objects.filter(
                person=person, observation_concept=sleep_concept,
            ).values_list('observation_date', flat=True)
        )
        if len(existing_sleep_days) < WEARABLE_MIN_VALID_DAYS:
            lo, hi = _WEARABLE_RANGES['sleep_duration']
            for day_offset in range(_WEARABLE_DAYS):
                d = today - timedelta(days=day_offset)
                if d in existing_sleep_days:
                    continue
                created += 1
                if dry_run:
                    continue
                observations_to_create.append(Observation(
                    person=person,
                    observation_concept=sleep_concept,
                    observation_date=d,
                    observation_type_concept_id=ehr_type_concept_id,
                    value_as_number=round(random.uniform(lo, hi), 1),
                ))

        if measurements_to_create:
            for measurement, measurement_id in zip(
                measurements_to_create,
                measurement_ids.take_batch(len(measurements_to_create)),
            ):
                measurement.measurement_id = measurement_id
            Measurement.objects.bulk_create(measurements_to_create, batch_size=500)
        if observations_to_create:
            for observation, observation_id in zip(
                observations_to_create,
                observation_ids.take_batch(len(observations_to_create)),
            ):
                observation.observation_id = observation_id
            Observation.objects.bulk_create(observations_to_create, batch_size=500)

        return created
