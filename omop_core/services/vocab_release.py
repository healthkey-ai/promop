"""
Vocabulary release service helpers.

Provides convenience functions for querying vocabulary releases and
generating stable ETag strings for HTTP conditional-request support.
"""

import hashlib


def get_latest_release():
    """
    Return the most recently published VocabularyRelease, or None.
    """
    from omop_core.models import VocabularyRelease

    return (
        VocabularyRelease.objects
        .filter(status='published')
        # `-pk` is a deterministic tiebreaker: two releases can share a
        # published_at (bulk publish, or one captured timestamp), and without it
        # "latest" would be arbitrary per query — which can make the snapshot
        # view's single-latest guard 409 the /latest/ URL against itself.
        .order_by('-published_at', '-pk')
        .first()
    )


def get_release_etag(release):
    """
    Return a stable, quoted ETag string for the given VocabularyRelease.

    Format: ``"vr-{pk}-{sha256_prefix}"`` where the SHA-256 is derived
    from the PK and published_at timestamp to guarantee uniqueness across
    releases while remaining deterministic for the same release.
    """
    if release is None:
        return None

    ts = release.published_at.isoformat() if release.published_at else ''
    raw = f"{release.pk}:{ts}"
    sha = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f'"vr-{release.pk}-{sha}"'
