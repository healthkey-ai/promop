"""
Management command: enrich_synthea_fl_omop_data

Post-import enrichment for synthetic Follicular Lymphoma cohorts (org
SYNTHEA-FL and friends). Intentionally idempotent — safe to re-run.

  - Refreshes PatientRecord for every patient in the target org(s) so all
    OMOP-derived fields (therapy lines, outcomes, FL → DLBCL transformation,
    death_date) are current.
  - Derives observation_period rows (required by OHDSI analytics).
  - Prints a completeness report across the FL fields the PRism FLF
    Section-4 charts depend on (POD24, CR30, pathways, treatment patterns,
    treatment burden, disease-state snapshot).

Usage:
    python manage.py enrich_synthea_fl_omop_data --confirm
    python manage.py enrich_synthea_fl_omop_data --org-slugs synthea-fl --limit 50 --confirm
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh

DEFAULT_ORG_SLUGS = 'synthea-fl'

# (label, PatientRecord field, "non-empty" predicate)
_COMPLETENESS_FIELDS = [
    ('disease',                 'disease',                    bool),
    ('diagnosis_date',          'diagnosis_date',             bool),
    ('FLIPI score',             'flipi_score',                lambda v: v is not None),
    ('1L therapy',              'first_line_therapy',         bool),
    ('1L outcome',              'first_line_outcome',         bool),
    ('transformation flag',     'transformed_to_dlbcl',       lambda v: v is not None),
]


class Command(BaseCommand):
    help = 'Refresh PatientRecord + observation_period for a synthetic FL cohort and report completeness.'

    def add_arguments(self, parser):
        parser.add_argument('--org-slugs', default=DEFAULT_ORG_SLUGS,
                            help=f'Comma-separated org slugs (default: {DEFAULT_ORG_SLUGS}).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Limit to the first N patients (smoke tests).')
        parser.add_argument('--confirm', action='store_true',
                            help='Required to make changes (safety gate).')

    def handle(self, *args, **options):
        if not options['confirm']:
            self.stdout.write(self.style.WARNING('Dry run — pass --confirm to make changes.'))
            return

        slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
        qs = PatientRecord.objects.filter(organization__slug__in=slugs).order_by('id')
        if options['limit']:
            qs = qs[:options['limit']]
        records = list(qs)
        if not records:
            self.stdout.write(self.style.WARNING(f'No patients found for orgs: {slugs}'))
            return

        self.stdout.write(f'Refreshing PatientRecord for {len(records)} patient(s)...')
        with suppress_patient_record_refresh():
            refreshed = [
                refresh_patient_record(rec.person) for rec in records
            ]

        self.stdout.write('Deriving observation_period rows...')
        call_command('populate_observation_period', org_slugs=','.join(slugs))

        # Completeness report
        self.stdout.write('')
        self.stdout.write('FL completeness report:')
        total = len(refreshed)
        for label, field, present in _COMPLETENESS_FIELDS:
            n = sum(1 for r in refreshed if present(getattr(r, field, None)))
            self.stdout.write(f'  {label:.<28s} {n:>5d} / {total} ({n / total:.0%})')
        n_deceased = sum(1 for r in refreshed if r.death_date)
        n_transformed = sum(1 for r in refreshed if r.transformed_to_dlbcl)
        n_multi_line = sum(1 for r in refreshed if (r.therapy_lines_count or 0) >= 2)
        self.stdout.write(f'  {"deceased":.<28s} {n_deceased:>5d} / {total} ({n_deceased / total:.0%})')
        self.stdout.write(f'  {"transformed to DLBCL":.<28s} {n_transformed:>5d} / {total} ({n_transformed / total:.0%})')
        self.stdout.write(f'  {"≥2 lines of therapy":.<28s} {n_multi_line:>5d} / {total} ({n_multi_line / total:.0%})')
        self.stdout.write(self.style.SUCCESS('FL enrichment complete.'))
