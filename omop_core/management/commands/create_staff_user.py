from django.core.management.base import BaseCommand
from patient_portal.models import Identity


class Command(BaseCommand):
    help = 'Creates or updates adam@healthkey.ai as a staff user (is_staff=True, not superuser)'

    def handle(self, *args, **options):
        email = 'adam@healthkey.ai'
        password = '1database'

        identity, created = Identity.objects.get_or_create(
            email=email,
            defaults={
                'issuer': 'urn:local',
                'is_staff': True,
                'is_superuser': False,
            },
        )

        if not created:
            identity.is_staff = True
            identity.issuer = 'urn:local'

        identity.set_password(password)
        identity.save()

        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} staff user "{email}"'))
