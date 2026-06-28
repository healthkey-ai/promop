"""
Sets up demo access for casual evaluators.

1. Creates (or updates) demo user: random@healthkey.ai
   Password: DEMO_PASSWORD env var, or 'password123!' as local-dev fallback.
2. Creates a domain trust: OrgTrust(granting_org=ABC Foundation, trusted_domain='healthkey.ai')

Idempotent — safe to run multiple times.

Production usage:
  DEMO_PASSWORD=<secret> python manage.py setup_demo
"""
import os

from django.core.management.base import BaseCommand
from patient_portal.models import Identity
from omop_core.models import Organization, OrgTrust


DEMO_EMAIL = 'random@healthkey.ai'
DEMO_DOMAIN = 'healthkey.ai'
ABC_SLUG = 'abc-foundation'


class Command(BaseCommand):
    help = 'Creates demo user and healthkey.ai domain trust for ABC Foundation (idempotent)'

    def handle(self, *args, **options):
        self._setup_demo_user()
        self._setup_domain_trust()

    def _setup_demo_user(self):
        password = os.environ.get('DEMO_PASSWORD', 'password123!')

        try:
            identity = Identity.objects.get(email=DEMO_EMAIL, issuer='urn:local')
            identity.is_staff = False
            identity.set_password(password)
            identity.save()
            self.stdout.write(self.style.SUCCESS(f'Updated demo user "{DEMO_EMAIL}"'))
        except Identity.DoesNotExist:
            Identity.objects.create_user(
                email=DEMO_EMAIL, password=password, is_staff=False,
            )
            self.stdout.write(self.style.SUCCESS(f'Created demo user "{DEMO_EMAIL}"'))

    def _setup_domain_trust(self):
        try:
            org = Organization.objects.get(slug=ABC_SLUG)
        except Organization.DoesNotExist:
            self.stdout.write(
                self.style.WARNING(
                    f'Org with slug "{ABC_SLUG}" not found — skipping domain trust creation.'
                )
            )
            return

        trust, created = OrgTrust.objects.get_or_create(
            granting_org=org,
            trusted_domain=DEMO_DOMAIN,
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(f'Created domain trust: {org.slug} → {DEMO_DOMAIN}')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'Domain trust already exists: {org.slug} → {DEMO_DOMAIN}')
            )
