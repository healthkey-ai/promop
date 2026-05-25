"""
Management command to bootstrap a SMART on FHIR OAuth2 Application.

Usage:
    python manage.py create_smart_app                    # local dev defaults
    python manage.py create_smart_app --name "My App" \\
        --redirect-uris "https://myapp.example.com/callback" \\
        --client-id my-client-id

The command is idempotent: running it again updates the existing record.
"""

from django.core.management.base import BaseCommand
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Bootstrap a SMART on FHIR OAuth2 Application for the React SPA'

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            default='CTOMOP SMART App',
            help='Application name (default: "CTOMOP SMART App")',
        )
        parser.add_argument(
            '--client-id',
            default='ctomop-smart-app',
            dest='client_id',
            help='OAuth2 client_id (default: ctomop-smart-app)',
        )
        parser.add_argument(
            '--redirect-uris',
            default='http://localhost:3000/auth/callback',
            dest='redirect_uris',
            help='Space-separated list of allowed redirect URIs',
        )
        parser.add_argument(
            '--owner-username',
            default=None,
            dest='owner_username',
            help='Django username to own the app (defaults to first superuser)',
        )

    def handle(self, *args, **options):
        # Import here so the command can be imported before migrations run
        from oauth2_provider.models import Application

        name = options['name']
        client_id = options['client_id']
        redirect_uris = options['redirect_uris']

        # Resolve owner
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

        app, created = Application.objects.update_or_create(
            client_id=client_id,
            defaults={
                'name': name,
                'user': owner,
                'client_type': Application.CLIENT_PUBLIC,
                'authorization_grant_type': Application.GRANT_AUTHORIZATION_CODE,
                'redirect_uris': redirect_uris,
                # Public clients do not have a secret; PKCE is required instead
                'client_secret': '',
                'skip_authorization': False,
            },
        )

        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} SMART on FHIR application:\n"
            f"  Name:          {app.name}\n"
            f"  client_id:     {app.client_id}\n"
            f"  Redirect URIs: {app.redirect_uris}\n"
            f"  Client type:   {app.client_type} (PKCE required)\n"
            f"  Owner:         {owner.username}\n\n"
            f"Authorization URL: /o/authorize/\n"
            f"Token URL:         /o/token/\n"
        ))
