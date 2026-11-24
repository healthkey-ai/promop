"""A patient's Garmin Connect Personal API key, saved for later syncs (#2834).

The sync by key (#2794) first kept the key nowhere, so the patient pasted it
for every sync. It is now saved when they enter it, unless they untick the
box, under these rules:

* **Encrypted at rest**, under ``GARMIN_TOKEN_KEY`` (``garmin_oauth.encrypt``):
  the Fernet key the linked account's tokens use, which is in no database. A
  deployment without it saves nothing and syncs as before.
* **Never given back.** Nothing here returns the key to a caller outside the
  sync that sends it to Garmin, and nothing logs it. ``describe`` says that a
  key is saved and when, and that is all a client learns.
* **Removed** by the patient, when Garmin refuses it, when it can no longer
  be decrypted, and with the person or their record.

Syncing stays the patient's act: nothing here reads a saved key on a
schedule. Doing that is a separate decision.
"""
import logging

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


def describe(person):
    """What the tab may know: whether a key is saved and when, never the key."""
    row = _row(person)
    return {
        'can_save': can_save(),
        'saved': row is not None,
        'saved_at': row.saved_at.isoformat() if row else None,
        'last_used_at': row.last_used_at.isoformat() if row and row.last_used_at else None,
    }


def is_saved(person):
    from omop_core.models import GarminPersonalKey

    return GarminPersonalKey.objects.filter(person=person).exists()


def save(person, identity, api_key):
    """Keep ``api_key`` for the person, replacing one already kept. False where
    this deployment cannot encrypt it: the sync that sent it is unaffected."""
    from omop_core.models import GarminPersonalKey

    if not can_save():
        return False
    try:
        stored = garmin_oauth.encrypt(api_key)
    except garmin_oauth.GarminOAuthError:
        return False
    now = timezone.now()
    GarminPersonalKey.objects.update_or_create(
        person=person,
        defaults={'api_key': stored, 'saved_by': identity, 'saved_at': now, 'last_used_at': now},
    )
    logger.info('garmin_key_saved person_id=%s', person.pk)
    return True


def load(person):
    """The person's saved key, or None. One that cannot be decrypted any more
    (the deployment's key was replaced) is removed: it will never read again."""
    row = _row(person)
    if row is None:
        return None
    api_key = garmin_oauth.decrypt(row.api_key)
    if api_key is None:
        row.delete()
        logger.info('garmin_key_removed person_id=%s reason=unreadable', person.pk)
    return api_key


def used(person):
    """Garmin accepted the saved key just now."""
    from omop_core.models import GarminPersonalKey

    GarminPersonalKey.objects.filter(person=person).update(last_used_at=timezone.now())


def remove(person, reason):
    """Delete the person's saved key. True when there was one."""
    from omop_core.models import GarminPersonalKey

    deleted, _ = GarminPersonalKey.objects.filter(person=person).delete()
    if deleted:
        logger.info('garmin_key_removed person_id=%s reason=%s', person.pk, reason)
    return bool(deleted)
