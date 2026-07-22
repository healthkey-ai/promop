"""
Management command: populate_observation_period

Derives one OMOP observation_period row per person from the span of their
clinical events (visit, condition, drug, procedure, measurement, observation).
observation_period is required by OHDSI tooling (Achilles, DataQualityDashboard,
cohort/incidence methods) but is not populated during FHIR ingestion.

Usage:
    DATABASE_URL="..." python manage.py populate_observation_period
    DATABASE_URL="..." python manage.py populate_observation_period --org-slugs synthea-bc
    DATABASE_URL="..." python manage.py populate_observation_period --overwrite
"""
from django.core.management.base import BaseCommand
from django.db.models import Max, Min
from django.db.models.functions import Coalesce

from omop_core.models import (
    Concept,
    ConditionOccurrence,
    DrugExposure,
    Measurement,
    Observation,
    ObservationPeriod,
    PatientRecord,
    ProcedureOccurrence,
    VisitOccurrence,
)

# Standard OMOP type concept "EHR" for EHR-derived observation periods; falls
# back to concept 0 ("No matching concept") when the vocabulary is not loaded.
_EHR_TYPE_CONCEPT_ID = 32817

# (model, start_field, end_field_or_None) — end falls back to start when absent.
_EVENT_SOURCES = [
    (VisitOccurrence, 'visit_start_date', 'visit_end_date'),
    (ConditionOccurrence, 'condition_start_date', 'condition_end_date'),
    (DrugExposure, 'drug_exposure_start_date', 'drug_exposure_end_date'),
    (ProcedureOccurrence, 'procedure_date', 'procedure_end_date'),
    (Measurement, 'measurement_date', None),
    (Observation, 'observation_date', None),
]


class Command(BaseCommand):
    help = 'Derive OMOP observation_period rows (one per person) from clinical-event spans.'

    def add_arguments(self, parser):
        parser.add_argument('--org-slugs', default='', help='Comma-separated org slugs to scope to.')
        parser.add_argument('--overwrite', action='store_true',
                            help='Delete existing observation_period rows for the cohort first.')

    def _cohort_person_ids(self, org_slugs):
        if org_slugs:
            slugs = [s.strip() for s in org_slugs.split(',') if s.strip()]
            return set(
                PatientRecord.objects.filter(organization__slug__in=slugs)
                .values_list('person_id', flat=True)
            )
        return None  # all persons

    def handle(self, *args, **options):
        person_ids = self._cohort_person_ids(options['org_slugs'])

        # spans[person_id] = [min_start, max_end]
        spans = {}
        for model, start_f, end_f in _EVENT_SOURCES:
            qs = model.objects.all()
            if person_ids is not None:
                qs = qs.filter(person_id__in=person_ids)
            end_expr = Coalesce(end_f, start_f) if end_f else start_f
            rows = qs.values('person_id').annotate(mn=Min(start_f), mx=Max(end_expr))
            for r in rows:
                pid, mn, mx = r['person_id'], r['mn'], r['mx']
                if mn is None:
                    continue
                cur = spans.get(pid)
                if cur is None:
                    spans[pid] = [mn, mx]
                else:
                    if mn < cur[0]:
                        cur[0] = mn
                    if mx > cur[1]:
                        cur[1] = mx

        if not spans:
            self.stdout.write('No clinical events found for the cohort; nothing to do.')
            return

        type_concept_id = (
            _EHR_TYPE_CONCEPT_ID
            if Concept.objects.filter(concept_id=_EHR_TYPE_CONCEPT_ID).exists()
            else 0
        )

        target_ids = set(spans)
        if options['overwrite']:
            deleted, _ = ObservationPeriod.objects.filter(person_id__in=target_ids).delete()
            self.stdout.write(f'Deleted {deleted} existing observation_period row(s).')
        else:
            # skip persons that already have a period
            existing = set(
                ObservationPeriod.objects.filter(person_id__in=target_ids)
                .values_list('person_id', flat=True)
            )
            target_ids -= existing
            if existing:
                self.stdout.write(f'Skipping {len(existing)} person(s) that already have a period '
                                  f'(use --overwrite to replace).')

        next_id = (ObservationPeriod.objects.aggregate(m=Max('observation_period_id'))['m'] or 0) + 1
        to_create = []
        for pid in sorted(target_ids):
            mn, mx = spans[pid]
            to_create.append(ObservationPeriod(
                observation_period_id=next_id,
                person_id=pid,
                observation_period_start_date=mn,
                observation_period_end_date=mx,
                period_type_concept_id=type_concept_id,
            ))
            next_id += 1

        ObservationPeriod.objects.bulk_create(to_create, batch_size=1000)
        self.stdout.write(self.style.SUCCESS(
            f'Created {len(to_create)} observation_period row(s) '
            f'(period_type_concept_id={type_concept_id}).'
        ))
