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

        if Identity.objects.filter(email=email).exists():
            user = Identity.objects.get(email=email)
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Updated password for "{email}"'))
        else:
            Identity.objects.create_superuser(email=email, password=password)
            self.stdout.write(self.style.SUCCESS(f'Created superuser "{email}"'))
