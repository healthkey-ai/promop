"""
Management command to bootstrap a SMART on FHIR OAuth2 Application.

Usage:
    python manage.py create_smart_app                    # local dev defaults
        (the default redirect URI is http://localhost:3000/auth/callback, so this
        needs ALLOWED_REDIRECT_URI_SCHEMES=https,http — set by .env.example and
        docker-compose.dev.yml; a bare clone must set it)
    python manage.py create_smart_app --name "My App" \\
        --redirect-uris "https://myapp.example.com/callback" \\
        --client-id my-client-id

The command is idempotent: running it again updates the existing record.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Bootstrap a SMART on FHIR OAuth2 Application for the React SPA'

    def add_arguments(self, parser):
        parser.add_argument(
            '--name',
            default='PRomop SMART App',
            help='Application name (default: "PRomop SMART App")',
        )
        parser.add_argument(
            '--client-id',
            default='promop-smart-app',
            dest='client_id',
            help='OAuth2 client_id (default: promop-smart-app)',
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
                raise CommandError(
                    f"User '{options['owner_username']}' not found."
                ) from None
        else:
            owner = Identity.objects.filter(is_staff=True).first()
            if not owner:
                raise CommandError(
                    'No staff user found. Create one first with: manage.py '
                    'createsuperuser (or set is_staff=True on an existing user)'
                )

        # Model.save() never calls full_clean(), so update_or_create() below would
        # store any redirect URI it is given — including a plaintext http:// one
        # under the https-only default. Ask the validator first, so the supported
        # provisioning path enforces what docs/security-settings.md promises.
        defaults = {
            'name': name,
            'user': owner,
            'client_type': Application.CLIENT_PUBLIC,
            'authorization_grant_type': Application.GRANT_AUTHORIZATION_CODE,
            'redirect_uris': redirect_uris,
            # Public clients do not have a secret; PKCE is required instead
            'client_secret': '',
            'skip_authorization': False,
        }
        candidate = Application(**defaults)
        # The toolkit's own default for an absent key; see patient_portal/checks.py.
        allowed = settings.OAUTH2_PROVIDER.get(
            'ALLOWED_REDIRECT_URI_SCHEMES', ['http', 'https'])
        if not allowed:
            # clean() would raise AttributeError from deep inside the toolkit here,
            # because it treats the setting as mandatory. Say so instead.
            raise CommandError(
                'ALLOWED_REDIRECT_URI_SCHEMES is empty, so no redirect URI can be '
                'accepted. Set https, or https,http for local HTTP callbacks.'
            )
        try:
            candidate.clean()
        except ValidationError as exc:
            raise CommandError(
                f"Refusing to provision '{name}': {'; '.join(exc.messages)}\n"
                f"Allowed redirect URI schemes: {', '.join(allowed)}.\n"
                'For local HTTP callbacks set ALLOWED_REDIRECT_URI_SCHEMES=https,http; '
                'keep that override out of staging and production.'
            ) from exc

        # Same dict the candidate was validated from, so the two cannot drift.
        app, created = Application.objects.update_or_create(
            client_id=client_id, defaults=defaults,
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
