"""
Management command: benchmark_patient_record

Benchmarks reading the materialized PatientRecord table against deriving
the same PatientRecord fields live from raw OMOP tables, for a scoped
PatientRecord cohort.

Why this comparison, and why it's read-only: EXACT's trial-matching cost is
identical no matter where the patient-data dict came from — the only
variable worth measuring is how the PatientRecord-shaped row gets built.
The derivation functions in patient_record_service.py are plain read-only
queries (they return dicts; the only .save() call lives in
refresh_patient_record itself, which this command does not call), so no
dry-run/rollback wrapper is needed.

Section functions mirror refresh_patient_record's extractor list. That keeps
the benchmark disease-aware in practice: PatientRecord reads the materialized
row, and the OMOP-direct path performs the same read-only section derivation
refresh_patient_record would perform before saving that row.

Cohort selection deliberately goes through PatientRecord.organization (org
attribution doesn't exist anywhere else — Person/OMOP tables carry no org
attribution at all) — this is an untimed, one-time setup step, not part of
either measured path.

Usage:
    python manage.py benchmark_patient_record
    python manage.py benchmark_patient_record --limit 50 --repeat 3
    python manage.py benchmark_patient_record --output results.json
"""
import json
import statistics
import time

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Person, PatientRecord
from omop_core.services.patient_record_service import (
    _get_demographics, _get_location_data, _get_disease_data,
    _get_treatment_data, _get_vitals_data, _get_biomarker_data,
    _get_staging_data, _get_social_data, _get_behavior_data, _get_infection_data,
    _get_assessment_data, _get_laboratory_data, _get_performance_data,
    _get_genetic_mutations, _get_cll_data, _get_lymphoma_data,
    _get_bc_clinical_data, _get_prior_procedures, _get_wearable_data,
    _compute_derived_fields,
)

# Keep this list in sync with refresh_patient_record(), but never call
# refresh_patient_record() itself because that saves PatientRecord.
_REFRESH_SECTIONS = [
    _get_demographics, _get_location_data, _get_disease_data,
    _get_treatment_data, _get_vitals_data, _get_biomarker_data,
    _get_staging_data,
    _get_social_data, _get_behavior_data, _get_infection_data,
    _get_assessment_data, _get_laboratory_data, _get_performance_data,
    _get_genetic_mutations, _get_cll_data, _get_lymphoma_data,
    _get_bc_clinical_data, _get_prior_procedures, _get_wearable_data,
]


def _derive_patient_record_fields(person) -> dict:
    """Live OMOP derivation of PatientRecord fields.

    Read-only — never calls PatientRecord.save(). Mirrors the section-calling
    loop in refresh_patient_record().
    """
    data = {}
    for section_fn in _REFRESH_SECTIONS:
        data.update(section_fn(person))

    # _compute_derived_fields mutates a PatientRecord instance in place; use
    # an unsaved, unpersisted one scoped to this person so tp53_disruption
    # etc. compute without any DB write.
    scratch = PatientRecord(person=person)
    for field, value in data.items():
        if hasattr(scratch, field):
            setattr(scratch, field, value)
    _compute_derived_fields(scratch)
    data['tp53_disruption'] = scratch.tp53_disruption

    return data


def _cohort_person_ids(org_slugs, person_ids_arg, disease_filter, limit):
    if person_ids_arg:
        return [int(x) for x in person_ids_arg.split(',') if x.strip()]
    qs = (
        PatientRecord.objects
        .filter(organization__slug__in=org_slugs)
        .order_by('person_id')
    )
    if disease_filter:
        qs = qs.filter(disease__icontains=disease_filter)
    qs = qs.values_list('person_id', flat=True)
    return list(qs[:limit]) if limit else list(qs)


def _stats(samples_sec):
    if not samples_sec:
        return {}
    ordered = sorted(samples_sec)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    return {
        'n': len(ordered),
        'mean_ms': round(statistics.mean(ordered) * 1000, 3),
        'median_ms': round(statistics.median(ordered) * 1000, 3),
        'p95_ms': round(ordered[p95_index] * 1000, 3),
        'min_ms': round(ordered[0] * 1000, 3),
        'max_ms': round(ordered[-1] * 1000, 3),
    }


