"""Mirror existing approved SourceCodeConceptMapping rows to concept_relationship.

For each SCCM row where status='approved' AND source_concept IS NOT NULL AND
target_concept IS NOT NULL, create a 'Maps to' (and reverse 'Mapped from') row
in concept_relationship with HealthKey provenance.  Existing CR rows (e.g.
Athena-loaded) are updated to add provenance rather than duplicated.
"""
import logging
from datetime import date

from django.db import migrations

logger = logging.getLogger(__name__)

MAPS_TO = 'Maps to'
MAPPED_FROM = 'Mapped from'


def mirror_approved_to_cr(apps, schema_editor):
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    ConceptRelationship = apps.get_model('omop_core', 'ConceptRelationship')
    Relationship = apps.get_model('omop_core', 'Relationship')

    # Ensure Relationship rows exist.
    maps_to, _ = Relationship.objects.get_or_create(
        relationship_id=MAPS_TO,
        defaults={
            'relationship_name': 'Maps to',
            'is_hierarchical': 0,
            'defines_ancestry': 0,
            'reverse_relationship_id': MAPPED_FROM,
            'relationship_concept_id': 0,
        },
    )
    mapped_from, _ = Relationship.objects.get_or_create(
        relationship_id=MAPPED_FROM,
        defaults={
            'relationship_name': 'Mapped from',
            'is_hierarchical': 0,
            'defines_ancestry': 0,
            'reverse_relationship_id': MAPS_TO,
            'relationship_concept_id': 0,
        },
    )

    approved = SourceCodeConceptMapping.objects.filter(
        status='approved',
        source_concept__isnull=False,
        target_concept__isnull=False,
    ).select_related('reviewer')

    created_count = 0
    updated_count = 0
    for sccm in approved.iterator():
        # Forward: Maps to
        cr, created = ConceptRelationship.objects.get_or_create(
            concept_1_id=sccm.source_concept_id,
            concept_2_id=sccm.target_concept_id,
            relationship_id=MAPS_TO,
            defaults={
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
                'source': 'HealthKey',
                'origin_system': sccm.origin_system or 'curator',
                'status': 'approved',
                'reviewer_id': sccm.reviewer_id,
                'reviewed_at': sccm.reviewed_at,
            },
        )
        if created:
            created_count += 1
        elif cr.source is None:
            # Athena row exists — mark as confirmed by HealthKey.
            cr.source = 'HealthKey'
            cr.status = 'approved'
            cr.origin_system = sccm.origin_system or 'curator'
            cr.reviewer_id = sccm.reviewer_id
            cr.reviewed_at = sccm.reviewed_at
            cr.save(update_fields=[
                'source', 'status', 'origin_system', 'reviewer_id', 'reviewed_at',
            ])
            updated_count += 1

        # Reverse: Mapped from
        ConceptRelationship.objects.get_or_create(
            concept_1_id=sccm.target_concept_id,
            concept_2_id=sccm.source_concept_id,
            relationship_id=MAPPED_FROM,
            defaults={
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
                'source': 'HealthKey',
                'origin_system': sccm.origin_system or 'curator',
                'status': 'approved',
                'reviewer_id': sccm.reviewer_id,
                'reviewed_at': sccm.reviewed_at,
            },
        )

    logger.info(
        'Mirrored approved SCCM → CR: %d created, %d updated (Athena confirmed).',
        created_count, updated_count,
    )


def reverse_noop(apps, schema_editor):
    # Cannot distinguish HealthKey-written CR rows from ones that coincidentally
    # matched Athena. Safe to leave them; the provenance columns tell the story.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0196_add_provenance_to_concept_relationship'),
    ]

    operations = [
        migrations.RunPython(mirror_approved_to_cr, reverse_noop),
    ]
