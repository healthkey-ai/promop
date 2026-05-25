"""
Register a confidential OAuth2 service client for machine-to-machine API access.

Any external system (hospital EHR, foundation platform, analytics service) that
needs to call the HealthKey API without a user session uses the client_credentials
grant type.  This command creates or updates that client registration.

Usage:
    python manage.py create_service_client --name "Acme Hospital EHR" \\
        --client-id acme-hospital --client-secret <secret>

The client can then obtain a Bearer token with:
    POST /o/token/
    grant_type=client_credentials&client_id=...&client_secret=...

Tokens honour the standard scope model: patient/*.read for reads,
patient/*.write for writes.
"""

import secrets

from django.core.management.base import BaseCommand
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Register a confidential OAuth2 client for service-to-service API access'

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True, help='Human-readable client name')
        parser.add_argument('--client-id', dest='client_id', required=True,
                            help='OAuth2 client_id (must be unique)')
        parser.add_argument('--client-secret', dest='client_secret', default=None,
                            help='OAuth2 client_secret (auto-generated if omitted)')
        parser.add_argument('--owner-username', dest='owner_username', default=None,
                            help='Django username to own the app (defaults to first superuser)')
        parser.add_argument('--org', dest='org_slug', default=None,
                            help='Organization slug to link this client to (for multi-tenant scoping)')

    def handle(self, *args, **options):
        from oauth2_provider.models import Application
        from omop_core.models import Organization, ApplicationOrganization

        client_secret = options['client_secret'] or secrets.token_urlsafe(40)

        owner = None
        if options['owner_username']:
            try:
                owner = Identity.objects.get(email=options['owner_username'])
            except Identity.DoesNotExist:
                self.stderr.write(self.style.ERROR(
                    f"User '{options['owner_username']}' not found."
                ))
                return
        else:
            owner = Identity.objects.filter(is_superuser=True).first()
            if not owner:
                self.stderr.write(self.style.WARNING(
                    'No superuser found. Create one first with: manage.py createsuperuser'
                ))
                return

        org = None
        if options['org_slug']:
            try:
                org = Organization.objects.get(slug=options['org_slug'])
            except Organization.DoesNotExist:
                self.stderr.write(self.style.ERROR(
                    f"Organization with slug '{options['org_slug']}' not found. "
                    "Create it in the Django admin first."
                ))
                return

        app, created = Application.objects.update_or_create(
            client_id=options['client_id'],
            defaults={
                'name': options['name'],
                'user': owner,
                'client_type': Application.CLIENT_CONFIDENTIAL,
                'authorization_grant_type': Application.GRANT_CLIENT_CREDENTIALS,
                'client_secret': client_secret,
            },
        )

        if org is not None:
            ApplicationOrganization.objects.update_or_create(
                application=app,
                defaults={'organization': org},
            )

        verb = 'Created' if created else 'Updated'
        org_line = f"  Organization: {org.name} ({org.slug})\n" if org else ""
        self.stdout.write(self.style.SUCCESS(
            f"{verb} service client:\n"
            f"  Name:        {app.name}\n"
            f"  client_id:   {app.client_id}\n"
            f"  client_secret: {client_secret}\n"
            f"  Grant type:  client_credentials\n"
            f"{org_line}"
            f"\nToken endpoint: POST /o/token/\n"
            f"  grant_type=client_credentials\n"
            f"  client_id={app.client_id}\n"
            f"  client_secret=<secret>\n"
        ))
        if not options['client_secret']:
            self.stdout.write(self.style.WARNING(
                'Auto-generated secret shown above — store it securely, it cannot be recovered.'
            ))
