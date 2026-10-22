"""Originally added provenance columns to ConceptRelationship.

Superseded: CR is Athena's table and we do not extend its schema.  All
curation metadata lives in SourceCodeConceptMapping.  This migration is
retained as a no-op so the migration graph stays coherent for any database
that recorded it.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0195_detach_code_named_suggest_placeholders'),
    ]

    operations = [
        # No-op: provenance columns are no longer added to concept_relationship.
    ]
