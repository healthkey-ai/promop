"""
Management command: generate_import_enrich_synthea_bc

Wrapper that runs the full staging pipeline for rich synthetic breast-cancer
cohorts:

  1. Generate a Synthea FHIR bundle with breast-cancer patients.
  2. Import that bundle into OMOP under a target organization.
  3. Run the breast-cancer OMOP enrichment pass so PatientRecord can derive
     fields from the imported OMOP rows.

This keeps raw generation, import, and enrichment as separate steps for
auditability while still exposing a single repeatable entrypoint for staging.
"""

from django.core.management.base import BaseCommand
from django.core.management import call_command

from omop_core.models import Organization
from omop_core.services.organization_cleanup import delete_organization_with_patient_cascade


class Command(BaseCommand):
    help = (
        'Generate a rich synthetic breast-cancer FHIR bundle, import it into '
        'OMOP, and run the OMOP enrichment pass for a target organization.'
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
            default='/tmp/synthea_bc_100_codex.json',
            help='Output path for the generated FHIR bundle.',
        )
        parser.add_argument(
            '--org-slug',
            default='synthea-bc',
            help='Organization slug to create/import into (default: synthea-bc).',
        )
        parser.add_argument(
            '--state',
            default=None,
            help='US state for Synthea demographics (passed through to the generator).',
        )
        parser.add_argument(
            '--city',
            default=None,
            help='City within the state (passed through to the generator).',
        )
        parser.add_argument(
            '--age-range',
            default=None,
            help='Age range for generated patients as "min-max".',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=None,
            help='Random seed for Synthea.',
        )
        parser.add_argument(
            '--java-path',
            default=None,
            help='Path to the java binary.',
        )
        parser.add_argument(
            '--jar-path',
            default=None,
            help='Path to synthea-with-dependencies.jar.',
        )
        parser.add_argument(
            '--deceased-fraction',
            type=float,
            default=None,
            help='Fraction of the cohort that should be deceased patients.',
        )
        parser.add_argument(
            '--keep-modules-dir',
            default=None,
            help='Directory to persist the generated Synthea custom modules.',
        )
        parser.add_argument(
            '--import-batch-size',
            type=int,
            default=1,
            help='Patients per import batch (default: 1).',
        )
        parser.add_argument(
            '--enrich-limit',
            type=int,
            default=None,
            help='Optional limit for the enrichment pass.',
        )
        parser.add_argument(
            '--wipe-existing',
            action='store_true',
            help=(
                'Delete the target organization and all of its existing patients '
                'before generating/importing the new cohort.'
            ),
        )

    def handle(self, *args, **options):
        org_slug = options['org_slug']
        if options['wipe_existing']:
            existing_org = Organization.objects.filter(slug__iexact=org_slug).first()
            if existing_org:
                self.stdout.write(
                    f'Wiping existing org {existing_org.slug!r} before regeneration...'
                )
                delete_organization_with_patient_cascade(existing_org)

        generate_kwargs = {
            'count': options['count'],
            'output': options['output'],
        }
        for key in ('state', 'city', 'age_range', 'seed', 'java_path', 'jar_path', 'keep_modules_dir'):
            value = options.get(key)
            if value is not None:
                generate_kwargs[key] = value
        if options.get('deceased_fraction') is not None:
            generate_kwargs['deceased_fraction'] = options['deceased_fraction']

        self.stdout.write('Step 1/3: generating Synthea bundle...')
        call_command('generate_synthea_bc', **generate_kwargs)

        self.stdout.write('Step 2/3: importing bundle into OMOP...')
        call_command(
            'import_fhir_bundle',
            file=options['output'],
            org_slug=org_slug,
            batch_size=options['import_batch_size'],
        )

        self.stdout.write('Step 3/3: enriching OMOP data and refreshing PatientRecord...')
        enrich_kwargs = {
            'org_slugs': org_slug,
            'confirm': True,
        }
        if options.get('enrich_limit') is not None:
            enrich_kwargs['limit'] = options['enrich_limit']
        call_command('enrich_breast_cancer_omop_data', **enrich_kwargs)

        self.stdout.write(
            self.style.SUCCESS(
                f'Completed generation, import, and enrichment for org {org_slug!r}.'
            )
        )
