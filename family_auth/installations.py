"""Which phr installation an account belongs to, for links sent to it.

A patient who signs up on a phr installation that PROMOP hosts identity for
should finish on that installation: the verification link points at its
/verify-email page and the email names it, not PROMOP. FAMILY_AUTH_APP_URLS
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


def installation_for(identity) -> Installation | None:
    """The installation this account signed up through, if any. A grant still
    waiting for email verification (the sign-up itself) wins over older ones."""
    urls = app_urls()
    if not urls:
        return None
    from omop_core.models import GroupAccess

    grant = (GroupAccess.objects.filter(identity=identity, org__slug__in=list(urls))
             .select_related("org").order_by("-pending_email_verification", "granted_at").first())
    if grant is None:
        return None
    return Installation(url=urls[grant.org.slug], name=grant.org.name)
