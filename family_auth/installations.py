"""Which phr installation an account belongs to, for links sent to it.

A patient who signs up on a phr installation that PROMOP hosts identity for
should finish on that installation: the verification link points at its
/verify-email page, the password-reset link at its /reset-password page when
it has one (FAMILY_AUTH_RESET_APPS), and the emails name it and sign off as
it, not PROMOP (``app_for``, ``reset_app_for``). FAMILY_AUTH_APP_URLS
maps each installation's sign-up org to its origin (``slug=https://…``); an
account with access to one of those orgs belongs to it.

The link's base comes only from this configuration, never from a request.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.text import slugify

logger = logging.getLogger(__name__)


class InvalidAppUrls(ValueError):
    """FAMILY_AUTH_APP_URLS has an entry that is not ``slug=https://origin``."""


@dataclass(frozen=True)
class Installation:
    url: str
    name: str
    slug: str = ""

    @property
    def sign_off(self) -> str:
        """Who an email from it is from: the installation, or HealthKey for PROMOP."""
        return self.name if self.slug else "The HealthKey team"


# Roles that run an organisation rather than belong to it as its patient or
# member: they keep PROMOP's own pages, where staff work.
_RUNS_THE_ORG = ("org_admin", "analyst")


def parse_app_urls(entries) -> dict[str, str]:
    urls = {}
    for entry in entries:
        slug, sep, url = entry.partition("=")
        slug, url = slug.strip(), url.strip().rstrip("/")
        parts = urlsplit(url)
        if (not sep or not slug or slugify(slug) != slug or parts.scheme not in ("https", "http")
                or not parts.netloc or parts.path or parts.query or parts.fragment):
            raise InvalidAppUrls(f'FAMILY_AUTH_APP_URLS entry {entry!r} is not "slug=https://origin".')
        urls[slug] = url
    return urls


def app_urls() -> dict[str, str]:
    """The configured mapping. A malformed setting is logged and ignored here
    (mail falls back to PROMOP's own links); the deploy step refuses it."""
    try:
        return parse_app_urls(settings.FAMILY_AUTH_APP_URLS)
    except InvalidAppUrls:
        logger.exception("Ignoring FAMILY_AUTH_APP_URLS")
        return {}


def promop() -> Installation:
    return Installation(url=settings.APP_BASE_URL.rstrip("/"), name="PROMOP")


def app_for(identity) -> Installation:
    """Where links emailed to this account point, and what the email calls
    it: the installation it signed up through, else PROMOP itself."""
    return installation_for(identity) or promop()


def reset_app_for(identity, app: Installation | None = None) -> Installation:
    """``app_for`` for a password reset: an installation sends its accounts to
    its own /reset-password only if it has that page (its slug is in
    FAMILY_AUTH_RESET_APPS); otherwise PROMOP's page completes the reset, and
    the email still names the installation."""
    app = app if app is not None else app_for(identity)
    if app.slug and app.slug not in set(settings.FAMILY_AUTH_RESET_APPS):
        return Installation(url=promop().url, name=app.name, slug=app.slug)
    return app


def installation_for(identity) -> Installation | None:
    """The installation this account signed up through, if any. A grant still
    waiting for email verification (the sign-up itself) wins over older ones.

    Only a grant that makes the account the installation's own: not one that
    has expired or is at an inactive org, nor one to run the org
    (``_RUNS_THE_ORG``); and never for staff, who keep PROMOP's pages.
    """
    urls = app_urls()
    if not urls or getattr(identity, "is_staff", False) or getattr(identity, "is_superuser", False):
        return None
    from django.db.models import Q
    from django.utils import timezone
    from omop_core.models import GroupAccess

    grant = (GroupAccess.objects
             .filter(identity=identity, org__slug__in=list(urls), org__is_active=True)
             .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
             .exclude(role__in=_RUNS_THE_ORG)
             .select_related("org").order_by("-pending_email_verification", "granted_at").first())
    if grant is None:
        return None
    return Installation(url=urls[grant.org.slug], name=grant.org.name, slug=grant.org.slug)
