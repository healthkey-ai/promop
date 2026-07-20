"""Seed the PostTransformationOutcome vocabulary (FL → DLBCL transformation outcomes)."""
from django.db import migrations

_VALUES = [
    ('complete-response', 'Complete Response'),
    ('partial-response', 'Partial Response'),
    ('stable-disease', 'Stable Disease'),
    ('progressive-disease', 'Progressive Disease'),
    ('deceased', 'Deceased'),
    ('unknown', 'Unknown'),
]


def seed(apps, schema_editor):
    Model = apps.get_model('omop_core', 'PostTransformationOutcome')
    for code, title in _VALUES:
        Model.objects.get_or_create(code=code, defaults={'title': title})


def unseed(apps, schema_editor):
    Model = apps.get_model('omop_core', 'PostTransformationOutcome')
    Model.objects.filter(code__in={code for code, _ in _VALUES}).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0114_posttransformationoutcome_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
