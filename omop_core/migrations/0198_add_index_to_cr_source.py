"""Originally added db_index to ConceptRelationship.source.

Superseded: we no longer add columns to CR.  Retained as a no-op for
migration graph coherence.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0197_mirror_approved_mappings_to_cr'),
    ]

    operations = [
        # No-op.
    ]
