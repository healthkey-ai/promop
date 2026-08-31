"""Return code-named bulk-Suggest placeholders to the review queue (#863)."""
from django.db import migrations
from django.db.models import F


def detach_code_named_placeholders(apps, schema_editor):
    """Remove only the destinations manufactured by the old Suggest fallback.

    A proposed mapping created by ``origin_system='suggest'`` has not rewritten
    clinical rows, so clearing its destination is safe.  The restrictive shape
    below identifies the five staging HK-Labs rows that minted a local concept
    whose *name* is merely the incoming code; it leaves real local concepts and
    curator choices untouched.
    """
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    placeholders = Mapping.objects.filter(
        origin='import',
        origin_system='suggest',
        status='proposed',
        destination_vocabulary_id='HK-Labs',
        target_concept__vocabulary__vocabulary_id='HK-Labs',
        target_concept__source='HealthKey',
        target_concept__concept_name=F('source_code'),
    )
    placeholders.filter(source_code_description=F('source_code')).update(
        source_code_description='',
    )
    placeholders.update(target_concept_id=None, destination_vocabulary_id='')


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0194_source_code_mapping_reviewer'),
    ]

    operations = [
        migrations.RunPython(detach_code_named_placeholders, migrations.RunPython.noop),
    ]
