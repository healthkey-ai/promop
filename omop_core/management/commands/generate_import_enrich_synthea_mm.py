"""
Management command: generate_import_enrich_synthea_mm

Full staging pipeline for rich synthetic Multiple Myeloma cohorts:

  1. Generate a FHIR bundle with clinically complete MM patients using the
     internal MM generator (called via generate_fhir_bundle --disease mm).

     The bundle includes:
       - ISS / R-ISS staging, cytogenetics (del17p, t(4;14), t(14;16), 1q, hyperdiploidy, del13q)
       - Disease burden: serum/urine M-spike, kappa/lambda FLC, bone marrow plasma cells %
       - CRAB criteria: haemoglobin, calcium, creatinine/CrCl/eGFR, bone lesions
       - SLiM criteria: plasma cells ≥60%, FLC ratio
       - 20+ standard labs: CBC, CMP, LFTs, LDH, Beta-2-microglobulin, ejection fraction
       - Performance: ECOG, Karnofsky, peripheral neuropathy grade, vital signs
       - Multi-line therapy: 1–5 lines of MM-specific regimens (VRd, DRd, Pd, KRd, etc.)
         with outcomes, therapy-line extensions, and individual drug RxNorm codes
       - SCT history / eligibility / date (autologous, allogeneic, tandem)
       - Refractory status (PI, IMiD, anti-CD38, triple-class)
       - Cytogenetic extensions and plasma cell leukemia flag
       - Behavior/lifestyle: smoking, alcohol, exercise, diet, sleep, stress, social support
       - Socioeconomic: employment, education, marital status, insurance, income, dependents
       - Wearable: 28 days of daily steps, resting HR, HRV, SpO2, respiratory rate,
         active minutes, sleep duration — correlated to ECOG
       - Infection status: HIV, HBV, HCV documented screening results

  2. Import that bundle into OMOP under a target organisation via import_fhir_bundle.

  3. Run the MM OMOP enrichment pass (enrich_synthea_mm_omop_data) which:
       - Backfills ConditionOccurrence and ProcedureOccurrence for any gaps
       - Calls refresh_patient_record for every patient so all OMOP-derived
         PatientRecord fields are populated
       - Prints a completeness check across 14 critical MM fields

Usage:
    python manage.py generate_import_enrich_synthea_mm
    python manage.py generate_import_enrich_synthea_mm --count 50 --org-slug synthea-mm
    python manage.py generate_import_enrich_synthea_mm --count 100 --wipe-existing --seed 42
    python manage.py generate_import_enrich_synthea_mm --rrmm-ratio 0.90 --import-batch-size 5
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
        'Generate a rich synthetic MM FHIR bundle, import it into OMOP, '
        'and run the MM OMOP enrichment pass for a target organisation.'
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
            default='synthea-mm',
            help='Organisation slug to create/import into (default: synthea-mm).',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=None,
            help='Random seed for reproducible patient generation.',
        )
        parser.add_argument(
            '--rrmm-ratio',
            type=float,
            default=0.80,
            help=(
                'Fraction of patients with ≥1 prior line of therapy (default: 0.80). '
                'All patients always receive at least one therapy line; this ratio '
                'controls how many have relapsed/refractory disease.'
            ),
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
            '--min-procedures',
            type=int,
            default=2,
            help='Minimum ProcedureOccurrence rows per patient (default: 2).',
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
                Path(tempfile.gettempdir()) / f'synthea_mm_{uuid.uuid4().hex[:12]}.json'
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
        self.stdout.write('Step 1/3: generating Synthea MM FHIR bundle...')
        generate_kwargs = {
            'disease': 'mm',
            'count': options['count'],
            'output': options['output'],
            'rrmm_ratio': options['rrmm_ratio'],
        }
        if options['seed'] is not None:
            generate_kwargs['seed'] = options['seed']
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
            'bundle': options['output'],
            'org_slugs': org_slug,
            'confirm': True,
            'min_procedures': options['min_procedures'],
        }
        if options['enrich_limit'] is not None:
            enrich_kwargs['limit'] = options['enrich_limit']
        call_command('enrich_synthea_mm_omop_data', **enrich_kwargs)

        self.stdout.write(self.style.SUCCESS(
            f'Completed MM generation, import, and enrichment for org {org_slug!r}.'
        ))