class Command(BaseCommand):
    help = (
        'Benchmark reading PatientRecord (materialized) vs. deriving the '
        'same PatientRecord fields live from OMOP tables.'
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
            '--disease-filter', default='breast',
            help=(
                'Optional case-insensitive PatientRecord.disease substring filter. '
                'Use "" to include all diseases in the scoped org/person cohort.'
            ),
        )
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument(
            '--repeat', type=int, default=1,
            help='Repeat each timed pass N times for more samples (default: 1).',
        )
        parser.add_argument('--output', default='', help='Write raw stats JSON to this path.')

    def handle(self, *args, **options):
        org_slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
        person_ids = _cohort_person_ids(
            org_slugs,
            options['person_ids'],
            options['disease_filter'].strip(),
            options['limit'],
        )

        if not person_ids:
            raise CommandError('No matching patients found for the given cohort.')

        repeat = options['repeat']
        self.stdout.write(
            f'Benchmarking {len(person_ids)} patient(s), '
            f'{repeat} repeat pass(es) per path...'
        )

        # Untimed warm-up: touch both PatientRecord and the OMOP tables for
        # every cohort person_id so neither timed pass gets an unfair
        # cold-cache advantage from running first.
        for pid in person_ids:
            try:
                PatientRecord.objects.select_related('person').get(person_id=pid)
            except PatientRecord.DoesNotExist:
                pass
            try:
                person = Person.objects.get(person_id=pid)
                _derive_patient_record_fields(person)
            except Person.DoesNotExist:
                pass

        patient_record_times = []
        omop_times = []
        field_coverage = []

        for _ in range(repeat):
            for pid in person_ids:
                try:
                    t0 = time.perf_counter()
                    PatientRecord.objects.select_related('person').get(person_id=pid)
                    patient_record_times.append(time.perf_counter() - t0)
                except PatientRecord.DoesNotExist:
                    pass

        for _ in range(repeat):
            for pid in person_ids:
                try:
                    person = Person.objects.get(person_id=pid)
                except Person.DoesNotExist:
                    continue
                t0 = time.perf_counter()
                fields = _derive_patient_record_fields(person)
                omop_times.append(time.perf_counter() - t0)
                populated = sum(1 for v in fields.values() if v not in (None, '', [], {}))
                field_coverage.append(populated)

        if not patient_record_times and not omop_times:
            raise CommandError(
                f'None of the given person_ids {person_ids} resolved to an actual '
                f'Person/PatientRecord — no timing samples collected.'
            )

        pr_stats = _stats(patient_record_times)
        omop_stats = _stats(omop_times)

        self.stdout.write(self.style.SUCCESS(f'\npatient_record read: {pr_stats}'))
        self.stdout.write(self.style.SUCCESS(f'OMOP-direct derive:  {omop_stats}'))

        if pr_stats.get('mean_ms') and omop_stats.get('mean_ms'):
            speedup = omop_stats['mean_ms'] / pr_stats['mean_ms']
            self.stdout.write(self.style.SUCCESS(
                f'\npatient_record is ~{speedup:.1f}x faster than live OMOP derivation '
                f'(mean {pr_stats["mean_ms"]}ms vs {omop_stats["mean_ms"]}ms)'
            ))

        if field_coverage:
            self.stdout.write(
                f'Avg populated fields per OMOP derivation: '
                f'{statistics.mean(field_coverage):.1f} (out of {len(_REFRESH_SECTIONS)} sections called)'
            )

        if options['output']:
            with open(options['output'], 'w') as f:
                json.dump({
                    'patient_record': pr_stats,
                    'omop_direct': omop_stats,
                    'person_ids': person_ids,
                    'repeat': repeat,
                    'disease_filter': options['disease_filter'],
                }, f, indent=2)
            self.stdout.write(self.style.SUCCESS(f'\nResults written to {options["output"]}'))
