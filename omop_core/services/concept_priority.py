"""Curator priority for Concept to Code (#1914).

Ranked concepts form one global list, positions 1..N with no gaps. A move
places one concept at a position (or removes it) and renumbers the rest, so a
curator can rank a concept from any page or filter without seeing the others.
"""
from django.db import connection, transaction
from django.utils import timezone

from omop_core.models import ConceptMappingPriority


def _lock():
    # Row locks cannot stop two moves from both inserting at "the end": neither
    # sees the other's new row. One move at a time keeps positions dense.
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute(f'LOCK TABLE {ConceptMappingPriority._meta.db_table} IN EXCLUSIVE MODE')


def move(concept, position, user=None):
    """Put concept at position (1-based, clamped to the list), or unrank it with None.

    Returns the concept's position afterwards, or None when it is unranked.
    """
    with transaction.atomic():
        _lock()
        rows = {row.concept_id: row for row in ConceptMappingPriority.objects.order_by('position', 'concept_id')}
        order = [concept_id for concept_id in rows if concept_id != concept.pk]
        if position is None:
            ConceptMappingPriority.objects.filter(concept_id=concept.pk).delete()
        else:
            order.insert(min(max(position, 1), len(order) + 1) - 1, concept.pk)

        changed, now = [], timezone.now()
        for index, concept_id in enumerate(order, start=1):
            row = rows.get(concept_id)
            if row is None:
                ConceptMappingPriority.objects.create(concept_id=concept_id, position=index, updated_by=user)
            elif row.position != index or concept_id == concept.pk:
                row.position, row.updated_at = index, now  # bulk_update skips auto_now
                if concept_id == concept.pk:
                    row.updated_by = user
                changed.append(row)
        if changed:
            ConceptMappingPriority.objects.bulk_update(changed, ['position', 'updated_by', 'updated_at'])
        return None if position is None else order.index(concept.pk) + 1


def positions(domain_id=None):
    """{concept_id: position} for ranked concepts, in one domain when given."""
    rows = ConceptMappingPriority.objects.all()
    if domain_id:
        rows = rows.filter(concept__domain_id=domain_id)
    return dict(rows.values_list('concept_id', 'position'))
