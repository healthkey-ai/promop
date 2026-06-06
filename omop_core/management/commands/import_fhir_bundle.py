"""
Import a FHIR R4 Bundle JSON file directly into the database via the existing
upload_fhir view logic, bypassing HTTP and Render's 30-second request timeout.

Usage:
    DATABASE_URL=... python manage.py import_fhir_bundle data/mm_patients_fhir.json
    DATABASE_URL=... python manage.py import_fhir_bundle data/mm_patients_fhir.json --batch-size 5
"""

import json
import socket
import sys
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections
from django.test import RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.request import Request as DRFRequest
from rest_framework.parsers import MultiPartParser, JSONParser

# Global socket timeout — prevents any DB operation from hanging forever.
# This covers both connect and query hangs on the remote Render PostgreSQL.
_SOCKET_TIMEOUT = 45  # seconds


def _patch_db_timeouts():
    """Add statement_timeout and keepalive options to prevent hanging on Render."""
    socket.setdefaulttimeout(_SOCKET_TIMEOUT)
    db = connections.databases['default']
    opts = dict(db.get('OPTIONS', {}))
    existing_options = opts.get('options', '')
    if 'statement_timeout' not in existing_options:
        opts['options'] = (existing_options + ' -c statement_timeout=40000').strip()
    # TCP keepalive — detects dead connections faster
    opts.setdefault('keepalives', 1)
    opts.setdefault('keepalives_idle', 20)
    opts.setdefault('keepalives_interval', 5)
    opts.setdefault('keepalives_count', 3)
    db['OPTIONS'] = opts


class Command(BaseCommand):
    help = 'Import a FHIR R4 Bundle JSON file directly (no HTTP timeout)'

    def add_arguments(self, parser):
        parser.add_argument('file', help='Path to FHIR Bundle JSON file')
        parser.add_argument('--batch-size', type=int, default=1, dest='batch_size',
                            help='Patients per batch (default: 1)')
        parser.add_argument('--start-from', type=int, default=0, dest='start_from',
                            help='Skip first N patients (for resuming after failure)')
        parser.add_argument('--username', default='admin',
                            help='Django user to authenticate as (default: admin)')

    def _print(self, msg, err=False):
        stream = self.stderr if err else self.stdout
        stream.write(msg)
        sys.stdout.flush()
        sys.stderr.flush()

    def handle(self, *args, **options):
        from patient_portal.api.views import PatientInfoViewSet

        _patch_db_timeouts()

        bundle_path = Path(options['file'])
        if not bundle_path.exists():
            raise CommandError(f'File not found: {bundle_path}')

        self._print(f'Loading {bundle_path}…')
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

        start_from = options['start_from']
        if start_from:
            groups = groups[start_from:]
            self._print(f'Skipping first {start_from} patients, resuming from patient {start_from + 1}')

        self._print(f'{len(groups)} patients to import, batch size {options["batch_size"]}')

        # Get a user to authenticate requests — do this before the loop
        close_old_connections()
        User = get_user_model()
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(
                f"User '{options['username']}' not found. "
                "Run: manage.py createsuperuser"
            )

        factory = RequestFactory()

        batch_size = options['batch_size']
        total_created = total_updated = total_errors = 0

        batches = [groups[i:i+batch_size] for i in range(0, len(groups), batch_size)]
        for batch_num, batch_groups in enumerate(batches, 1):
            # Fresh connection per batch — prevents "connection is closed" on Render
            close_old_connections()

            entries = [e for g in batch_groups for e in g]
            mini_bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': entries}
            content = json.dumps(mini_bundle).encode('utf-8')

            uploaded = SimpleUploadedFile('bundle.json', content, content_type='application/json')
            django_request = factory.post('/api/patient-info/upload_fhir/', {'file': uploaded})
            django_request.user = user
            django_request._dont_enforce_csrf_checks = True

            request = DRFRequest(django_request, parsers=[MultiPartParser(), JSONParser()])
            request.user = user

            viewset = PatientInfoViewSet()
            viewset.request = request
            viewset.format_kwarg = None
            viewset.kwargs = {}

            try:
                response = viewset.upload_fhir(request)
                data = response.data if hasattr(response, 'data') else {}
                created = data.get('created_count', 0) or 0
                updated = data.get('updated_count', 0) or 0
                errors = data.get('errors', [])
            except Exception as exc:
                created = updated = 0
                errors = [str(exc)]
                # Close broken connection so next batch starts fresh
                close_old_connections()

            total_created += created
            total_updated += updated
            total_errors += len(errors)

            self._print(
                f'  Batch {batch_num}/{len(batches)}: '
                f'{len(batch_groups)} patients — '
                f'created={created} updated={updated} errors={len(errors)}'
            )
            if errors:
                for e in errors[:3]:
                    self._print(f'    {e}', err=True)

        self._print(self.style.SUCCESS(
            f'Done. Total: created={total_created} updated={total_updated} errors={total_errors}'
        ))
