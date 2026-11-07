"""Keep each phr installation's sign-up organization in place (phr#43).

A phr installation that PROMOP hosts identity for signs its patients up into
one Organization (POST /api/v1/orgs/<slug>/patient-signup/), which must exist
and allow patient sign-up. FAMILY_AUTH_SIGNUP_ORGS lists them as
``slug:Name`` pairs; every deploy runs this (scripts/prepare-deployment.sh),
so a fresh database gets them and a later toggle-off is put back.

Idempotent: a missing org is created with its name; an existing one keeps
its name and settings, except that patient sign-up is switched on. A
deactivated org is reported, not reactivated: that's an operator's call.
Unset or empty, it does nothing.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from omop_core.models import Organization


def parse(entries) -> list[tuple[str, str]]:
    orgs = []
    for entry in entries:
        slug, sep, name = entry.partition(":")
        slug, name = slug.strip(), name.strip()
        if not sep or not slug or not name or slugify(slug) != slug:
            raise CommandError(f'FAMILY_AUTH_SIGNUP_ORGS entry {entry!r} is not "slug:Name".')
        orgs.append((slug, name))
    return orgs


class Command(BaseCommand):
    help = "Create or update the sign-up organizations in FAMILY_AUTH_SIGNUP_ORGS."

    def handle(self, *args, **options):
        # A malformed installation URL would send verification links to
        # PROMOP instead of phr; refuse it here, at deploy.
        from family_auth.installations import InvalidAppUrls, parse_app_urls

        try:
            parse_app_urls(settings.FAMILY_AUTH_APP_URLS)
        except InvalidAppUrls as exc:
            raise CommandError(str(exc)) from exc
        orgs = parse(settings.FAMILY_AUTH_SIGNUP_ORGS)
        if not orgs:
            self.stdout.write("No FAMILY_AUTH_SIGNUP_ORGS; nothing to do.")
            return
        for slug, name in orgs:
            org, created = Organization.objects.get_or_create(
                slug=slug, defaults={"name": name, "allows_patient_signup": True},
            )
            if created:
                self.stdout.write(f"Created sign-up organization {slug}.")
                continue
            if not org.allows_patient_signup:
                org.allows_patient_signup = True
                org.save(update_fields=["allows_patient_signup"])
                self.stdout.write(f"Turned on patient sign-up for {slug}.")
            else:
                self.stdout.write(f"Sign-up organization {slug} is in place.")
            if not org.is_active:
                self.stderr.write(f"Organization {slug} is deactivated: its sign-up answers 404 until reactivated.")
