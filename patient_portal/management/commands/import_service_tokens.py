"""Import existing private token configuration without rotating distributed keys."""
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from patient_portal.models import ServiceAccessToken, ServiceApplication
from patient_portal.service_applications import ALLOWED_SCOPES, token_digest
from patient_portal.service_tokens import parse_service_tokens


class Command(BaseCommand):
    help = 'Import SERVICE_AUTH_TOKENS JSON into named apps; secrets are stored only as hashes.'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='Private JSON file from generate_service_tokens.')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            config = parse_service_tokens(Path(options['file']).read_text())
        except (OSError, ImproperlyConfigured):
            raise CommandError('Cannot read a valid service-token configuration file.') from None
        created_count = 0
        for service_id, grant in config.items():
            secret = grant['token']
            scopes = grant.get('scopes', 'patient/*.read')
            if len(secret) < 32 or set(scopes.split()) - ALLOWED_SCOPES:
                raise CommandError('Import requires strong tokens (at least 32 characters) and supported scopes.')
            app, _ = ServiceApplication.objects.get_or_create(
                service_id=service_id, defaults={'name': service_id, 'scopes': scopes},
            )
            app = ServiceApplication.objects.select_for_update().get(pk=app.pk)
            if not app.tokens.exists():
                app.scopes = scopes
                app.save(update_fields=['scopes', 'updated_at'])
            record, created = ServiceAccessToken.objects.get_or_create(
                digest=token_digest(secret), defaults={
                    'application': app, 'label': 'Initial distributed token', 'suffix': secret[-4:],
                },
            )
            if record.application_id != app.pk:
                raise CommandError('A token already belongs to another application.')
            created_count += int(created)
            # Existing records (including revocations), app metadata, scopes, and
            # disabled flags survive re-import. No secret is printed.
        self.stdout.write(f'Imported {created_count} new token(s) for {len(config)} application(s).')
