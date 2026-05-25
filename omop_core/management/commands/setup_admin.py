from django.core.management.base import BaseCommand
from patient_portal.models import Identity

class Command(BaseCommand):
    help = 'Creates admin user with default credentials'

    def handle(self, *args, **options):
        email = 'admin@example.com'
        password = '1database'

        if Identity.objects.filter(email=email).exists():
            self.stdout.write(self.style.WARNING(f'User "{email}" already exists'))
            # Update password in case it changed
            user = Identity.objects.get(email=email)
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Updated password for user "{email}"'))
        else:
            Identity.objects.create_superuser(
                email=email,
                password=password
            )
            self.stdout.write(self.style.SUCCESS(f'Successfully created superuser "{email}"'))
