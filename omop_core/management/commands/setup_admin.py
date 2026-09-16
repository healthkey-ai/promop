import os

from django.core.management.base import BaseCommand, CommandError
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Creates or updates the admin superuser from environment variables'

    def handle(self, *args, **options):
        email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
        password = os.environ.get('ADMIN_PASSWORD')

        if not password:
            raise CommandError(
                'ADMIN_PASSWORD environment variable is not set. '
                'Set it in the Render environment before deploying.'
            )

        # Email is not an identity key: a local account can share it with OIDC
        # or service identities. Match the same scope as EmailBackend and never
        # assign a local password to an external identity.
        try:
            user = Identity.objects.get(issuer='urn:local', email__iexact=email)
        except Identity.DoesNotExist:
            Identity.objects.create_superuser(email=email, password=password)
            self.stdout.write(self.style.SUCCESS(f'Created superuser "{email}"'))
        except Identity.MultipleObjectsReturned:
            raise CommandError(
                'ADMIN_EMAIL matches multiple local identities. '
                'Resolve the local-account ambiguity before resetting a password; '
                'no accounts were changed.'
            )
        else:
            user.set_password(password)
            user.save(update_fields=['password'])
            self.stdout.write(self.style.SUCCESS(f'Updated password for "{email}"'))
