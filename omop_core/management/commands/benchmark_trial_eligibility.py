"""
Management command: benchmark_trial_eligibility

Benchmarks a 20-criterion trial eligibility pull from raw OMOP tables versus
the same 20 fields materialized on PatientRecord.

This is narrower than benchmark_patient_record.py: it does not re-derive the
full PatientRecord projection. Instead it measures the query shape that trial
matching cares about most:

    raw OMOP tables -> one patient eligibility row
    PatientRecord    -> the same row from a flat projection

Usage:
    python manage.py benchmark_trial_eligibility --org-slugs synthea-mm --limit 10
    python manage.py benchmark_trial_eligibility --person-ids 123,456
    python manage.py benchmark_trial_eligibility --output results.json
"""
import json
import statistics
import time
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db.models import (
    BooleanField,
    CharField,
    DecimalField,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce

from omop_core.models import ConditionOccurrence, Measurement, Observation, PatientRecord, Person


TRIAL_ELIGIBILITY_FIELDS = [
    'patient_age',
    'gender',
    'disease',
    'stage',
    'ecog_performance_status',
    'karnofsky_performance_score',
    'hemoglobin_g_dl',
    'platelet_count_thousand_per_ul',
    'anc_thousand_per_ul',
    'wbc_count_thousand_per_ul',
    'serum_creatinine_mg_dl',
    'creatinine_clearance_ml_min',
    'serum_calcium_mg_dl',
    'bilirubin_total_mg_dl',
    'ast_u_l',
    'alt_u_l',
    'albumin_g_dl',
    'her2_status',
    'estrogen_receptor_status',
    'progesterone_receptor_status',
]

_CANCER_NAME_FILTERS = (
    Q(condition_concept__concept_name__icontains='cancer')
    | Q(condition_concept__concept_name__icontains='neoplasm')
    | Q(condition_concept__concept_name__icontains='malignant')
    | Q(condition_concept__concept_name__icontains='lymphoma')
    | Q(condition_concept__concept_name__icontains='leukemia')
    | Q(condition_concept__concept_name__icontains='myeloma')
    | Q(condition_concept__concept_name__icontains='carcinoma')
    | Q(condition_concept__concept_name__icontains='sarcoma')
    | Q(condition_concept__concept_name__icontains='tumor')
)


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


def _cohort_person_ids(org_slugs, person_ids_arg, limit):
    if person_ids_arg:
        return [int(x) for x in person_ids_arg.split(',') if x.strip()]

    qs = (
        PatientRecord.objects
        .filter(organization__slug__in=org_slugs)
        .order_by('person_id')
        .values_list('person_id', flat=True)
    )
    return list(qs[:limit]) if limit else list(qs)


def _latest_measurement_text_subquery(concept_codes):
    qs = (
        Measurement.objects
        .filter(person=OuterRef('pk'))
        .filter(
            Q(measurement_concept__concept_code__in=concept_codes)
            | Q(measurement_source_value__in=concept_codes)
        )
        .annotate(
            raw_text=Coalesce(
                'value_as_string',
                'value_source_value',
                'value_as_concept__concept_name',
            )
        )
        .order_by('-measurement_date', '-measurement_id')
        .values('raw_text')[:1]
    )
    return Subquery(qs, output_field=CharField())


def _latest_measurement_number_subquery(concept_codes, output_field):
    qs = (
        Measurement.objects
        .filter(person=OuterRef('pk'))
        .filter(
            Q(measurement_concept__concept_code__in=concept_codes)
            | Q(measurement_source_value__in=concept_codes)
        )
        .exclude(value_as_number__isnull=True)
        .order_by('-measurement_date', '-measurement_id')
        .values('value_as_number')[:1]
    )
    return Subquery(qs, output_field=output_field)


def _latest_observation_text_subquery(concept_codes):
    qs = (
        Observation.objects
        .filter(person=OuterRef('pk'))
        .filter(
            Q(observation_concept__concept_code__in=concept_codes)
            | Q(observation_source_value__in=concept_codes)
        )
        .annotate(
            raw_text=Coalesce(
                'value_as_string',
                'value_source_value',
                'value_as_concept__concept_name',
            )
        )
        .order_by('-observation_date', '-observation_id')
        .values('raw_text')[:1]
    )
    return Subquery(qs, output_field=CharField())


def _latest_observation_number_subquery(concept_codes, output_field):
    qs = (
        Observation.objects
        .filter(person=OuterRef('pk'))
        .filter(
            Q(observation_concept__concept_code__in=concept_codes)
            | Q(observation_source_value__in=concept_codes)
        )
        .exclude(value_as_number__isnull=True)
        .order_by('-observation_date', '-observation_id')
        .values('value_as_number')[:1]
    )
    return Subquery(qs, output_field=output_field)


def _latest_observation_number_name_subquery(name_terms, output_field):
    name_filter = Q()
    for term in name_terms:
        name_filter |= Q(observation_concept__concept_name__icontains=term)

    qs = (
        Observation.objects
        .filter(person=OuterRef('pk'))
        .filter(name_filter)
        .exclude(value_as_number__isnull=True)
        .order_by('-observation_date', '-observation_id')
        .values('value_as_number')[:1]
    )
    return Subquery(qs, output_field=output_field)


def _latest_cancer_condition_subquery():
    qs = (
        ConditionOccurrence.objects
        .filter(person=OuterRef('pk'))
        .filter(_CANCER_NAME_FILTERS)
        .order_by('-condition_start_date', '-condition_occurrence_id')
        .values('condition_concept__concept_name')[:1]
    )
    return Subquery(qs, output_field=CharField())


def _normalize_gender(raw):
    if not raw:
        return None
    value = str(raw).lower()
    if 'female' in value:
        return 'F'
    if 'male' in value and 'female' not in value:
        return 'M'
    return 'U'


def _normalize_receptor_status(raw):
    if not raw:
        return None
    value = str(raw).lower()
    if 'positive' in value:
        return 'POSITIVE'
    if 'negative' in value:
        return 'NEGATIVE'
    return str(raw).strip().upper()


def _normalize_disease(raw):
    if not raw:
        return None
    value = str(raw).strip()
    if value.lower() == 'myeloma':
        return 'multiple myeloma'
    return value


def _fetch_patient_record_trial_row(person_id):
    return (
        PatientRecord.objects
        .filter(person_id=person_id)
        .values(*TRIAL_ELIGIBILITY_FIELDS)
        .get()
    )


def _fetch_omop_trial_row(person_id):
    today = date.today()
    qs = (
        Person.objects
        .filter(person_id=person_id)
        .annotate(
            patient_age=Value(today.year, output_field=IntegerField()) - F('year_of_birth'),
            gender_raw=Coalesce('gender_concept__concept_name', 'gender_source_value'),
            disease=_latest_cancer_condition_subquery(),
            stage=Coalesce(
                _latest_measurement_text_subquery(['21908-9']),
                _latest_observation_text_subquery(['21908-9']),
            ),
            ecog_performance_status=_latest_observation_number_name_subquery(['ecog'], IntegerField()),
            karnofsky_performance_score=_latest_observation_number_name_subquery(['karnofsky'], IntegerField()),
            hemoglobin_g_dl=_latest_measurement_number_subquery(['718-7'], DecimalField(max_digits=5, decimal_places=1)),
            platelet_count_thousand_per_ul=_latest_measurement_number_subquery(['777-3'], DecimalField(max_digits=6, decimal_places=1)),
            anc_thousand_per_ul=_latest_measurement_number_subquery(['751-8'], DecimalField(max_digits=6, decimal_places=1)),
            wbc_count_thousand_per_ul=_latest_measurement_number_subquery(['6690-2'], DecimalField(max_digits=6, decimal_places=1)),
            serum_creatinine_mg_dl=_latest_measurement_number_subquery(['2160-0', '38483-4'], DecimalField(max_digits=5, decimal_places=2)),
            creatinine_clearance_ml_min=_latest_measurement_number_subquery(['2164-2'], DecimalField(max_digits=6, decimal_places=1)),
            serum_calcium_mg_dl=_latest_measurement_number_subquery(['17861-6', '49765-1'], DecimalField(max_digits=5, decimal_places=1)),
            bilirubin_total_mg_dl=_latest_measurement_number_subquery(['1975-2'], DecimalField(max_digits=5, decimal_places=1)),
            ast_u_l=_latest_measurement_number_subquery(['1920-8'], IntegerField()),
            alt_u_l=_latest_measurement_number_subquery(['1742-6'], IntegerField()),
            albumin_g_dl=_latest_measurement_number_subquery(['1751-7'], DecimalField(max_digits=5, decimal_places=1)),
            her2_raw=_latest_measurement_text_subquery(['48676-1']),
            estrogen_receptor_raw=_latest_measurement_text_subquery(['16112-5']),
            progesterone_receptor_raw=_latest_measurement_text_subquery(['16113-3']),
        )
        .values(
            'patient_age',
            'gender_raw',
            'disease',
            'stage',
            'ecog_performance_status',
            'karnofsky_performance_score',
            'hemoglobin_g_dl',
            'platelet_count_thousand_per_ul',
            'anc_thousand_per_ul',
            'wbc_count_thousand_per_ul',
            'serum_creatinine_mg_dl',
            'creatinine_clearance_ml_min',
            'serum_calcium_mg_dl',
            'bilirubin_total_mg_dl',
            'ast_u_l',
            'alt_u_l',
            'albumin_g_dl',
            'her2_raw',
            'estrogen_receptor_raw',
            'progesterone_receptor_raw',
        )
    )

    row = qs.get()
    return {
        'patient_age': row['patient_age'],
        'gender': _normalize_gender(row['gender_raw']),
        'disease': _normalize_disease(row['disease']),
        'stage': row['stage'],
        'ecog_performance_status': row['ecog_performance_status'],
        'karnofsky_performance_score': row['karnofsky_performance_score'],
        'hemoglobin_g_dl': row['hemoglobin_g_dl'],
        'platelet_count_thousand_per_ul': row['platelet_count_thousand_per_ul'],
        'anc_thousand_per_ul': row['anc_thousand_per_ul'],
        'wbc_count_thousand_per_ul': row['wbc_count_thousand_per_ul'],
        'serum_creatinine_mg_dl': row['serum_creatinine_mg_dl'],
        'creatinine_clearance_ml_min': row['creatinine_clearance_ml_min'],
        'serum_calcium_mg_dl': row['serum_calcium_mg_dl'],
        'bilirubin_total_mg_dl': row['bilirubin_total_mg_dl'],
        'ast_u_l': row['ast_u_l'],
        'alt_u_l': row['alt_u_l'],
        'albumin_g_dl': row['albumin_g_dl'],
        'her2_status': _normalize_receptor_status(row['her2_raw']),
        'estrogen_receptor_status': _normalize_receptor_status(row['estrogen_receptor_raw']),
        'progesterone_receptor_status': _normalize_receptor_status(row['progesterone_receptor_raw']),
    }


def _count_populated_fields(row):
    return sum(1 for value in row.values() if value not in (None, '', [], {}))


class Command(BaseCommand):
    help = (
        'Benchmark a 20-criterion trial eligibility fetch from raw OMOP tables '
        'versus the same fields on PatientRecord.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slugs',
            default='synthea-mm',
            help='Comma-separated organization slugs to scope the cohort to.',
        )
        parser.add_argument(
            '--person-ids',
            default='',
            help='Comma-separated person_ids — overrides --org-slugs cohort selection.',
        )
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument(
            '--repeat',
            type=int,
            default=1,
            help='Repeat each timed pass N times for more samples (default: 1).',
        )
        parser.add_argument('--output', default='', help='Write raw stats JSON to this path.')

    def handle(self, *args, **options):
        org_slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
        person_ids = _cohort_person_ids(org_slugs, options['person_ids'], options['limit'])

        if not person_ids:
            raise CommandError('No matching patients found for the given cohort.')

        repeat = options['repeat']
        self.stdout.write(
            f'Benchmarking {len(person_ids)} patient(s), '
            f'{repeat} repeat pass(es) per path...'
        )

        # Warm-up: touch both paths once per patient so the timed runs do not
        # inherit a one-sided cold-cache penalty.
        for pid in person_ids:
            try:
                _fetch_patient_record_trial_row(pid)
            except PatientRecord.DoesNotExist:
                pass
            try:
                _fetch_omop_trial_row(pid)
            except Person.DoesNotExist:
                pass

        patient_record_times = []
        omop_times = []
        patient_record_populated = []
        omop_populated = []

        for _ in range(repeat):
            for pid in person_ids:
                try:
                    t0 = time.perf_counter()
                    row = _fetch_patient_record_trial_row(pid)
                    patient_record_times.append(time.perf_counter() - t0)
                    patient_record_populated.append(_count_populated_fields(row))
                except PatientRecord.DoesNotExist:
                    continue

        for _ in range(repeat):
            for pid in person_ids:
                try:
                    t0 = time.perf_counter()
                    row = _fetch_omop_trial_row(pid)
                    omop_times.append(time.perf_counter() - t0)
                    omop_populated.append(_count_populated_fields(row))
                except Person.DoesNotExist:
                    continue

        if not patient_record_times and not omop_times:
            raise CommandError(
                f'None of the given person_ids {person_ids} resolved to an actual '
                f'Person/PatientRecord — no timing samples collected.'
            )

        pr_stats = _stats(patient_record_times)
        omop_stats = _stats(omop_times)

        self.stdout.write(self.style.SUCCESS(f'\npatient_record pull: {pr_stats}'))
        self.stdout.write(self.style.SUCCESS(f'OMOP pull:           {omop_stats}'))

        if pr_stats.get('mean_ms') and omop_stats.get('mean_ms'):
            speedup = omop_stats['mean_ms'] / pr_stats['mean_ms']
            self.stdout.write(self.style.SUCCESS(
                f'\npatient_record is ~{speedup:.1f}x faster than the OMOP pull '
                f'(mean {pr_stats["mean_ms"]}ms vs {omop_stats["mean_ms"]}ms)'
            ))

        if patient_record_populated and omop_populated:
            self.stdout.write(
                f'Avg populated eligibility fields: '
                f'patient_record={statistics.mean(patient_record_populated):.1f}/20, '
                f'OMOP={statistics.mean(omop_populated):.1f}/20'
            )

        if options['output']:
            with open(options['output'], 'w') as f:
                json.dump({
                    'patient_record': pr_stats,
                    'omop': omop_stats,
                    'person_ids': person_ids,
                    'repeat': repeat,
                    'org_slugs': org_slugs,
                    'criteria_fields': TRIAL_ELIGIBILITY_FIELDS,
                }, f, indent=2)
            self.stdout.write(self.style.SUCCESS(f'\nResults written to {options["output"]}'))
