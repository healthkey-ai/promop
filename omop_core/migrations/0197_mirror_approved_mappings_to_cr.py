"""Originally mirrored approved SCCM rows to concept_relationship with provenance.

Superseded: the mirror now writes only standard CR columns (concept_1,
concept_2, relationship, valid dates) and happens on approval in the view
layer, not as a data migration.  Retained as a no-op for migration graph
coherence.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0196_add_provenance_to_concept_relationship'),
    ]

    operations = [
        # No-op.
    ]
