"""
Import a FHIR R4 Bundle JSON file directly into the database via the existing
upload_fhir view logic, bypassing HTTP and Render's 30-second request timeout.

Usage:
    DATABASE_URL=... python manage.py import_fhir_bundle data/mm_patients_fhir.json
    DATABASE_URL=... python manage.py import_fhir_bundle data/mm_patients_fhir.json --batch-size 20
"""

import json
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.request import Request as DRFRequest
from rest_framework.parsers import MultiPartParser, JSONParser


class Command(BaseCommand):
    help = 'Import a FHIR R4 Bundle JSON file directly (no HTTP timeout)'

    def add_arguments(self, parser):
        parser.add_argument('file', help='Path to FHIR Bundle JSON file')
        parser.add_argument('--batch-size', type=int, default=20, dest='batch_size',
                            help='Patients per batch (default: 20)')
        parser.add_argument('--username', default='admin',
                            help='Django user to authenticate as (default: admin)')

    def handle(self, *args, **options):
        from patient_portal.api.views import PatientInfoViewSet

        bundle_path = Path(options['file'])
        if not bundle_path.exists():
            raise CommandError(f'File not found: {bundle_path}')

        self.stdout.write(f'Loading {bundle_path}…')
        with open(bundle_path) as f:
            bundle = json.load(f)

        if bundle.get('resourceType') != 'Bundle':
            raise CommandError('File must be a FHIR Bundle')

        # Group entries by patient
        groups: list[list[dict]] = []
        current: list[dict] = []
        for entry in bundle.get('entry', []):
            rt = entry.get('resource', {}).get('resourceType')
            if rt == 'Patient' and current:
                groups.append(current)
                current = []
            current.append(entry)
        if current:
            groups.append(current)

        self.stdout.write(f'{len(groups)} patients, batch size {options["batch_size"]}')

        # Get a user to authenticate requests
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(
                f"User '{options['username']}' not found. "
                "Run: manage.py createsuperuser"
            )

        factory = RequestFactory()
        viewset = PatientInfoViewSet()

        batch_size = options['batch_size']
        total_created = total_updated = total_errors = 0

        batches = [groups[i:i+batch_size] for i in range(0, len(groups), batch_size)]
        for batch_num, batch_groups in enumerate(batches, 1):
            entries = [e for g in batch_groups for e in g]
            mini_bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': entries}
            content = json.dumps(mini_bundle).encode('utf-8')

            uploaded = SimpleUploadedFile('bundle.json', content, content_type='application/json')
            django_request = factory.post('/api/patient-info/upload_fhir/', {'file': uploaded})
            django_request.user = user
            django_request._dont_enforce_csrf_checks = True

            # Wrap as DRF Request so request.data and request.FILES both work
            request = DRFRequest(django_request, parsers=[MultiPartParser(), JSONParser()])
            request.user = user

            viewset.request = request
            viewset.format_kwarg = None
            viewset.kwargs = {}

            # Call the view action directly
            response = viewset.upload_fhir(request)
            data = response.data if hasattr(response, 'data') else {}

            created = data.get('created_count', 0) or 0
            updated = data.get('updated_count', 0) or 0
            errors = data.get('errors', [])
            total_created += created
            total_updated += updated
            total_errors += len(errors)

            self.stdout.write(
                f'  Batch {batch_num}/{len(batches)}: '
                f'{len(batch_groups)} patients — '
                f'created={created} updated={updated} errors={len(errors)}'
            )
            if errors:
                for e in errors[:3]:
                    self.stderr.write(f'    {e}')

        self.stdout.write(self.style.SUCCESS(
            f'Done. Total: created={total_created} updated={total_updated} errors={total_errors}'
        ))
