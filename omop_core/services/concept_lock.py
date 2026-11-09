"""A curator's claim on a standard concept for Concept to Code work (#2390).

One lock per concept. The holder maps; everyone else may open the concept
and review its source codes but every write for it -- propose, apply,
Suggest for the concept, and any mapping save whose destination it is --
is refused with 423 naming the holder. The holder, or a staff user, releases
it. Explicit and unexpiring, unlike the mapping dialog's per-row edit lock,
which protects one row while a dialog is open.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import APIException

from omop_core.models import ConceptMappingLock


def user_display(user):
    if user is None:
        return ''
    return (getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'email', '') or str(user))


class ConceptLocked(APIException):
    status_code = 423
    default_code = 'concept_locked'

    def __init__(self, lock):
        self.lock = lock
        super().__init__({
            'detail': f'{lock.concept.concept_name} is locked by {user_display(lock.locked_by)}.',
            'locked_by': user_display(lock.locked_by),
            'locked_at': lock.locked_at.isoformat() if lock.locked_at else None,
            'concept_id': lock.concept_id,
        })


def lock_payload(lock, user):
    """What a row shows: who holds the lock and whether it is the viewer's."""
    if lock is None:
        return None
    return {'username': user_display(lock.locked_by), 'locked_at': lock.locked_at,
            'mine': lock.locked_by_id == getattr(user, 'pk', None)}


def locks_for(concept_ids, user):
    """{concept_id: payload} for the concepts of one page, in one query."""
    rows = ConceptMappingLock.objects.filter(concept_id__in=concept_ids).select_related('locked_by')
    return {lock.concept_id: lock_payload(lock, user) for lock in rows}


def check_concept_lock(concept_id, user):
    """Raise ConceptLocked when another user holds the concept."""
    lock = ConceptMappingLock.objects.filter(concept_id=concept_id).select_related('locked_by', 'concept').first()
    if lock is not None and lock.locked_by_id != getattr(user, 'pk', None):
        raise ConceptLocked(lock)
    return lock


def acquire(concept, user):
    """Lock the concept for *user*; idempotent for the holder, 423 for anyone else."""
    with transaction.atomic():
        lock = (ConceptMappingLock.objects.select_for_update().select_related('locked_by', 'concept')
                .filter(concept=concept).first())
        if lock is None:
            lock = ConceptMappingLock.objects.create(concept=concept, locked_by=user)
        elif lock.locked_by_id != user.pk:
            raise ConceptLocked(lock)
        return lock


def release(concept, user):
    """Unlock; only the holder or staff may. Returns whether a lock was removed."""
    with transaction.atomic():
        lock = (ConceptMappingLock.objects.select_for_update().select_related('locked_by', 'concept')
                .filter(concept=concept).first())
        if lock is None:
            return False
        if lock.locked_by_id != user.pk and not getattr(user, 'is_staff', False):
            raise ConceptLocked(lock)
        lock.delete()
        return True
