"""
Sets up demo access for casual evaluators.

1. Creates (or updates) demo user: random@healthkey.ai / password123!
2. Creates a domain trust: OrgTrust(granting_org=ABC Foundation, trusted_domain='healthkey.ai')

Idempotent — safe to run multiple times.
"""
from django.core.management.base import BaseCommand
from patient_portal.models import Identity
from omop_core.models import Organization, OrgTrust


class Command(BaseCommand):
    help = 'Creates demo user (random@healthkey.ai) and healthkey.ai domain trust for ABC Foundation'

    def handle(self, *args, **options):
        self._setup_demo_user()
        self._setup_domain_trust()

    def _setup_demo_user(self):
        email = 'random@healthkey.ai'
        password = 'password123!'

        identity, created = Identity.objects.get_or_create(
            email=email,
            defaults={
                'issuer': 'urn:local',
                'is_staff': False,
                'is_superuser': False,
            },
        )

        if not created:
            identity.issuer = 'urn:local'

        identity.set_password(password)
        identity.save()

        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} demo user "{email}"'))

    def _setup_domain_trust(self):
        try:
            org = Organization.objects.get(slug='abc-foundation')
        except Organization.DoesNotExist:
            self.stdout.write(
                self.style.WARNING(
                    'Org with slug "abc-foundation" not found — skipping domain trust creation.'
                )
            )
            return

        trust, created = OrgTrust.objects.get_or_create(
            granting_org=org,
            trusted_domain='healthkey.ai',
            defaults={'trusted_org': None},
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Created domain trust: {org.slug} → healthkey.ai'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Domain trust already exists: {org.slug} → healthkey.ai'
                )
            )
