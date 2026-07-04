"""
Creates a staff user (is_staff=True, not superuser).

Usage:
  python manage.py create_staff_user <email> <password>
"""
from django.core.management.base import BaseCommand, CommandError
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Creates or updates a staff user (is_staff=True).'

    def add_arguments(self, parser):
        parser.add_argument('email', help='Email address')
        parser.add_argument('password', help='Password')

    def handle(self, *args, **options):
        email = options['email']
        password = options['password']

        if not password:
            raise CommandError('Password is required.')

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
