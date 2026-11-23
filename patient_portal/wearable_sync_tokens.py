"""Wearable sync tokens: what a patient's phone holds to add wearable values (#2800).

The raw token exists in two places only: the response that issues it, and the
patient's phone. This module never logs it and the database holds its SHA-256.
A token of 256 random bits needs no salt or slow hash: there is nothing to
guess from the digest.
"""
import hashlib
import re
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from omop_core.models import Person
from patient_portal.models import PatientUser, WearableSyncToken

#: Tokens start with this, so one pasted into the wrong place is recognisable
#: (and a secret scanner can be told what to look for).
PREFIX = 'hkws_'

#: A token works for a year, then the patient makes a new one.
LIFETIME = timedelta(days=365)

_TOKEN_RE = re.compile(r'hkws_[A-Za-z0-9_-]{43}')
_HINT_CHARS = 4


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode('ascii')).hexdigest()


def patient_person(identity):
    """The person an account is the patient of, or None. One rule for making a
    token and for using it."""
    link = (PatientUser.objects.select_related('person')
            .filter(identity=identity, is_active=True).first())
    return link.person if link else None


def current(person, device_type: str):
    """The person's token row for a device type, usable or not, or None."""
    return (WearableSyncToken.objects.select_related('created_by')
            .filter(person=person, device_type=device_type).first())


def usable(row) -> bool:
    """True while the token may add values: not expired, not stopped by a
    password change, and its creator could still upload themselves."""
    creator = row.created_by
    return (
        not row.is_expired
        and row.stopped_at is None
        and creator.is_active
        and not getattr(creator, 'must_change_password', False)
        and PatientUser.objects.filter(
            identity=creator, person_id=row.person_id, is_active=True).exists()
    )


@transaction.atomic
def issue(person, identity, device_type: str):
    """Replace the person's token for a device type. Returns ``(row, raw token)``.

    The raw token is returned here and nowhere else.
    """
    # One at a time per patient: two requests at once would both delete, and
    # the second insert would trip the one-per-device constraint.
    Person.objects.select_for_update(no_key=True).filter(pk=person.pk).first()
    WearableSyncToken.objects.filter(person=person, device_type=device_type).delete()
    raw = PREFIX + secrets.token_urlsafe(32)
    row = WearableSyncToken.objects.create(
        person=person, device_type=device_type, token_hash=_digest(raw),
        hint=raw[-_HINT_CHARS:], created_by=identity,
        expires_at=timezone.now() + LIFETIME,
    )
    return row, raw


def stop_for_identity(identity) -> int:
    """Stop every token an account made. Called when its password is changed
    or reset (``services.set_new_password``): whoever held the account before
    keeps nothing, as their sessions end. The row stays, stopped, so the tab
    can say why the phone no longer syncs. Returns how many were stopped."""
    return WearableSyncToken.objects.filter(
        created_by=identity, stopped_at__isnull=True).update(stopped_at=timezone.now())


def revoke(person, device_type: str) -> bool:
    """Delete the person's token for a device type. True when there was one."""
    deleted, _ = WearableSyncToken.objects.filter(
        person=person, device_type=device_type).delete()
    return bool(deleted)


def bearer(header) -> str | None:
    """The token in an ``Authorization: Bearer`` header, if it has a token's shape."""
    if not isinstance(header, str):
        return None
    scheme, _, raw = header.strip().partition(' ')
    raw = raw.strip()
    if scheme.lower() != 'bearer' or not _TOKEN_RE.fullmatch(raw):
        return None
    return raw


def resolve(raw: str | None, device_type: str):
    """The usable token row for a raw token, or None.

    None covers every way a token fails, and the caller answers them alike: not
    a token, unknown, for another device type, or not ``usable``.
    """
    if not raw or not _TOKEN_RE.fullmatch(raw):
        return None
    row = (WearableSyncToken.objects
           .select_related('person', 'created_by')
           .filter(token_hash=_digest(raw), device_type=device_type).first())
    return row if row is not None and usable(row) else None


def touch(row) -> None:
    """Record that the token was just used."""
    row.last_used_at = timezone.now()
    WearableSyncToken.objects.filter(pk=row.pk).update(last_used_at=row.last_used_at)
