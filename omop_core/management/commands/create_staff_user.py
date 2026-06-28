"""
Creates a staff user (is_staff=True, not superuser).

Email and password are read from arguments or environment variables.
Default email: adam@healthkey.ai (override with --email or STAFF_EMAIL env var).
Password: required via --password or STAFF_PASSWORD env var.

Usage:
  python manage.py create_staff_user --password <password>
  python manage.py create_staff_user --email other@example.com --password <password>
  STAFF_PASSWORD=<password> python manage.py create_staff_user
"""
import os

from django.core.management.base import BaseCommand, CommandError
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Creates or updates a staff user (is_staff=True). Email/password via args or env vars.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            default=os.environ.get('STAFF_EMAIL', 'adam@healthkey.ai'),
            help='Email address (default: STAFF_EMAIL env var or adam@healthkey.ai)',
        )
        parser.add_argument(
            '--password',
            default=os.environ.get('STAFF_PASSWORD'),
            help='Password (required: --password arg or STAFF_PASSWORD env var)',
        )

    def handle(self, *args, **options):
        email = options['email']
        password = options['password']

        if not password:
            raise CommandError(
                'Password is required. Use --password or set the STAFF_PASSWORD environment variable.'
            )

        try:
            identity = Identity.objects.get(email=email, issuer='urn:local')
            identity.is_staff = True
            identity.set_password(password)
            identity.save()
            self.stdout.write(self.style.SUCCESS(f'Updated staff user "{email}"'))
        except Identity.DoesNotExist:
            identity = Identity.objects.create_user(
                email=email, password=password, is_staff=True,
            )
            self.stdout.write(self.style.SUCCESS(f'Created staff user "{email}"'))
