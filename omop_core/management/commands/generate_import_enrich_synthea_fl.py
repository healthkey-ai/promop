"""
Management command: generate_import_enrich_synthea_fl

Full staging pipeline for rich synthetic Follicular Lymphoma cohorts:

  1. Generate a FHIR bundle with clinically complete FL patients using the
     internal FL generator (generate_fhir_bundle --disease fl):

       - Ann Arbor stage, tumor grade (1–3b), FLIPI score/risk, GELF criteria
       - B symptoms, bulky disease, nodal sites, bone-marrow involvement
       - 20+ standard labs (CBC, CMP, LDH, Beta-2-microglobulin)
       - Performance: ECOG, Karnofsky, vital signs
       - Watch-and-wait patients (low tumor burden)
       - Multi-year therapy timelines with realistic PFS intervals:
         ~20% of treated patients progress within 24 months of 1L start
         (POD24); later lines have shorter remissions; recently-diagnosed
         patients naturally have fewer lines
       - Multi-line FL regimens (BR, R-CHOP, R-CVP, R², tazemetostat,
         bispecifics, CAR-T, Pola-BR, …) with per-line outcomes
       - Maintenance rituximab
       - FL → DLBCL histologic transformation (DLBCL Condition)
       - Deaths (deceasedDateTime), weighted toward POD24 / heavily
         pre-treated / transformed patients — supports OS analyses

     The dataset is shaped to feed the PRism FLF Section-4 charts:
     POD24 split, CR-by-landmark (CR30), OS by 1L→2L pathway, treatment
     patterns/categories by line, treatment burden, and the disease-state
     snapshot.

  2. Import that bundle into OMOP under a target organisation via
     import_fhir_bundle.

  3. Run the FL enrichment pass (enrich_synthea_fl_omop_data):
     refreshes PatientRecord for every patient, derives observation_period
     rows, and prints a completeness report for the chart-critical fields.

Usage:
    python manage.py generate_import_enrich_synthea_fl
    python manage.py generate_import_enrich_synthea_fl --count 1000 --org-slug synthea-fl
    python manage.py generate_import_enrich_synthea_fl --count 100 --wipe-existing --seed 42
"""

import tempfile
import uuid
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand

from omop_core.models import Organization
from omop_core.services.organization_cleanup import delete_organization_with_patient_cascade


class Command(BaseCommand):
    help = (
        'Generate a rich synthetic FL FHIR bundle, import it into OMOP, '
        'and run the FL OMOP enrichment pass for a target organisation.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=100,
            help='Number of patients to generate (default: 100).',
        )
        parser.add_argument(
            '--output',
            default=None,
            help=(
                'Output path for the generated FHIR bundle. Defaults to a unique file '
                'under the system temp dir per run, so concurrent/retried invocations '
                "don't clobber each other's bundle before the enrichment step reads it."
            ),
        )
        parser.add_argument(
            '--org-slug',
            default='synthea-fl',
            help='Organisation slug to create/import into (default: synthea-fl).',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=None,
            help='Random seed for reproducible patient generation.',
        )
        parser.add_argument(
            '--watch-wait-ratio',
            type=float,
            default=None,
            help='Fraction of eligible (low tumor burden) patients managed with '
                 'watch-and-wait (default: generator default 0.20).',
        )
        parser.add_argument(
            '--import-batch-size',
            type=int,
            default=1,
            help='Patients per FHIR import batch (default: 1).',
        )
        parser.add_argument(
            '--enrich-limit',
            type=int,
            default=None,
            help='Limit the enrichment pass to the first N patients (useful for smoke tests).',
        )
        parser.add_argument(
            '--wipe-existing',
            action='store_true',
            help=(
                'Delete the target organisation and all of its existing patients '
                'before generating and importing the new cohort.'
            ),
        )

    def handle(self, *args, **options):
        org_slug = options['org_slug']

        if options['output'] is None:
            options['output'] = str(
                Path(tempfile.gettempdir()) / f'synthea_fl_{uuid.uuid4().hex[:12]}.json'
            )
        self.stdout.write(f"Using bundle file: {options['output']}")

        if options['wipe_existing']:
            existing_org = Organization.objects.filter(slug__iexact=org_slug).first()
            if existing_org:
                self.stdout.write(
                    f'Wiping existing org {existing_org.slug!r} before regeneration...'
                )
                delete_organization_with_patient_cascade(existing_org)

        # ------------------------------------------------------------------
        # Step 1: Generate FHIR bundle
        # ------------------------------------------------------------------
        self.stdout.write('Step 1/3: generating Synthea FL FHIR bundle...')
        generate_kwargs = {
            'disease': 'fl',
            'count': options['count'],
            'output': options['output'],
        }
        if options['seed'] is not None:
            generate_kwargs['seed'] = options['seed']
        if options['watch_wait_ratio'] is not None:
            generate_kwargs['watch_wait_ratio'] = options['watch_wait_ratio']
        call_command('generate_fhir_bundle', **generate_kwargs)

        # ------------------------------------------------------------------
        # Step 2: Import bundle into OMOP
        # ------------------------------------------------------------------
        self.stdout.write('Step 2/3: importing bundle into OMOP...')
        call_command(
            'import_fhir_bundle',
            file=options['output'],
            org_slug=org_slug,
            batch_size=options['import_batch_size'],
        )

        # ------------------------------------------------------------------
        # Step 3: Enrich OMOP data and validate PatientRecord completeness
        # ------------------------------------------------------------------
        self.stdout.write('Step 3/3: enriching OMOP data and refreshing PatientRecord...')
        enrich_kwargs = {
            'org_slugs': org_slug,
            'confirm': True,
        }
        if options['enrich_limit'] is not None:
            enrich_kwargs['limit'] = options['enrich_limit']
        call_command('enrich_synthea_fl_omop_data', **enrich_kwargs)

        self.stdout.write(self.style.SUCCESS(
            f'Completed FL generation, import, and enrichment for org {org_slug!r}.'
        ))
