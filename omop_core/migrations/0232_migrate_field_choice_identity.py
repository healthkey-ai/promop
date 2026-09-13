"""Preserve legacy option identity; old source codes become proposals only."""
from hashlib import sha256

from django.db import migrations
from django.utils.text import slugify


def migrate_choices(apps, schema_editor):
    Choice = apps.get_model('omop_core', 'FieldChoice')
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldValueConceptMapping')
    db = schema_editor.connection.alias
    for choice in Choice.objects.using(db).prefetch_related('codes').all().iterator(chunk_size=500):
        choice.code = (slugify(choice.display)[:80] or 'value') + '-' + sha256(choice.display.encode()).hexdigest()[:12]
        choice.canonical_value = choice.display
        choice.save(using=db, update_fields=['code', 'canonical_value'])
        primary = [c for c in choice.codes.all() if c.is_primary]
        target = None
        if len(primary) == 1:
            matches = list(Concept.objects.using(db).filter(vocabulary_id=primary[0].vocabulary_id, concept_code=primary[0].code)[:2])
            target = matches[0] if len(matches) == 1 else None
        Mapping.objects.using(db).get_or_create(choice=choice, defaults={
            'target_concept': target, 'status': 'proposed',
            'outcome': 'ambiguous' if len(primary) > 1 else 'needs_review',
            'notes': 'Migrated legacy choice code; standard status, role and clinical meaning require review.',
        })


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0231_field_value_concept_mapping')]
    operations = [migrations.RunPython(migrate_choices, migrations.RunPython.noop)]
