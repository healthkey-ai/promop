"""A patient's Garmin Connect Personal API key, saved for later syncs (#2834).

The sync by key (#2794) first kept the key nowhere, so the patient pasted it
for every sync. It is now saved when they ask for that, under these rules:

* **Encrypted at rest**, under ``GARMIN_TOKEN_KEY`` (``garmin_oauth.encrypt``):
  the Fernet key the linked account's tokens use, which is in no database. A
  deployment without it saves nothing and syncs as before.
* **Never given back.** Nothing here returns the key to a caller outside the
  sync that sends it to Garmin, and nothing logs it. ``describe`` says that a
  key is saved and when, and that is all a client learns.
* **The account's own.** A key is used only by the account that saved it. If
  the record is later linked to another account, that account never syncs
  with its predecessor's Garmin key: the row is removed when it tries.
* **Not deleted for being unreadable.** A key that cannot be decrypted (the
  deployment's key is unset, mistyped or was replaced without a fallback)
  reads as not saved and stays in place: putting the right key back makes it
  usable again, and a newly entered key overwrites it.
* **Removed** by the patient, when Garmin answers 401 to a sync that used it,
  and with the person or their record.

Syncing stays the patient's act: nothing here reads a saved key on a
schedule. Doing that is a separate decision.
"""
import logging

from django.db import transaction
from django.utils import timezone

from omop_core.services import garmin_oauth

logger = logging.getLogger(__name__)

SAVED_KEY_REJECTED = ('Garmin no longer accepts your saved API key, so it has been removed. '
                      'Create a new key in Garmin Connect and enter it here.')


def can_save():
    """Whether this deployment can keep a key: it has a key to encrypt under."""
    return garmin_oauth.can_encrypt()


def _row(person):
    from omop_core.models import GarminPersonalKey

    return GarminPersonalKey.objects.filter(person=person).first()


def _own(row, identity):
    """Whether ``identity`` is the account that saved ``row``."""
    return row.saved_by_id is not None and row.saved_by_id == getattr(identity, 'pk', None)


def _usable(person, identity):
    """``(row, key)`` for the key this account saved and can still read, else
    ``(None, None)``. Changes nothing."""
    row = _row(person)
    if row is None or not _own(row, identity):
        return None, None
    api_key = garmin_oauth.decrypt(row.api_key)
    if api_key is None:
        logger.warning('garmin_key_unreadable person_id=%s', person.pk)
        return None, None
    return row, api_key


def describe(person, identity):
    """What the tab may know: whether a usable key is saved and when, never
    the key."""
    row, _api_key = _usable(person, identity)
    return {
        'can_save': can_save(),
        'saved': row is not None,
        'saved_at': row.saved_at.isoformat() if row else None,
        'last_used_at': row.last_used_at.isoformat() if row and row.last_used_at else None,
    }


def is_saved(person, identity):
    return _usable(person, identity)[0] is not None


def save(person, identity, api_key):
    """Keep ``api_key`` for the person, replacing one already kept. False when
    nothing was kept: this deployment cannot encrypt it, or the record went
    off or away while Garmin was being read. The sync is unaffected."""
    from omop_core.models import GarminPersonalKey, Person
    from patient_portal.consent import record_permitted

    if not can_save() or getattr(identity, 'pk', None) is None:
        return False
    try:
        stored = garmin_oauth.encrypt(api_key)
    except garmin_oauth.GarminOAuthError:
        return False
    now = timezone.now()
    # The person locked, as an erasure takes it: a key is not left behind for
    # a record that was erased or switched off during the read.
    with transaction.atomic():
        locked = Person.objects.select_for_update(no_key=True).filter(pk=person.pk).first()
        if locked is None or not record_permitted(locked):
            return False
        GarminPersonalKey.objects.update_or_create(
            person=locked,
            defaults={'api_key': stored, 'saved_by': identity, 'saved_at': now, 'last_used_at': now},
        )
    logger.info('garmin_key_saved person_id=%s', person.pk)
    return True


def load(person, identity):
    """``(row, key)`` for a sync that sent no key, else ``(None, None)``.

    A key another account saved is removed here: whoever holds the record now
    is asked for their own. One that cannot be read is left alone.
    """
    from omop_core.models import GarminPersonalKey

    row = _row(person)
    if row is not None and not _own(row, identity):
        deleted, _ = GarminPersonalKey.objects.filter(pk=row.pk).delete()
        if deleted:
            logger.info('garmin_key_removed person_id=%s reason=other_account', person.pk)
        return None, None
    return _usable(person, identity)


def _unchanged(row):
    """The stored row, only while it still holds the key ``row`` was read with:
    a sync that began on an earlier key decides nothing about a later one."""
    from omop_core.models import GarminPersonalKey

    return GarminPersonalKey.objects.filter(pk=row.pk, api_key=row.api_key)


def used(row):
    """Garmin accepted the saved key just now."""
    _unchanged(row).update(last_used_at=timezone.now())


def refused(row):
    """Garmin answered that the saved key is no key of its. True when it was
    removed."""
    deleted, _ = _unchanged(row).delete()
    if deleted:
        logger.info('garmin_key_removed person_id=%s reason=refused', row.person_id)
    return bool(deleted)


def remove(person, reason):
    """Delete the person's saved key, whoever saved it. True when there was one."""
    from omop_core.models import GarminPersonalKey

    deleted, _ = GarminPersonalKey.objects.filter(person=person).delete()
    if deleted:
        logger.info('garmin_key_removed person_id=%s reason=%s', person.pk, reason)
    return bool(deleted)
